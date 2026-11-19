"""GPS webhook, dead-letter replay/discard and audit log against the real schema."""

from __future__ import annotations

import json
from datetime import timedelta

import psycopg
import pytest

from tests.helpers import row, write_export
from tower.erp import process_file
from tower.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign

pytestmark = pytest.mark.db
ADMIN = {"X-Admin-Key": "test-admin"}
ASHA = {"Authorization": "Bearer test-asha"}


def ping(pid, ship, at, lat=17.5, lon=78.5, **kw):
    return {"ping_id": pid, "shipment_id": ship, "vehicle": "TS-09-1234", "lat": lat, "lon": lon,
            "speed_kmph": 40, "event_ts": at.isoformat(), **kw}


def post(client, clock, pings, secret="test-gps-secret", ts=None):
    body = json.dumps({"pings": pings}).encode()
    ts = int(clock.now().timestamp()) if ts is None else ts
    return client.post("/ingest/gps", content=body, headers={TIMESTAMP_HEADER: str(ts), SIGNATURE_HEADER: sign(secret, ts, body)})


def seed_shipment(pool, settings, clock, tmp_path, ship="SH-1001"):
    process_file(pool, settings, clock, write_export(tmp_path / "in", "erp_export_20261113_0500.csv", [row(ship)]))


def audit_mark(conn):
    return conn.execute("SELECT COALESCE(max(id), 0) AS n FROM audit_log").fetchone()["n"]


def position(conn, ship="SH-1001"):
    return conn.execute("SELECT last_lat, last_position_at FROM shipment WHERE id = %s", (ship,)).fetchone()


def test_ping_updates_position_and_duplicates_are_suppressed(client, clock, pool, settings, conn, tmp_path):
    seed_shipment(pool, settings, clock, tmp_path)
    p = ping("P-0001", "SH-1001", clock.now() - timedelta(minutes=5), lat=18.0)
    r = post(client, clock, [p, p])
    assert r.status_code == 200 and r.json()["accepted"] == 1 and r.json()["duplicate"] == 1
    assert position(conn)["last_lat"] == 18.0
    assert conn.execute("SELECT count(*) AS n FROM gps_ping").fetchone()["n"] == 1


def test_out_of_order_ping_is_stored_but_never_moves_position_back(client, clock, pool, settings, conn, tmp_path):
    seed_shipment(pool, settings, clock, tmp_path)
    newer = ping("P-0002", "SH-1001", clock.now() - timedelta(minutes=2), lat=19.0)
    older = ping("P-0001", "SH-1001", clock.now() - timedelta(minutes=30), lat=18.0)
    assert post(client, clock, [newer]).json()["accepted"] == 1
    r = post(client, clock, [older])
    assert r.json()["reordered"] == 1
    assert position(conn)["last_lat"] == 19.0
    assert conn.execute("SELECT count(*) AS n FROM gps_ping").fetchone()["n"] == 2


def test_ping_before_its_erp_row_is_picked_up_when_the_shipment_arrives(client, clock, pool, settings, conn, tmp_path):
    post(client, clock, [ping("P-0001", "SH-1001", clock.now() - timedelta(minutes=5), lat=18.25)])
    seed_shipment(pool, settings, clock, tmp_path)
    assert position(conn)["last_lat"] == 18.25


def test_future_skewed_ping_goes_to_dead_letters_and_can_be_replayed(client, clock, pool, settings, conn, tmp_path):
    seed_shipment(pool, settings, clock, tmp_path)
    skewed = ping("P-0009", "SH-1001", clock.now() + timedelta(minutes=30), lat=18.5)
    r = post(client, clock, [skewed])
    assert r.json()["dead_lettered"] == 1 and r.json()["accepted"] == 0
    assert position(conn)["last_position_at"] is None
    dl = conn.execute("SELECT * FROM dead_letter WHERE feed = 'gps'").fetchone()
    assert dl["reason_code"] == "clock_skew_future" and "event_ts" in dl["suggested_patch"]
    res = client.post(f"/dead-letters/{dl['id']}/replay", headers=ADMIN, json={})   # applies the suggested patch
    assert res.status_code == 200 and res.json()["outcome"] == "accepted"
    assert position(conn)["last_lat"] == 18.5


def test_bad_signature_and_stale_timestamp_are_rejected(client, clock, conn):
    p = [ping("P-0001", "SH-1001", clock.now())]
    assert post(client, clock, p, secret="wrong").status_code == 401
    assert post(client, clock, p, ts=int(clock.now().timestamp()) - 1000).status_code == 401
    assert conn.execute("SELECT count(*) AS n FROM gps_ping").fetchone()["n"] == 0
    h = conn.execute("SELECT * FROM feed_health WHERE feed = 'gps'").fetchone()
    assert h["rejected"] == 2 and h["counters"]["bad_signature"] == 2


