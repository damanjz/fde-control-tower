"""Action engine: approval gating at the threshold, scope and role checks, idempotent execution, audit and outbox rows."""

from __future__ import annotations

import pytest

from tests.conftest import AS_ASHA, AS_KIRAN, AS_MEERA
from tests.world import NOW, customer, run_sql
from tower import actions
from tower.access import USERS
from tower.playbooks import Planner
from tower.scoped import ScopedStore

pytestmark = pytest.mark.db


def make_playbook(pool, world, action: str, params: dict | None = None, *, incident: str = "geo", user: str = "asha") -> int:
    """Build a playbook with the planner (as the given user) and store it, as the agent would."""
    st = ScopedStore(pool, USERS[user], NOW, None)
    ev = st.incident(world[incident])
    cand = Planner(pool, st, ev, NOW).build(action, params or {})
    with pool.connection() as conn:
        return actions.save_playbook(conn, incident_id=ev["id"], run_id=None, user=USERS[user], cand=cand, clause_id="MSA-7.2",
                                     clause_chain="Kirana network", clause_quote="x", rationale="test", source="agent")


@pytest.fixture()
def road(world, pool):
    """A narrow road closure on the HYD to Nagpur lane. The shared world's weather event is 3000 km wide, so no re-route can
    leave it; here the DC-MUM to Nagpur lane passes 137 km away, outside the 80 km radius, so a re-route there escapes."""
    from tests.world import incident
    from tower import risk
    with pool.connection() as c:
        world["road"] = incident(c, "road", "weather", "NH44 closed near Adilabad",
                                 "Heavy rain closed the road. Reopening estimate 26 hours.", lat=19.67, lon=78.53, radius_km=80)
    risk.recompute_all(pool, NOW)
    return world


def audit_mark(conn) -> int:
    """audit_log is append-only and survives the per-test reset, so tests read only what they wrote."""
    return conn.execute("SELECT COALESCE(max(id), 0) AS n FROM audit_log").fetchone()["n"]


def pb_row(conn, pid):
    return conn.execute("SELECT * FROM playbook WHERE id = %s", (pid,)).fetchone()


def test_goods_at_the_threshold_runs_and_one_rupee_above_waits_for_approval(client, road, pool, conn):
    world = road
    a = make_playbook(pool, world, "reroute_via_dc", {"to_dc": "DC-MUM"}, incident="road")
    assert float(pb_row(conn, a)["goods_value_inr"]) == 1_000_000           # the three HYD shipments: above the threshold
    for goods, expected in ((500_000, "executed"), (500_001, "pending_approval")):
        pid = make_playbook(pool, world, "notify_customers")                  # goods 0; set the value under test
        run_sql(pool, "UPDATE playbook SET goods_value_inr = %s WHERE id = %s", (goods, pid))
        r = client.post(f"/playbooks/{pid}/run", headers=AS_ASHA)
        assert r.status_code == 200 and r.json()["status"] == expected, (goods, r.json())
        if expected == "pending_approval":
            assert pb_row(conn, pid)["requested_by"] == "asha" and pb_row(conn, pid)["executed_at"] is None


