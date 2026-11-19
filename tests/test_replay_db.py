"""Chaos-drill findings 2 and 3: a replay never overwrites fresher data, and its result says what it did."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.helpers import row, snapshot, write_export
from tower.clock import IST
from tower.erp import process_file

pytestmark = pytest.mark.db
ADMIN = {"X-Admin-Key": "test-admin"}
PROMISED = datetime(2026, 11, 14, 18, 0, tzinfo=IST)


def export(pool, settings, clock, tmp_path, hhmm: str, rows: list[str], **kw) -> dict:
    return process_file(pool, settings, clock, write_export(tmp_path / "in", f"erp_export_20261113_{hhmm}.csv", rows), **kw)


def dead_letter(conn, code: str) -> dict:
    return conn.execute("SELECT * FROM dead_letter WHERE reason_code = %s", (code,)).fetchone()


def shipment(conn, ship="SH-1001") -> dict:
    return conn.execute("SELECT * FROM shipment WHERE id = %s", (ship,)).fetchone()


def test_replayed_row_older_than_a_confirming_export_is_stale(client, pool, settings, clock, conn, tmp_path):
    """05:00 and 10:00 carry the same row; 08:00 has the promised date blank. The 10:00 row is a duplicate of what is
    stored, yet it is the newest export holding that content: replaying the 08:00 row must not win."""
    export(pool, settings, clock, tmp_path, "0500", [row("SH-1001", promised="14/11/2026 18:00")])
    export(pool, settings, clock, tmp_path, "0800", [row("SH-1001", promised="")])
    ten = [row("SH-1001", promised="14/11/2026 18:00"), row("SH-1002")]          # another row: not the same file
    assert export(pool, settings, clock, tmp_path, "1000", ten)["duplicate"] == 1
    assert shipment(conn)["source_exported_at"] == datetime(2026, 11, 13, 10, 0, tzinfo=IST)
    before = snapshot(conn)
    dl = dead_letter(conn, "missing_promised_at")
    r = client.post(f"/dead-letters/{dl['id']}/replay", headers=ADMIN, json={})
    assert r.status_code == 200 and r.json()["outcome"] == "stale" and r.json()["result"] == "stale"
    s = shipment(conn)
    assert s["promised_at"] == PROMISED and s["version"] == 1
    assert snapshot(conn) == before
    # the row-level re-drop of all three exports leaves the state where it was (the drill's md5 check)
    for hhmm, rows in (("0500", [row("SH-1001", promised="14/11/2026 18:00")]), ("0800", [row("SH-1001", promised="")]),
                       ("1000", ten)):
        export(pool, settings, clock, tmp_path, hhmm, rows, skip_seen_check=True)
    assert snapshot(conn)["state"] == before["state"]


def test_suggested_fix_for_a_blank_field_keeps_the_stored_value(client, pool, settings, clock, conn, tmp_path):
    """The newer row is applied (its ETA changed), but its blank promised date does not become departed + transit."""
    export(pool, settings, clock, tmp_path, "0700", [row("SH-1001", promised="14/11/2026 18:00")])
    export(pool, settings, clock, tmp_path, "0800", [row("SH-1001", promised="", eta="2026-11-14T03:00:00+05:30")])
    dl = dead_letter(conn, "missing_promised_at")
    suggested = datetime.fromisoformat(dl["suggested_patch"]["promised_at"])
    assert suggested != PROMISED
    r = client.post(f"/dead-letters/{dl['id']}/replay", headers=ADMIN, json={})
    assert r.status_code == 200 and r.json()["result"] == "applied" and r.json()["outcome"] == "updated"
    s = shipment(conn)
    assert s["promised_at"] == PROMISED and s["eta"] == datetime(2026, 11, 14, 3, 0, tzinfo=IST) and s["version"] == 2
    detail = conn.execute("SELECT detail FROM audit_log WHERE action = 'dead_letter.replay' ORDER BY id DESC LIMIT 1").fetchone()["detail"]
    assert detail["kept_stored"] == ["promised_at"] and detail["result"] == "applied"


def test_suggested_fix_still_fills_the_blank_on_a_new_shipment(client, pool, settings, clock, conn, tmp_path):
    export(pool, settings, clock, tmp_path, "0800", [row("SH-1001", promised="")])
    dl = dead_letter(conn, "missing_promised_at")
    r = client.post(f"/dead-letters/{dl['id']}/replay", headers=ADMIN, json={})
    assert r.json()["result"] == "applied" and r.json()["outcome"] == "new"
    assert shipment(conn)["promised_at"] == datetime.fromisoformat(dl["suggested_patch"]["promised_at"])


def test_replay_result_separates_applied_unchanged_and_failed(client, pool, settings, clock, conn, tmp_path):
    export(pool, settings, clock, tmp_path, "0500", [row("SH-1001"), row("SH-1002", city="HYD-2")])
    a = dead_letter(conn, "unknown_zone")["id"]
    failed = client.post(f"/dead-letters/{a}/replay", headers=ADMIN, json={"payload": {"city": "Nowhere"}})
    assert failed.status_code == 422 and failed.json()["result"] == "failed"
    assert client.post(f"/dead-letters/{a}/replay", headers=ADMIN, json={}).json()["result"] == "applied"
    # a row whose content is already stored changes nothing
    export(pool, settings, clock, tmp_path, "0600", [row("SH-1001", amount="TBD")])
    b = dead_letter(conn, "bad_amount")["id"]
    fixed_value = conn.execute("SELECT value_inr FROM shipment WHERE id = 'SH-1001'").fetchone()["value_inr"]
    corrected = {"value": str(fixed_value)}
    assert client.post(f"/dead-letters/{b}/replay", headers=ADMIN, json={"payload": corrected}).json()["result"] == "unchanged"
    h = conn.execute("SELECT counters FROM feed_health WHERE feed = 'erp'").fetchone()["counters"]
    assert h["replayed"] == 2 and h["replayed_applied"] == 1 and h["replayed_unchanged"] == 1


def test_gps_replay_of_a_ping_already_stored_is_unchanged(client, pool, settings, clock, conn, tmp_path):
    from tests.test_gps_dlq_db import ping, post

    export(pool, settings, clock, tmp_path, "0500", [row("SH-1001")])
    skewed = ping("P-0009", "SH-1001", clock.now() + timedelta(minutes=30))
    post(client, clock, [skewed])
    post(client, clock, [{**skewed, "event_ts": (clock.now() - timedelta(minutes=1)).isoformat()}])   # device fixed its clock
    dl = dead_letter(conn, "clock_skew_future")
    r = client.post(f"/dead-letters/{dl['id']}/replay", headers=ADMIN, json={})
    assert r.json()["outcome"] == "duplicate" and r.json()["result"] == "unchanged"
    assert conn.execute("SELECT count(*) AS n FROM gps_ping").fetchone()["n"] == 1