def test_replay_with_corrected_payload_succeeds_and_is_audited(client, clock, pool, settings, conn, tmp_path):
    p = write_export(tmp_path / "in", "erp_export_20261113_0500.csv", [row("SH-1003", city="HYD-2")])
    process_file(pool, settings, clock, p)
    mark = audit_mark(conn)
    dl = conn.execute("SELECT * FROM dead_letter").fetchone()
    assert dl["status"] == "waiting"
    assert client.post(f"/dead-letters/{dl['id']}/replay", json={}).status_code == 401      # admin key required
    bad = client.post(f"/dead-letters/{dl['id']}/replay", headers=ADMIN, json={"payload": {"city": "Nowhere"}})
    assert bad.status_code == 422 and bad.json()["reason_code"] == "unknown_zone"
    ok = client.post(f"/dead-letters/{dl['id']}/replay", headers={**ADMIN, "X-Actor": "asha"}, json={"payload": {"city": "Z01"}})
    assert ok.status_code == 200 and ok.json()["outcome"] == "new"
    s = conn.execute("SELECT zone_id, source FROM shipment WHERE id = 'SH-1003'").fetchone()
    assert s == {"zone_id": "Z01", "source": "replay"}
    after = conn.execute("SELECT status, attempts, replayed_at FROM dead_letter WHERE id = %s", (dl["id"],)).fetchone()
    assert after["status"] == "replayed" and after["attempts"] == 2 and after["replayed_at"] is not None
    entries = conn.execute("SELECT actor, action, detail FROM audit_log WHERE id > %s ORDER BY id", (mark,)).fetchall()
    assert [e["action"] for e in entries] == ["dead_letter.replay_failed", "dead_letter.replay"]
    assert entries[1]["actor"] == "asha" and entries[1]["detail"]["applied"] == {"city": "Z01"}
    assert client.post(f"/dead-letters/{dl['id']}/replay", headers=ADMIN, json={}).status_code == 409   # already replayed


def test_replay_with_suggested_fix_and_discard_are_audited(client, clock, pool, settings, conn, tmp_path):
    p = write_export(tmp_path / "in", "erp_export_20261113_0500.csv", [row("SH-1004", promised=""), row("SH-1005", amount="TBD")])
    process_file(pool, settings, clock, p)
    mark = audit_mark(conn)
    a = conn.execute("SELECT id FROM dead_letter WHERE reason_code = 'missing_promised_at'").fetchone()["id"]
    b = conn.execute("SELECT id FROM dead_letter WHERE reason_code = 'bad_amount'").fetchone()["id"]
    assert client.post(f"/dead-letters/{a}/replay", headers=ADMIN, json={}).json()["ok"] is True
    assert client.post(f"/dead-letters/{b}/discard", headers=ADMIN, json={"note": "test"}).status_code == 200
    assert conn.execute("SELECT count(*) AS n FROM shipment").fetchone()["n"] == 1
    got = client.get("/dead-letters", params={"status": "discarded"}, headers=ASHA).json()
    assert got["total"] == 1 and got["items"][0]["id"] == b
    actions = [r["action"] for r in conn.execute(
        "SELECT action FROM audit_log WHERE id > %s ORDER BY id", (mark,)).fetchall()]
    assert actions == ["dead_letter.replay", "dead_letter.discard"]
    # the same bad row arriving again (a reprocessed file) is not resurrected
    process_file(pool, settings, clock, p, skip_seen_check=True)
    assert client.get("/dead-letters", params={"status": "waiting"}, headers=ASHA).json()["total"] == 0


def test_audit_log_rejects_update_delete_and_truncate(conn, pool):
    with pool.connection() as c:
        c.execute("INSERT INTO audit_log (actor, action, target) VALUES ('t', 'test.append', 'x')")
    for stmt in ("UPDATE audit_log SET actor = 'evil'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation), pool.connection() as c:
            c.execute(stmt)
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE actor = 'evil'").fetchone()["n"] == 0


def test_read_apis(client, clock, pool, settings, tmp_path):
    seed_shipment(pool, settings, clock, tmp_path)
    assert client.get("/shipments", params={"dc": "DC-HYD"}, headers=ASHA).json()["total"] == 1
    assert client.get("/shipments", params={"dc": "DC-MUM"}, headers=ASHA).json()["total"] == 0
    assert client.get("/shipments/SH-1001", headers=ASHA).json()["lines"][0]["sku"] == "PE-001"
    net = client.get("/network", headers=ASHA).json()
    assert len(net["warehouses"]) == 4 and len(net["zones"]) == 36 and "5010 0033 4471" not in json.dumps(net)   # bank account masked even for VP Operations
    feeds = {f["feed"]: f for f in client.get("/health/feeds", headers=ASHA).json()["feeds"]}
    assert feeds["erp"]["accepted"] == 1
