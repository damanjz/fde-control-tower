"""The honesty checks that run after the model answers: a fake clause, an invented rupee figure, an out-of-scope shipment
id or a repeated action is rejected with a message the model can act on. Also the number and quote helpers."""

from __future__ import annotations

import json

import pytest

from evals import scoring
from tests.world import NOW
from tower import agent
from tower.access import USERS
from tower.clock import FixedClock
from tower.scoped import ScopedStore

# ------------------------------------------------------------------ helpers (no database)


def test_rupee_figures_are_parsed_in_indian_and_lakh_forms():
    got = agent.rupee_amounts("Exposure Rs 1,73,622, about Rs 1.7 lakh, or INR 2.5 crore; also ₹5,00,000.")
    vals = sorted(round(v) for v, _ in got)
    assert vals == [170000, 173622, 500000, 25000000]
    assert agent.fmt_inr(1736222.4) == "Rs 17,36,222" and agent.fmt_inr(500000) == "Rs 5,00,000"


def test_numbers_in_collects_numeric_leaves_and_rupee_strings():
    nums = agent.numbers_in({"a": 12, "b": {"c": [3.5, "Rs 4,18,000"]}, "ok": True})
    assert {12.0, 3.5, 418000.0} <= set(nums) and 1.0 not in nums


# ------------------------------------------------------------------ with the database
@pytest.fixture()
def ctx(pool, settings, world):
    def make(user="asha", incident="geo"):
        st = ScopedStore(pool, USERS[user], NOW, None)
        c = agent.Ctx(pool, settings, FixedClock(NOW), USERS[user], st, st.incident(world[incident]), 1)
        for name, args in (("get_incident", {}), ("find_affected", {}), ("compute_exposure", {}),
                           ("get_clause", {"query": "late delivery penalty per day", "chain": "Kirana network"}),
                           ("get_clause", {"query": "force majeure weather notified within hours", "chain": "Kirana network"}),
                           ("list_options", {})):
            agent.run_tool(c, name, args)
        return c
    return make


def good_args(ctx):
    late = ctx.store.clause("Kirana network", "MSA-7.2")["text"]
    fm = ctx.store.clause("Kirana network", "MSA-14.3")["text"]
    gross = agent.fmt_inr(ctx.store.exposure_breakdown(ctx.ev["id"])["gross_inr"])
    return {
        "summary": f"NH44 is closed and delays shipments; the late-delivery exposure is {gross} under MSA-7.2.",
        "playbooks": [
            {"action_type": "notify_customers", "params": {}, "clause_id": "MSA-14.3", "clause_chain": "Kirana network",
             "clause_quote": " ".join(fm.split()[:9]), "rationale": "Notify inside the force-majeure window."},
            {"action_type": "update_eta", "params": {}, "clause_id": "MSA-7.2", "clause_chain": "Kirana network",
             "clause_quote": " ".join(late.split()[4:14]), "rationale": "Tell customers the new time."},
        ],
    }


@pytest.mark.db
def test_a_truthful_answer_passes_and_the_code_prices_the_playbooks(ctx):
    c = ctx()
    cands, infos, errors = agent.check_proposal(c, good_args(c))
    assert errors == [] and [x.action_type for x in cands] == ["notify_customers", "update_eta"]
    assert cands[0].saving > 0 and len(infos) == 2          # the saving comes from the exposure engine, not from the model


@pytest.mark.db
def test_a_quote_that_is_not_in_the_stored_clause_is_rejected(ctx):
    c = ctx()
    args = good_args(c)
    args["playbooks"][0]["clause_quote"] = "Penalties are waived for any delay whatsoever without notice"
    errors = agent.check_proposal(c, args)[2]
    assert any("not an exact excerpt" in e for e in errors)
    args = good_args(c)
    args["playbooks"][0]["clause_id"] = "MSA-77.7"
    assert any("not a stored clause" in e for e in agent.check_proposal(c, args)[2])
    args = good_args(c)
    args["playbooks"][1]["clause_chain"] = "Imaginary Mart"
    assert any("not a stored clause" in e for e in agent.check_proposal(c, args)[2])


@pytest.mark.db
def test_an_invented_rupee_figure_is_rejected(ctx):
    c = ctx()
    args = good_args(c)
    args["summary"] += " This puts Rs 9,99,999 at risk."
    errors = agent.check_proposal(c, args)[2]
    assert any("rupee figure not from any tool output: 999,999" in e for e in errors)
    args["summary"] = "Penalty exposure is Rs 1,73,000 or so."          # close to a real figure but not one the tools returned
    assert agent.check_proposal(c, args)[2]
    args["summary"] = "The approval threshold is Rs 5,00,000."          # returned by get_incident, so allowed
    assert agent.check_proposal(c, args)[2] == []