def test_above_threshold_needs_asha_to_approve_and_then_executes_once(client, road, pool, conn):
    pid = make_playbook(pool, road, "reroute_via_dc", {"to_dc": "DC-MUM"}, incident="road", user="kiran")     # his three HYD shipments, Rs 10 lakh
    run = client.post(f"/playbooks/{pid}/run", headers=AS_KIRAN)               # kiran may act in his scope, but it is above Rs 5 lakh
    assert run.status_code == 200 and run.json()["status"] == "pending_approval"
    before = {r["id"]: r for r in conn.execute("SELECT id, dc_id, eta, version FROM shipment WHERE dc_id = 'DC-HYD'").fetchall()}
    assert client.post(f"/playbooks/{pid}/approve", headers=AS_KIRAN).status_code == 403
    assert client.post(f"/playbooks/{pid}/approve", headers=AS_MEERA).status_code == 403
    assert {r["dc_id"] for r in conn.execute("SELECT dc_id FROM shipment WHERE id LIKE 'SH-91%'").fetchall()} == {"DC-HYD"}
    ok = client.post(f"/playbooks/{pid}/approve", headers=AS_ASHA)
    assert ok.status_code == 200 and ok.json()["status"] == "executed" and ok.json()["approved_by"] == "asha"
    assert ok.json()["executed_by"] == "kiran"                                  # the requester executes; asha approved
    ships = conn.execute("SELECT id, dc_id, eta, version FROM shipment WHERE id LIKE 'SH-91%'").fetchall()
    assert {s["dc_id"] for s in ships} == {"DC-MUM"} and all(s["version"] == before[s["id"]]["version"] + 1 for s in ships)
    assert all(s["eta"] > before[s["id"]]["eta"] for s in ships)
    assert conn.execute("SELECT trucks_free FROM fleet_capacity WHERE dc_id = 'DC-MUM' AND carrier_id = 'C1'").fetchone()["trucks_free"] == 8
    res = ok.json()["result"]
    assert res["changed"] == 3 and res["exposure_before_inr"] > 0
    # the second approve and a second run return the first result and change nothing
    again = client.post(f"/playbooks/{pid}/approve", headers=AS_ASHA).json()
    run2 = client.post(f"/playbooks/{pid}/run", headers=AS_KIRAN).json()
    assert again["replayed"] is True and run2["replayed"] is True and again["result"] == res == run2["result"]
    assert again["idempotency_key"] == f"playbook:{pid}:exec"
    assert {s["version"] for s in conn.execute("SELECT version FROM shipment WHERE id LIKE 'SH-91%'").fetchall()} == {2}
    assert conn.execute("SELECT trucks_free FROM fleet_capacity WHERE dc_id = 'DC-MUM' AND carrier_id = 'C1'").fetchone()["trucks_free"] == 8


def test_execution_writes_audit_outbox_and_before_after(client, road, pool, conn):
    mark = audit_mark(conn)
    pid = make_playbook(pool, road, "reroute_via_dc", {"to_dc": "DC-MUM"}, incident="road", user="kiran")
    client.post(f"/playbooks/{pid}/run", headers=AS_ASHA)
    client.post(f"/playbooks/{pid}/approve", headers=AS_ASHA)
    rows = conn.execute("SELECT actor, action, target, detail, dc_ids FROM audit_log WHERE id > %s AND target = %s ORDER BY id",
                        (mark, f"playbook:{pid}")).fetchall()
    assert [r["action"] for r in rows] == ["playbook.request_approval", "playbook.approve", "playbook.execute"]
    ex = rows[-1]
    assert ex["actor"] == "asha" and ex["dc_ids"] == ["DC-HYD"] and ex["detail"]["approved_by"] == "asha"
    first = ex["detail"]["before_after"][0]
    assert first["dc_id"] == ["DC-HYD", "DC-MUM"] and first["version"] == [1, 2] and first["eta"][0] != first["eta"][1]
    ob = conn.execute("SELECT topic, status, payload FROM outbox WHERE playbook_id = %s", (pid,)).fetchall()
    assert len(ob) == 1 and ob[0]["topic"] == "chat_webhook" and ob[0]["status"] == "pending"
    assert ob[0]["payload"]["kind"] == "playbook_executed" and ob[0]["payload"]["playbook_id"] == pid
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE id > %s AND action = 'playbook.execute' AND target = %s",
                        (mark, f"playbook:{pid}")).fetchone()["n"] == 1


