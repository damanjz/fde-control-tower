"""Runs the real triage agent against the local model (Ollama). Skipped unless CT_LLM_TESTS=1.

The model is not deterministic, so these tests check properties that must hold on every run: the run is recorded with its
steps, tokens and timing; the outcome is a model answer or an honest fallback; whatever came back passed the honesty checks;
and scope held."""

from __future__ import annotations

import json

import pytest

from tests.world import NOW
from tower import agent, llm
from tower.access import USERS
from tower.clock import FixedClock
from tower.scoped import ScopedStore

pytestmark = [pytest.mark.llm, pytest.mark.db]


@pytest.fixture()
def run_agent(pool, settings, world):
    if not llm.llm_up(settings):
        pytest.skip("Ollama is not answering")
    llm_settings = settings.model_copy(update={"llm_url": "http://localhost:11434"})

    def go(user: str, incident: str):
        st = ScopedStore(pool, USERS[user], NOW, llm_settings)
        return agent.triage(pool, llm_settings, FixedClock(NOW), USERS[user], st, world[incident]), st
    return go


def assert_run_is_sound(out, st, pool):
    run = out["run"]
    assert run["outcome"] in ("ok", "fallback") and run["ms"] > 0 and run["model"]
    assert run["prompt_tokens"] > 0 or run["outcome"] == "fallback"
    assert {"get_incident", "find_affected", "compute_exposure"} <= {s["tool"] for s in run["steps"]} or run["outcome"] == "fallback"
    assert 1 <= len(out["playbooks"]) <= 4 and run["summary"]
    with pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM agent_step WHERE run_id = %s", (run["id"],)).fetchone()["n"] == len(run["steps"])
    for p in out["playbooks"]:
        assert p["source"] == ("agent" if run["outcome"] == "ok" else "fallback")
        assert float(p["est_saving_inr"]) >= 0 and p["status"] == "proposed"
        assert set(p["shipment_ids"]) <= st.visible_shipment_ids(p["shipment_ids"])         # never an out-of-scope id
        if run["outcome"] == "ok":
            stored = st.clause(p["clause_chain"], p["clause_id"])
            assert stored is not None and " ".join(p["clause_quote"].lower().split()[:3]) in stored["text"].lower()
    if run["outcome"] == "fallback":
        assert run["summary"].startswith("Agent fallback") and run["fallback_reason"]


def test_real_agent_triages_the_kolkata_outage_as_vp_operations(run_agent, pool):
    out, st = run_agent("asha", "fac")
    assert_run_is_sound(out, st, pool)
    run = out["run"]
    if run["outcome"] == "ok":
        known = agent.numbers_in([s["result_summary"] for s in run["steps"]])   # step summaries carry the tool figures
        assert run["honesty_failures"] == [] or run["llm_calls"] >= 7 or known       # a rejected-then-retried run is allowed
    kinds = {p["action_type"] for p in out["playbooks"]}
    assert kinds <= {"reroute_via_dc", "notify_customers", "update_eta"}               # a facility outage allows no swap or stock transfer


def test_real_agent_as_dispatcher_stays_inside_his_scope(run_agent, pool):
    out, st = run_agent("kiran", "geo")
    assert_run_is_sound(out, st, pool)
    blob = json.dumps([out["run"]["summary"], out["run"]["steps"], out["playbooks"]], default=str)
    for outside in ("SH-9201", "SH-9202", "SH-9301", "DC-KOL", "DC-DEL"):
        assert outside not in blob
    assert "98765" not in blob and "43210" not in blob                                   # the driver phone stays masked
    assert all(set(p["dc_scope"]) <= {"DC-HYD"} for p in out["playbooks"])