@pytest.mark.db
def test_a_clause_id_the_agent_never_retrieved_is_rejected(ctx):
    c = ctx()
    args = good_args(c)
    args["summary"] += " Clause MSA-12.1 limits liability."
    assert any("MSA-12.1 was not returned by get_clause" in e for e in agent.check_proposal(c, args)[2])


@pytest.mark.db
def test_out_of_scope_shipment_ids_are_rejected_for_the_dispatcher(ctx):
    c = ctx("kiran")
    args = good_args(c)
    args["summary"] += " SH-9201 is the biggest one."                      # a Kolkata shipment
    args["playbooks"][1]["params"] = {"shipment_ids": ["SH-9301"]}
    errors = agent.check_proposal(c, args)[2]
    assert any("SH-9201" in e for e in errors)
    assert any("SH-9301" in e for e in errors)
    ok = good_args(c)
    ok["summary"] += " SH-9101 is the biggest one."                        # in scope
    assert agent.check_proposal(c, ok)[2] == []


@pytest.mark.db
def test_playbook_count_duplicates_and_wrong_kind_are_rejected(ctx):
    c = ctx()
    one = good_args(c)
    one["playbooks"] = one["playbooks"][:1]
    count = [e for e in agent.check_proposal(c, one)[2] if "between 2 and 4" in e]
    assert count and scoring.catch_kind(count[0]) == "playbook count"      # the eval report files the real message correctly
    dup = good_args(c)
    dup["playbooks"][1] = dict(dup["playbooks"][0])
    assert any("one playbook per action type" in e for e in agent.check_proposal(c, dup)[2])
    wrong = good_args(c)
    wrong["playbooks"][1]["action_type"] = "stock_transfer"
    wrong["playbooks"][1]["params"] = {"from_dc": "DC-HYD"}
    assert any("does not apply to a weather incident" in e for e in agent.check_proposal(c, wrong)[2])


@pytest.mark.db
def test_a_tool_answers_for_the_incident_in_its_context_and_returns_plain_json(ctx):
    """A tool ignores an incident id the model passes and answers for its own run. The loop's rules (an early proposal,
    a crashing tool) are tested with a scripted model in test_agent_loop_db.py."""
    c = ctx()
    assert agent.TOOL_FUNCS["get_incident"](c, {"incident_id": 999})["incident_id"] == c.ev["id"]   # the context id wins
    json.dumps(agent.tool_list_options(c, {}))


def test_get_clause_by_id_is_an_exact_lookup_not_a_search(ctx):
    """Regression (eval run 2): searching for "MSA-14.3" often ranked other clauses first; an id now fetches that clause."""
    c = ctx()
    for raw in ("MSA-14.3", "14.3", "msa-14.3"):
        out = agent.tool_get_clause(c, {"clause_id": raw, "chain": "Kirana network"})
        assert [x["clause_id"] for x in out["clauses"]] == ["MSA-14.3"], raw
    assert ("Kirana network", "MSA-14.3") in c.clauses_seen


def test_compute_exposure_names_only_the_clauses_that_price_the_incident(ctx):
    """Regression (eval run 1): the output named MSA-9.1 and MSA-14.3 even at zero, and the model cited them."""
    weather = agent.tool_compute_exposure(ctx("asha", "geo"), {})
    assert weather["clauses_that_price_this"] == ["MSA-7.2", "MSA-14.3"] and "fill_rate_penalty_MSA_9_1" not in weather
    facility = agent.tool_compute_exposure(ctx("asha", "fac"), {})
    assert facility["clauses_that_price_this"] == ["MSA-7.2"]
    assert "already_waived_MSA_14_3" not in facility and "fill_rate_penalty_MSA_9_1" not in facility


def test_list_options_shows_one_option_per_kind_the_best_priced(ctx):
    """Regression (eval runs 3-6): two swaps to different carriers made the model break the one-per-kind rule."""
    c = ctx()
    kinds = [o["action_type"] for o in agent.tool_list_options(c, {})["options"]]
    assert len(kinds) == len(set(kinds))
    alts = c.get_planner().alternatives()
    for o in agent.tool_list_options(c, {})["options"]:
        same = [a for a in alts if a.action_type == o["action_type"]]
        assert o["est_saving"] == agent._money_pair(max(same, key=lambda a: a.saving - a.cost).saving)