def test_notification_is_recorded_with_a_timestamp_and_waives_the_penalty(client, world, pool, conn, clock):
    pid = make_playbook(pool, world, "notify_customers", user="kiran")
    pb = pb_row(conn, pid)
    assert float(pb["est_saving_inr"]) > 0                                      # the waiver has a rupee value
    before = float(client.get("/incidents", headers=AS_ASHA).json()["items"][0]["net_inr"])
    r = client.post(f"/playbooks/{pid}/run", headers=AS_KIRAN)                  # all three shipments are in DC-HYD
    assert r.status_code == 200 and r.json()["status"] == "executed"
    n = conn.execute("SELECT * FROM customer_notification WHERE incident_id = %s", (world["geo"],)).fetchall()
    assert len(n) == 1 and n[0]["customer_id"] == customer(conn, "Kirana network", "Nagpur")
    assert n[0]["notified_at"] == clock.now()       # the recorded time is the scenario clock, the MSA-14.3 evidence
    # 08:00 is inside the 6 hours from the 05:00 event start, so the HYD shipments' penalties are now waived
    rows = conn.execute("SELECT waived, net_penalty_inr FROM incident_impact WHERE incident_id = %s AND shipment_id LIKE 'SH-91%%'",
                        (world["geo"],)).fetchall()
    assert all(x["waived"] and x["net_penalty_inr"] == 0 for x in rows)
    top = {i["id"]: i for i in client.get("/incidents", headers=AS_ASHA).json()["items"]}[world["geo"]]
    assert float(top["net_inr"]) < before and float(top["waived_inr"]) > 0
    assert conn.execute("SELECT count(*) AS n FROM outbox WHERE playbook_id = %s", (pid,)).fetchone()["n"] == 2   # summary + customer


def test_late_notification_has_no_saving(world, pool, conn):
    """After the 6 hour window a notification can no longer waive anything: the planner prices its saving at zero."""
    from datetime import datetime

    from tower.clock import IST
    pid = make_playbook(pool, world, "notify_customers")
    late = datetime(2026, 11, 13, 11, 30, tzinfo=IST)
    st = ScopedStore(pool, USERS["asha"], late, None)
    cand = Planner(pool, st, st.incident(world["geo"]), late).build("notify_customers")
    assert cand.saving == 0 and pb_row(conn, pid)["est_saving_inr"] > 0


def test_finance_and_out_of_scope_users_cannot_execute(client, world, pool, conn):
    own = make_playbook(pool, world, "update_eta")
    assert client.post(f"/playbooks/{own}/run", headers=AS_MEERA).status_code == 403
    kol = make_playbook(pool, world, "update_eta", incident="fac")
    assert client.post(f"/playbooks/{kol}/run", headers=AS_KIRAN).status_code == 404       # invisible to him
    assert client.get(f"/playbooks/{kol}", headers=AS_KIRAN).status_code == 404
    assert pb_row(conn, kol)["status"] == "proposed"
    mixed = make_playbook(pool, world, "update_eta")                                       # geo incident, asha: HYD, KOL and DEL
    assert set(pb_row(conn, mixed)["dc_scope"]) == {"DC-HYD", "DC-KOL", "DC-DEL"}
    assert client.post(f"/playbooks/{mixed}/run", headers=AS_KIRAN).status_code == 404
    assert client.post(f"/playbooks/{mixed}/run", headers=AS_ASHA).json()["status"] == "executed"


def test_state_drift_aborts_the_whole_execution(client, road, pool, conn):
    mark = audit_mark(conn)
    pid = make_playbook(pool, road, "reroute_via_dc", {"to_dc": "DC-MUM"}, incident="road")
    run_sql(pool, "UPDATE playbook SET goods_value_inr = 1000 WHERE id = %s", (pid,))
    run_sql(pool, "UPDATE shipment SET dc_id = 'DC-DEL' WHERE id = 'SH-9103'")                 # someone moved it after the plan
    r = client.post(f"/playbooks/{pid}/run", headers=AS_ASHA)
    assert r.status_code == 409
    assert {x["dc_id"] for x in conn.execute("SELECT dc_id FROM shipment WHERE id IN ('SH-9101', 'SH-9102')").fetchall()} == {"DC-HYD"}
    assert pb_row(conn, pid)["status"] == "proposed"
    assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE id > %s AND action = 'playbook.execute'",
                        (mark,)).fetchone()["n"] == 0


