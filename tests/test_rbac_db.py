"""Role-based access enforced at retrieval: the dispatcher cannot see a DC-KOL shipment through any endpoint or tool,
masked fields are masked in API output and in what the agent receives, and finance cannot execute."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.conftest import AS_ASHA, AS_KIRAN, AS_MEERA
from tests.world import NOW
from tower import agent
from tower.access import USERS, mask_bank, mask_phone
from tower.clock import FixedClock
from tower.scoped import ScopedStore

pytestmark = pytest.mark.db


def ctx_for(pool, settings, user, incident_id):
    st = ScopedStore(pool, USERS[user], NOW, settings)
    return agent.Ctx(pool, settings, FixedClock(NOW), USERS[user], st, st.incident(incident_id), 0)


# ------------------------------------------------------------------ authentication
@pytest.mark.parametrize("path", ["/me", "/incidents", "/shipments", "/shipments/SH-9101", "/network", "/disruptions", "/audit",
                                  "/dead-letters", "/health/feeds", "/incidents/1/brief"])
def test_every_read_endpoint_requires_a_token(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert client.get("/health").status_code == 200       # health stays open


def test_me_describes_the_role(client):
    me = client.get("/me", headers=AS_KIRAN).json()
    assert me["id"] == "kiran" and me["scope"] == {"dcs": ["DC-HYD"]} and me["can_approve"] is False
    assert client.get("/me", headers=AS_MEERA).json()["can_execute"] is False


# ------------------------------------------------------------------ row scope through every endpoint
def test_dispatcher_never_sees_a_kolkata_shipment(client, world):
    hyd = {world["ships"][k] for k in ("H1", "H2", "H3")}
    ship_list = client.get("/shipments", headers=AS_KIRAN).json()
    assert {s["id"] for s in ship_list["items"]} == hyd and ship_list["total"] == 3
    assert client.get("/shipments", params={"dc": "DC-KOL"}, headers=AS_KIRAN).json()["total"] == 0
    assert client.get("/shipments", params={"carrier": "C2"}, headers=AS_KIRAN).json()["total"] == 0
    assert client.get("/shipments/SH-9201", headers=AS_KIRAN).status_code == 404
    assert client.get("/shipments/SH-9301", headers=AS_KIRAN).status_code == 404
    assert client.get("/shipments/SH-9201", headers=AS_ASHA).status_code == 200
    # the incidents: the Kolkata outage is hidden, the weather event shows only his shipments
    inc = client.get("/incidents", headers=AS_KIRAN).json()
    assert [i["id"] for i in inc["items"]] == [world["geo"]] and inc["items"][0]["shipments"] == 3
    assert {i["id"] for i in client.get("/incidents", headers=AS_ASHA).json()["items"]} == {world["geo"], world["fac"]}
    assert [d["id"] for d in client.get("/disruptions", headers=AS_KIRAN).json()] == [world["geo"]]
    assert client.get(f"/incidents/{world['fac']}/brief", headers=AS_KIRAN).status_code == 404
    assert client.post(f"/incidents/{world['fac']}/triage", headers=AS_KIRAN).status_code == 404
    brief = client.get(f"/incidents/{world['geo']}/brief", headers=AS_KIRAN, params={"limit": 200}).json()
    assert {s["shipment_id"] for s in brief["shipments"]["items"]} == hyd and brief["shipments"]["total"] == 3
    assert [w["id"] for w in brief["chain"]["warehouses"]] == ["DC-HYD"]
    assert brief["incident"]["shipments"] == 3
    whole = json.dumps(brief)
    for outside in ("SH-9201", "SH-9202", "SH-9301", "DC-KOL", "DC-DEL"):
        assert outside not in whole
    full = client.get(f"/incidents/{world['geo']}/brief", headers=AS_ASHA).json()
    assert full["shipments"]["total"] == 6 and full["incident"]["gross_inr"] > brief["incident"]["gross_inr"]


def test_audit_trail_is_scoped_for_the_dispatcher(client, world, pool):
    with pool.connection() as c:
        c.execute("INSERT INTO audit_log (actor, action, target, dc_ids) VALUES ('asha', 'x.kol', 'a', %s)", (["DC-KOL"],))
        c.execute("INSERT INTO audit_log (actor, action, target, dc_ids) VALUES ('asha', 'x.hyd', 'b', %s)", (["DC-HYD"],))
        c.execute("INSERT INTO audit_log (actor, action, target, dc_ids) VALUES ('system', 'x.global', 'c', NULL)")
    k = {r["action"] for r in client.get("/audit", headers=AS_KIRAN, params={"limit": 500}).json()["items"]}
    assert "x.hyd" in k and "x.kol" not in k and "x.global" not in k
    a = {r["action"] for r in client.get("/audit", headers=AS_ASHA, params={"limit": 500}).json()["items"]}
    assert {"x.hyd", "x.kol", "x.global"} <= a


def test_dead_letters_are_for_vp_operations_only(client, world):
    assert client.get("/dead-letters", headers=AS_ASHA).status_code == 200
    assert client.get("/dead-letters", headers=AS_KIRAN).status_code == 403
    assert client.get("/dead-letters", headers=AS_MEERA).status_code == 403


# ------------------------------------------------------------------ field masking
def test_driver_phone_is_full_masked_or_removed_by_role(client, world):
    full = "+91 98765 43210"
    a = client.get("/shipments/SH-9101", headers=AS_ASHA).json()
    k = client.get("/shipments/SH-9101", headers=AS_KIRAN).json()
    m = client.get("/shipments/SH-9101", headers=AS_MEERA).json()
    assert a["driver_phone"] == full and a["driver_name"] == "Ravi Kumar"
    assert k["driver_phone"] == mask_phone(full) == "+91 ***** **210" and k["driver_name"] == "Ravi Kumar"
    assert "driver_phone" not in m and "driver_name" not in m
    for who, body in (("kiran", k), ("meera", m)):
        assert "98765" not in json.dumps(body), who
    assert "98765" not in json.dumps(client.get("/shipments", headers=AS_KIRAN).json())


def test_carrier_bank_and_dispatch_phone_by_role(client, world):
    def carriers(h):
        return {c["id"]: c for c in client.get("/network", headers=h).json()["carriers"]}
    a, k, m = carriers(AS_ASHA), carriers(AS_KIRAN), carriers(AS_MEERA)
    assert a["C1"]["bank_account"] == mask_bank("HDFC A/c 5010 0033 4471") == "HDFC A/c **** **** 4471"
    assert k["C1"]["bank_account"] == "HDFC A/c **** **** 4471"
    assert m["C1"]["bank_account"] == "HDFC A/c 5010 0033 4471"
    assert a["C1"]["dispatch_phone"] == "+91 98490 22713" and k["C1"]["dispatch_phone"] == "+91 ***** **713"
    assert "dispatch_phone" not in m["C1"]


def test_agent_receives_masked_fields_only(pool, settings, world):
    for user, expect in (("asha", "+91 98765 43210"), ("kiran", "+91 ***** **210")):
        text = json.dumps(agent.tool_find_affected(ctx_for(pool, settings, user, world["geo"]), {}))
        assert expect in text, user
        if user == "kiran":
            assert "98765" not in text
    meera = json.dumps(agent.tool_find_affected(ctx_for(pool, settings, "meera", world["geo"]), {}))
    assert "98765" not in meera and "driver_phone" not in meera and "Ravi" not in meera


# ------------------------------------------------------------------ the agent tools obey the same scope
def test_dispatcher_agent_tools_never_return_out_of_scope_data(pool, settings, world):
    ctx = ctx_for(pool, settings, "kiran", world["geo"])
    outs = [agent.tool_get_incident(ctx, {}), agent.tool_find_affected(ctx, {}), agent.tool_compute_exposure(ctx, {}),
            agent.tool_list_options(ctx, {}), agent.tool_get_clause(ctx, {"query": "late delivery penalty"})]
    text = json.dumps(outs)
    assert outs[1]["affected_count"] == 3 and outs[2]["shipments"] == 3
    for leak in ("SH-9201", "SH-9202", "SH-9301", "Patna", "Lucknow"):
        assert leak not in text
    assert {s["shipment_id"] for s in outs[1]["top_shipments_by_penalty"]} <= {"SH-9101", "SH-9102", "SH-9103"}
    # the hidden incident: every tool answers as if it did not exist
    hidden = ctx_for(pool, settings, "asha", world["fac"])
    hidden.store = ScopedStore(pool, USERS["kiran"], NOW, settings)
    for fn in (agent.tool_get_incident, agent.tool_find_affected):
        assert fn(hidden, {}) == {"error": "no such incident"}
    assert agent.tool_compute_exposure(hidden, {})["shipments"] == 0
    from tower.playbooks import Planner, PlaybookError
    with pytest.raises(PlaybookError):
        Planner(pool, hidden.store, hidden.ev, NOW).build("reroute_via_dc", {"to_dc": "DC-MUM"})


def test_contract_retrieval_is_limited_for_the_dispatcher(pool, world):
    admin = ScopedStore(pool, USERS["asha"], NOW, None)
    allowed = {r["chain"] for r in admin._q(
        "SELECT DISTINCT cu.chain FROM customer cu JOIN shipment s ON s.customer_id = cu.id WHERE s.dc_id = 'DC-HYD'")}
    everything = {c["chain"] for c in admin.search_clauses("late delivery penalty", k=60)}
    kiran = ScopedStore(pool, USERS["kiran"], NOW, None)
    seen = {h["chain"] for h in kiran.search_clauses("late delivery penalty", k=60)}
    assert seen and seen <= allowed
    outside = sorted(everything - allowed)
    assert outside, "the test needs a chain the dispatcher does not ship to"
    assert kiran.clause(outside[0], "MSA-7.2") is None and admin.clause(outside[0], "MSA-7.2") is not None


# ------------------------------------------------------------------ finance and approvals
def test_finance_cannot_execute_and_dispatcher_cannot_approve(client, world, pool):
    from tests.test_actions_db import make_playbook
    pb = make_playbook(pool, world, "notify_customers", incident="geo")
    assert client.post(f"/playbooks/{pb}/run", headers=AS_MEERA).status_code == 403
    assert client.post(f"/playbooks/{pb}/approve", headers=AS_KIRAN).status_code == 403
    assert client.post(f"/playbooks/{pb}/approve", headers=AS_MEERA).status_code == 403
    assert client.get(f"/playbooks/{pb}", headers=AS_MEERA).status_code == 200       # finance may read it


# ------------------------------------------------------------------ no read path bypasses the scoped layer
def test_no_endpoint_or_tool_queries_scoped_tables_directly():
    root = Path(__file__).resolve().parent.parent / "tower"
    guarded = ("shipment", "shipment_line", "incident_impact", "incident_exposure", "contract_chunk", "msa_document",
               "customer_notification", "dead_letter", "audit_log", "playbook", "agent_run")
    for name in ("routes.py", "agent.py", "playbooks.py"):
        src = (root / name).read_text(encoding="utf-8")
        for table in guarded:
            assert not re.search(rf"\b(FROM|JOIN)\s+{table}\b", src, re.I), f"{name} reads {table} directly"
    routes = (root / "routes.py").read_text(encoding="utf-8")
    assert ".execute(" not in routes and "psycopg" not in routes and "pool.connection" not in routes
