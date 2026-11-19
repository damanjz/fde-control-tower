"""Milestone 3 endpoints for the UI: /live, the outage switch, and dead-letter replay and discard with a bearer token.
Each one keeps the role rules: scoped rows, VP Operations only for the switch and the queue, and an audit row per write."""

from __future__ import annotations

import httpx
import pytest

from sim.erp_writer import write_malformed_export
from sim.world import SCENARIO_END, World
from tests.conftest import AS_ASHA, AS_KIRAN, AS_MEERA
from tests.world import run_sql
from tower import erp, outage
from tower.config import load_settings
from tower.store import RowError, add_dead_letter

pytestmark = pytest.mark.db
RISKS = {"on time", "watch", "at risk"}


# ------------------------------------------------------------------ /live
def test_live_requires_a_token(client):
    assert client.get("/live").status_code == 401
    assert client.get("/live", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_live_is_scoped_and_carries_no_pii(client, world):
    asha = client.get("/live", headers=AS_ASHA).json()
    kiran = client.get("/live", headers=AS_KIRAN).json()
    ids = {s["id"] for s in asha["shipments"]}
    assert set(world["ships"].values()) <= ids
    assert {s["dc_id"] for s in kiran["shipments"]} == {"DC-HYD"}
    assert {s["id"] for s in kiran["shipments"]} == {world["ships"][k] for k in ("H1", "H2", "H3")}
    for row in asha["shipments"] + kiran["shipments"]:
        assert row["risk"] in RISKS
        assert not {"driver_name", "driver_phone", "vehicle_no", "bol_no"} & set(row)


def test_live_risk_follows_the_impact_engine(client, world):
    rows = {s["id"]: s for s in client.get("/live", headers=AS_ASHA).json()["shipments"]}
    for s in rows.values():
        if s["days_late"] > 0:
            assert s["risk"] == "at risk"
        elif s["incidents"]:
            assert s["risk"] == "watch"
    k1 = rows[world["ships"]["K1"]]
    assert world["fac"] in k1["incidents"] and world["geo"] in k1["incidents"]


def test_live_exposure_counts_each_shipment_once(client, world):
    live = client.get("/live", headers=AS_ASHA).json()
    assert live["exposure_inr"] == pytest.approx(sum(s["net_penalty_inr"] for s in live["shipments"]), abs=0.01)
    by_incident = sum(i["net_inr"] for i in client.get("/incidents", headers=AS_ASHA).json()["items"])
    assert live["exposure_inr"] <= by_incident + 0.01


def test_live_dead_letter_count_only_for_vp_operations(client, world, pool):
    with pool.connection() as c:
        add_dead_letter(c, "erp", world_now(), {"raw_line": "x;y"}, RowError("malformed_row", "broken"))
    assert client.get("/live", headers=AS_ASHA).json()["dead_letters_waiting"] >= 1
    assert client.get("/live", headers=AS_KIRAN).json()["dead_letters_waiting"] is None
    assert client.get("/live", headers=AS_MEERA).json()["dead_letters_waiting"] is None


def test_live_plans_follow_playbook_visibility(client, world):
    fac = world["fac"]
    run_sql(client.app.state.pool,
            """INSERT INTO playbook (incident_id, role, action_type, title, dc_scope, status, executed_by, executed_at)
               VALUES (%s, 'vp_operations', 'update_eta', 'Update ETAs', '{DC-KOL}', 'executed', 'asha', now())""", (fac,))
    assert client.get("/live", headers=AS_ASHA).json()["plans"][str(fac)]["executed_by"] == "asha"
    assert str(fac) not in client.get("/live", headers=AS_KIRAN).json()["plans"]


# ------------------------------------------------------------------ the outage switch
class FakeSim:
    """The simulator's admin API in memory: a fault table and a log of calls."""

    def __init__(self, up: bool = True):
        self.faults: dict = {}
        self.calls: list[tuple[str, str]] = []
        self.up = up

    def handler(self, req: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused", request=req)
        assert req.headers["X-Admin-Key"] == "ka"
        self.calls.append((req.method, req.url.path))
        if req.url.path == "/admin/faults" and req.method == "PUT":
            body = httpx.Response(200, content=req.content).json()
            self.faults[body["carrier"]] = body
        elif req.url.path == "/admin/faults" and req.method == "DELETE":
            self.faults.clear()
        elif req.url.path == "/admin/erp-malformed":
            return httpx.Response(200, json={"file": "erp_export_20261113_0800_outage.csv", "rows": 41})
        return httpx.Response(200, json=self.faults)


@pytest.fixture()
def sim(monkeypatch):
    fake = FakeSim()
    monkeypatch.setattr(outage, "sim_client", lambda s: httpx.Client(base_url="http://sim", headers={"X-Admin-Key": s.carrier_admin_key},
                                                                       transport=httpx.MockTransport(fake.handler)))
    return fake


def test_outage_needs_a_token_and_vp_operations(client, sim):
    assert client.get("/outage").status_code == 401
    assert client.post("/outage", json={"on": True}).status_code == 401
    assert client.post("/outage", json={"on": True}, headers=AS_KIRAN).status_code == 403
    assert client.post("/outage", json={"on": True}, headers=AS_MEERA).status_code == 403
    assert client.post("/outage", json={"on": "yes"}, headers=AS_ASHA).status_code == 400
    assert sim.calls == [] or all(m == "GET" for m, _ in sim.calls)
    assert client.get("/outage", headers=AS_KIRAN).json()["can_toggle"] is False
    assert client.get("/outage", headers=AS_ASHA).json()["can_toggle"] is True


def test_outage_switch_on_and_off_is_audited(client, sim, pool):
    r = client.post("/outage", json={"on": True}, headers=AS_ASHA)
    assert r.status_code == 200
    on = r.json()
    assert on["active"] is True and on["error_rate"] == 0.3 and on["by"] == "asha"
    assert on["erp_file"].endswith("_outage.csv") and on["erp_rows"] == 41
    assert ("PUT", "/admin/faults") in sim.calls and ("POST", "/admin/erp-malformed") in sim.calls
    assert sim.faults["*"]["error_rate"] == 0.3
    # switching on again does not drop a second file
    n = len(sim.calls)
    assert client.post("/outage", json={"on": True}, headers=AS_ASHA).json()["active"] is True
    assert ("POST", "/admin/erp-malformed") not in sim.calls[n:]
    assert client.get("/outage", headers=AS_MEERA).json()["active"] is True   # everyone sees the state
    off = client.post("/outage", json={"on": False}, headers=AS_ASHA).json()
    assert off["active"] is False and sim.faults == {}
    with pool.connection() as c:
        rows = c.execute("SELECT actor, detail FROM audit_log WHERE action = 'sim.outage' ORDER BY id DESC LIMIT 2").fetchall()
    assert [r["detail"]["on"] for r in rows] == [False, True]
    assert all(r["actor"] == "asha" for r in rows) and "feeds" in rows[1]["detail"]


def test_outage_reports_a_simulator_that_is_down(client, sim):
    sim.up = False
    assert client.post("/outage", json={"on": True}, headers=AS_ASHA).status_code == 503
    st = client.get("/outage", headers=AS_ASHA).json()
    assert st["active"] is False and st["simulator_up"] is False


def test_malformed_export_only_holds_broken_rows(tmp_path):
    world = World(load_settings().network_dir, 20261113)
    path, rows = write_malformed_export(world, SCENARIO_END, tmp_path, rows=40, seed=1)
    fields, parsed = erp.parse_export(path.read_text(encoding="utf-8-sig"))
    assert "shipment_no" in fields                      # the header maps, so the file is not rejected whole
    assert rows == 41 and len(parsed) == 41
    assert all(d is None for _, d, _ in parsed)          # every data line is malformed: nothing can change a shipment
    again, _ = write_malformed_export(world, SCENARIO_END, tmp_path / "b", rows=40, seed=2)
    assert again.read_text(encoding="utf-8") != path.read_text(encoding="utf-8")


def test_simulator_drops_the_malformed_file_with_its_admin_key(tmp_path):
    from fastapi.testclient import TestClient

    from sim.carrier_api import create_app
    from tower.clock import FixedClock

    s = load_settings(data_dir=tmp_path, carrier_admin_key="ka", carrier_api_key="k")
    app = create_app(s, FixedClock(SCENARIO_END), World(load_settings().network_dir, 20261113))
    with TestClient(app) as c:
        assert c.post("/admin/erp-malformed", json={"rows": 5}).status_code == 401
        r = c.post("/admin/erp-malformed", json={"rows": 5}, headers={"X-Admin-Key": "ka"})
    assert r.status_code == 200 and r.json()["rows"] == 6
    assert (s.inbox / r.json()["file"]).exists()


# ------------------------------------------------------------------ dead letters with a bearer token
def world_now():
    from tests.world import NOW
    return NOW


def _dead_letter(pool, payload: dict) -> int:
    with pool.connection() as c:
        add_dead_letter(c, "erp", world_now(), payload, RowError("malformed_row", "broken", "Supply a corrected payload"))
        return c.execute("SELECT max(id) AS id FROM dead_letter").fetchone()["id"]


def test_dead_letter_actions_with_a_bearer_token(client, pool):
    with pool.connection() as c:   # the audit log survives resets, and dead-letter ids restart: only look at new rows
        before = c.execute("SELECT COALESCE(max(id), 0) AS n FROM audit_log").fetchone()["n"]
    a = _dead_letter(pool, {"raw_line": "SH-1;broken"})
    b = _dead_letter(pool, {"raw_line": "SH-2;broken"})
    assert client.post(f"/dead-letters/{a}/discard").status_code == 401
    assert client.post(f"/dead-letters/{a}/discard", headers=AS_KIRAN).status_code == 403
    assert client.post(f"/dead-letters/{a}/replay", headers=AS_MEERA).status_code == 403
    r = client.post(f"/dead-letters/{a}/replay", headers=AS_ASHA)
    assert r.status_code == 422 and r.json()["reason_code"] == "cannot_replay"     # a cut-off line has no fields
    assert client.post(f"/dead-letters/{a}/discard", headers=AS_ASHA).json()["ok"] is True
    assert client.post(f"/dead-letters/{a}/discard", headers=AS_ASHA).status_code == 409
    assert client.post(f"/dead-letters/{b}/discard", headers={"X-Admin-Key": "test-admin"}).status_code == 200   # scripts still work
    with pool.connection() as c:
        rows = c.execute("SELECT actor, action, target FROM audit_log WHERE id > %s AND target IN (%s, %s) ORDER BY id",
                         (before, f"dead_letter:{a}", f"dead_letter:{b}")).fetchall()
    assert [(r["actor"], r["action"]) for r in rows] == [("asha", "dead_letter.replay_failed"), ("asha", "dead_letter.discard"),
                                                        ("admin", "dead_letter.discard")]


# ------------------------------------------------------------------ the current scenario morning (audit and feed times)
def _audit(pool, action, target, at, detail=None, actor="system", dc_ids=None):
    from tower.store import audit
    with pool.connection() as c:
        audit(c, actor, action, target, detail or {}, at=at, dc_ids=dc_ids)
        return c.execute("SELECT max(id) AS id FROM audit_log").fetchone()["id"]


def test_audit_morning_keeps_the_current_run_only_and_nothing_after_the_clock(client, pool):
    from datetime import timedelta

    from tests.conftest import NOW
    old = _audit(pool, "sim.outage", "old-run", NOW - timedelta(hours=1), {"on": True}, actor="asha")
    marker = _audit(pool, "scenario.start", "scenario", NOW - timedelta(hours=3))
    early = _audit(pool, "erp.file_processed", "morning-early", NOW - timedelta(hours=2), {"rows": 1})
    late = _audit(pool, "erp.file_processed", "morning-late", NOW - timedelta(minutes=5), {"rows": 1})
    kol = _audit(pool, "playbook.execute", "playbook:kol", NOW - timedelta(minutes=10), {"action_type": "update_eta"}, actor="asha", dc_ids=["DC-KOL"])
    future = _audit(pool, "erp.file_processed", "morning-future", NOW + timedelta(minutes=50), {"rows": 1})

    body = client.get("/audit", params={"morning": "true", "limit": 500}, headers=AS_ASHA).json()
    ids = [r["id"] for r in body["items"]]
    assert body["morning_from"] == marker
    assert old not in ids and future not in ids
    assert {early, late, kol} <= set(ids)
    assert ids.index(late) < ids.index(kol) < ids.index(early)            # newest first by time
    ats = [r["at"] for r in body["items"]]
    assert ats == sorted(ats, reverse=True)
    # the role rules still hold inside the morning
    kiran = [r["id"] for r in client.get("/audit", params={"morning": "true", "limit": 500}, headers=AS_KIRAN).json()["items"]]
    assert kol not in kiran
    # without the flag the full append-only log is still there
    assert old in [r["id"] for r in client.get("/audit", params={"limit": 500}, headers=AS_ASHA).json()["items"]]


def test_feed_last_at_is_never_after_the_scenario_clock(client, pool):
    from datetime import timedelta

    from tests.conftest import NOW
    _audit(pool, "scenario.start", "scenario", NOW - timedelta(hours=3))
    read = NOW - timedelta(minutes=20)
    _audit(pool, "erp.file_processed", "erp_export_x.csv", read, {"rows": 3})
    _audit(pool, "erp.file_processed", "erp_export_y.csv", NOW + timedelta(minutes=51), {"rows": 3})
    run_sql(pool, "INSERT INTO feed_health (feed, received, accepted, last_at) VALUES ('erp', 3, 3, %s), ('gps', 1, 1, %s) "
                  "ON CONFLICT (feed) DO UPDATE SET last_at = EXCLUDED.last_at", (NOW + timedelta(minutes=51), NOW + timedelta(minutes=5)))
    feeds = {f["feed"]: f for f in client.get("/health/feeds", headers=AS_ASHA).json()["feeds"]}
    from datetime import datetime
    assert datetime.fromisoformat(feeds["erp"]["last_at"]) == read
    assert feeds["gps"]["last_at"] is None                                 # no audited read to fall back on


# ------------------------------------------------------------------ contract PDFs for the clause citations
def test_contract_pdf_follows_the_clause_read_rule(client, settings, world):
    settings.contracts_dir.mkdir(parents=True, exist_ok=True)
    (settings.contracts_dir / "MSA_kirana-network.pdf").write_bytes(b"%PDF-1.4 test\n")
    (settings.contracts_dir / "MSA_spar.pdf").write_bytes(b"%PDF-1.4 test\n")
    ok = client.get("/contracts/Kirana network/pdf", headers=AS_ASHA)
    assert ok.status_code == 200 and ok.headers["content-type"] == "application/pdf" and ok.content.startswith(b"%PDF")
    assert client.get("/contracts/Kirana network/pdf", headers=AS_KIRAN).status_code == 200      # a chain he ships to
    assert client.get("/contracts/Spar/pdf", headers=AS_KIRAN).status_code == 403                # a chain he does not
    assert client.get("/contracts/Spar/pdf", headers=AS_MEERA).status_code == 200
    assert client.get("/contracts/Nobody/pdf", headers=AS_ASHA).status_code == 404
    for bad in ("..%2F..%2F.env", "..", "%2E%2E%2Fsecret"):
        assert client.get(f"/contracts/{bad}/pdf", headers=AS_ASHA).status_code == 404
    assert client.get("/contracts/Kirana network/pdf").status_code == 401


def test_brief_links_unverified_rows_to_their_dead_letter_for_vp_operations_only(client, pool, world):
    with pool.connection() as c:
        add_dead_letter(c, "contracts", world_now(), {"chain": "Kirana network", "clause_id": "MSA-7.2"},
                        RowError("param_mismatch", "Kirana network MSA-7.2: the PDF differs from the contract master"))
        dl = c.execute("SELECT max(id) AS id FROM dead_letter").fetchone()["id"]
        c.execute("UPDATE incident_impact SET params_unverified = true, clauses = ARRAY['MSA-7.2'] WHERE incident_id = %s "
                  "AND shipment_id = %s", (world["geo"], world["ships"]["H1"]))
    asha = client.get(f"/incidents/{world['geo']}/brief", headers=AS_ASHA).json()["money"]
    assert {"chain": "Kirana network", "clause_id": "MSA-7.2", "dead_letter_id": dl} in asha["unverified_sources"]
    kiran = client.get(f"/incidents/{world['geo']}/brief", headers=AS_KIRAN).json()["money"]
    assert kiran["unverified_sources"] and all(s["dead_letter_id"] is None for s in kiran["unverified_sources"])