def test_a_reroute_that_cannot_leave_the_weather_event_is_not_offered(world, pool):
    """Every lane into Nagpur passes through the 3000 km event: a re-route would add detour days on top of the storm."""
    from tower.playbooks import PlaybookError
    st = ScopedStore(pool, USERS["asha"], NOW, None)
    with pytest.raises(PlaybookError, match="still pass through the event"):
        Planner(pool, st, st.incident(world["geo"]), NOW).build("reroute_via_dc", {"to_dc": "DC-MUM"})


def test_an_executed_reroute_lowers_the_exposure_it_promised_to_lower(client, road, pool, conn):
    """Regression (2026-11-13): an approved re-route raised exposure because the planner assumed it escaped the event."""
    pid = make_playbook(pool, road, "reroute_via_dc", {"to_dc": "DC-MUM"}, incident="road")
    assert float(pb_row(conn, pid)["est_saving_inr"]) > 0
    client.post(f"/playbooks/{pid}/run", headers=AS_ASHA)
    res = client.post(f"/playbooks/{pid}/approve", headers=AS_ASHA).json()["result"]
    assert res["exposure_after_inr"] < res["exposure_before_inr"]


def test_stock_transfer_moves_stock_and_cuts_the_fill_rate_penalty(client, world, pool, conn):
    from tests.world import incident, ship
    from tower import risk
    sku = conn.execute("SELECT sku FROM product WHERE supplier_id = 'S036' ORDER BY sku LIMIT 1").fetchone()["sku"]
    run_sql(pool, "UPDATE dc_stock SET cases = 12 WHERE dc_id = 'DC-MUM' AND sku = %s", (sku,))
    run_sql(pool, "UPDATE dc_stock SET cases = 300 WHERE dc_id = 'DC-HYD' AND sku = %s", (sku,))
    with pool.connection() as c:
        ship(c, "T-SUP", dc="DC-MUM", city="Pune", lines=[(sku, 40, 400_000)], value=400_000)
        iid = incident(c, "sup", "supplier", "Supplier late", "5 days late", supplier_id="S036")
    risk.recompute_all(pool, NOW)
    world["sup"] = iid
    pid = make_playbook(pool, world, "stock_transfer", {"from_dc": "DC-HYD"}, incident="sup")
    pb = pb_row(conn, pid)
    assert float(pb["est_saving_inr"]) == 8000.0 and pb["params"]["transfers"] == [
        {"from_dc": "DC-HYD", "to_dc": "DC-MUM", "sku": sku, "cases": 16}]                  # 40 cases x 40% short = 16
    r = client.post(f"/playbooks/{pid}/run", headers=AS_ASHA).json()
    assert r["status"] == "executed" and r["result"]["exposure_before_inr"] == 8000.0
    assert r["result"]["exposure_after_inr"] == 0
    assert conn.execute("SELECT cases FROM dc_stock WHERE dc_id = 'DC-HYD' AND sku = %s", (sku,)).fetchone()["cases"] == 284
    assert conn.execute("SELECT cases FROM dc_stock WHERE dc_id = 'DC-MUM' AND sku = %s", (sku,)).fetchone()["cases"] == 28
    assert conn.execute("SELECT count(*) AS n FROM stock_transfer WHERE playbook_id = %s", (pid,)).fetchone()["n"] == 1


def test_options_never_include_a_move_that_costs_more_than_it_saves(road, pool):
    """Regression (2026-11-14): the agent proposed a Rs 1.72 lakh re-route that removed only Rs 86,811 of penalty."""
    from decimal import Decimal

    from tower.playbooks import Candidate, pays_for_itself
    loser = Candidate("reroute_via_dc", "t", {}, [], [], Decimal(0), cost=Decimal(172164), saving=Decimal("86811.17"))
    assert not pays_for_itself(loser)
    assert pays_for_itself(Candidate("notify_customers", "t", {}, [], [], Decimal(0), cost=Decimal(160), saving=Decimal(0)))
    st = ScopedStore(pool, USERS["asha"], NOW, None)
    for key in ("geo", "road", "fac"):
        alts = Planner(pool, st, st.incident(road[key]), NOW).alternatives()
        assert all(pays_for_itself(c) for c in alts), key
