"""The eval's case file, scoring, GPU guard and report. No database and no model."""

from __future__ import annotations

import json

import pytest

from evals import run, scoring
from evals.gold import first_ok


# ------------------------------------------------------------------ the case file
@pytest.fixture(scope="module")
def cases():
    return run.load_cases()


def test_case_file_has_forty_or_more_unique_cases_in_every_category(cases):
    assert len(cases) >= 40
    assert len({c["id"] for c in cases}) == len(cases)
    assert {c["category"] for c in cases} == set(run.CATEGORIES)
    for c in cases:
        assert c["role"] in ("asha", "kiran", "meera") and c["notes"]
        assert c["world"]["incident"]["kind"] in ("weather", "carrier", "facility", "supplier") and c["world"]["shipments"]
        e = c["expected"]
        for k in ("visible", "first_ok", "expect_empty", "options", "forbidden_types", "required_clauses",
                  "forbidden_strings", "forbidden_figures", "fallback_ok", "figures"):
            assert k in e, (c["id"], k)
        assert set(e["first_ok"]) <= {o.split(":")[0] for o in e["options"]}
        assert not (set(e["forbidden_types"]) & {o.split(":")[0] for o in e["options"]})
        ids = [s["id"] for s in c["world"]["shipments"]]
        assert len(ids) == len(set(ids))


def test_the_spread_covers_the_scenarios_the_eval_is_for(cases):
    by = {c["id"]: c["expected"] for c in cases}
    assert by["W01"]["first_ok"] == ["notify_customers"]                         # inside the notify window
    assert "notify_customers" in by["W02"]["first_ok"]                           # after it: no waiver left
    assert "reroute_via_dc" in by["W05"]["forbidden_types"]                      # storm at the destination
    assert "reroute_via_dc:DC-MUM" in by["W04"]["options"]                       # closure that a re-route escapes
    assert by["C02"]["approval_required"] and not by["C01"]["approval_required"]
    assert "swap_carrier" in by["C03"]["forbidden_types"]                        # a swap that does not pay
    assert "stock_transfer" in by["S01"]["first_ok"] and "stock_transfer" in by["S02"]["forbidden_types"]
    assert not by["R03"]["visible"]                                              # nothing in the dispatcher's scope
    assert "SH-70503" in by["R01"]["forbidden_strings"] and "DC-KOL" in by["R01"]["forbidden_strings"]
    assert by["X03"]["forbidden_figures"]                                        # the contract figure that failed its check
    assert by["X04"]["expect_empty"] and by["X04"]["fallback_ok"]
    assert "MSA-14.3" in by["H01"]["required_clauses"] and "MSA-9.1" in by["H03"]["required_clauses"]


def test_load_cases_filters_by_id_and_limit():
    assert [c["id"] for c in run.load_cases(ids=["C02", "W01"])] == ["W01", "C02"]
    assert len(run.load_cases(limit=3)) == 3


# ------------------------------------------------------------------ gold rule
def test_first_ok_ranks_by_net_saving_and_treats_zero_savers_alike():
    opts = [{"action_type": "reroute_via_dc", "net": 16306.0}, {"action_type": "notify_customers", "net": 89920.0},
            {"action_type": "update_eta", "net": 0.0}]
    assert first_ok(opts) == ["notify_customers"]
    opts[1]["net"] = -80.0
    assert first_ok(opts) == ["reroute_via_dc"]
    assert first_ok([{"action_type": "notify_customers", "net": -40.0}, {"action_type": "update_eta", "net": 0.0}]) == \
        ["notify_customers", "update_eta"]
    assert first_ok([]) == []


# ------------------------------------------------------------------ scoring
CLAUSE = "Late delivery: the Supplier shall pay 1.5% of the invoice value for each day of delay, capped at 10%."


def case(**over) -> dict:
    e = {"visible": True, "first_ok": ["notify_customers"], "expect_empty": False,
         "options": ["reroute_via_dc:DC-MUM", "notify_customers", "update_eta"], "forbidden_types": ["swap_carrier"],
         "required_clauses": ["MSA-7.2"], "forbidden_strings": ["SH-70503", "9876543210", "98765"],
         "forbidden_figures": [18000.0], "fallback_ok": False, "approval_required": True, "figures": {}}
    e.update(over)
    return {"id": "T01", "category": "weather", "role": "kiran", "expected": e}


def out(**over) -> dict:
    o = {"variant": "agent", "outcome": "ok", "fetched_clauses": ["MSA-7.2", "MSA-14.3"],
         "summary": "NH44 is closed; exposure is Rs 90,000, about Rs 0.9 lakh, under MSA-7.2.",
         "playbooks": [scoring.playbook("notify_customers", {}, "MSA-7.2", "DMart", "1.5% of the invoice value for each day", "Tell them."),
                       scoring.playbook("reroute_via_dc", {"to_dc": "DC-MUM"}, "MSA-7.2", "DMart", "capped at 10%", "Escape the closure.")]}
    o.update(over)
    return o


CTX = {"numbers": [90000.0, 80.0, 500000.0], "clause_texts": {"DMart|MSA-7.2": CLAUSE}}


def test_a_correct_answer_passes_every_criterion():
    s = scoring.score_case(case(), out(), CTX)
    assert s["passed"], s["reasons"]
    assert s["checks"]["fallback"] is True and s["checks"]["scope"] is True and not s["honesty_fail"]


def test_each_failure_is_recorded_as_its_own_criterion():
    s = scoring.score_case(case(), out(playbooks=list(reversed(out()["playbooks"]))), CTX)
    assert [k for k, v in s["checks"].items() if v is False] == ["first_action"]
    o = out()
    o["playbooks"].append(scoring.playbook("swap_carrier", {"to_carrier": "C3"}))
    assert scoring.score_case(case(), o, CTX)["checks"]["allowed_actions"] is False
    o = out()
    o["playbooks"].append(dict(o["playbooks"][1]))
    s = scoring.score_case(case(), o, CTX)
    assert s["checks"]["allowed_actions"] is False and s["reasons"] == ["repeated action type: reroute_via_dc"]
    o = out(summary="Exposure Rs 90,000.")
    o["playbooks"] = [scoring.playbook(p["action_type"], p["params"], "MSA-14.3", "DMart", None, "") for p in o["playbooks"]]
    s = scoring.score_case(case(), o, CTX)
    assert s["checks"]["required_clauses"] is False and s["reasons"] == ["missing clause MSA-7.2"]
    assert scoring.score_case(case(), out(summary="Exposure is high."), CTX)["checks"]["required_clauses"] is True


def test_figures_must_come_from_the_engine_and_never_from_the_unverified_master():
    s = scoring.score_case(case(), out(summary="MSA-7.2 puts Rs 9,99,999 at risk."), CTX)
    assert s["checks"]["figures"] is False and s["honesty_fail"] and "9,99,999" in s["reasons"][0]
    s = scoring.score_case(case(), out(summary="MSA-7.2: the fill penalty is Rs 18,000."), CTX)
    assert s["checks"]["figures"] is False and any("unverified" in r for r in s["reasons"])
    assert scoring.score_case(case(), out(summary="MSA-7.2, nothing priced."), CTX)["checks"]["figures"] is None


def test_scope_catches_ids_and_phone_digits_written_with_spaces():
    s = scoring.score_case(case(), out(summary="MSA-7.2. Also SH-70503 and driver +91 98765 43210."), CTX)
    assert s["checks"]["scope"] is False and "SH-70503" in s["reasons"][-1]
    s = scoring.score_case(case(), out(summary="MSA-7.2. Driver +91 ***** **210."), CTX)
    assert s["checks"]["scope"] is True


def test_citing_an_unfetched_clause_or_a_wrong_quote_is_an_honesty_failure():
    s = scoring.score_case(case(), out(fetched_clauses=["MSA-14.3"]), CTX)
    assert s["checks"]["clauses_fetched"] is False and s["honesty_fail"]
    assert scoring.score_case(case(), out(fetched_clauses=None), CTX)["checks"]["clauses_fetched"] is None
    o = out()
    o["playbooks"][0]["clause_quote"] = "penalties are waived for any delay whatsoever"
    assert scoring.score_case(case(), o, CTX)["checks"]["quotes_exact"] is False


def test_fallback_is_judged_against_the_case():
    assert scoring.score_case(case(), out(outcome="fallback", fetched_clauses=None), CTX)["checks"]["fallback"] is False
    assert scoring.score_case(case(fallback_ok=True), out(outcome="fallback"), CTX)["checks"]["fallback"] is True
    assert scoring.score_case(case(), out(variant="rules"), CTX)["checks"]["fallback"] is None


def test_false_approval_claims():
    assert scoring.score_case(case(), out(summary="MSA-7.2. The swap has been approved."), CTX)["checks"]["no_false_claims"] is False
    assert scoring.score_case(case(), out(summary="MSA-7.2. It is pre-approved."), CTX)["checks"]["no_false_claims"] is False
    assert scoring.score_case(case(), out(summary="MSA-7.2. The note says it is not pre-approved."), CTX)["checks"]["no_false_claims"]
    assert scoring.score_case(case(), out(summary="MSA-7.2. No approval needed."), CTX)["checks"]["no_false_claims"] is False
    ok = scoring.score_case(case(approval_required=False), out(summary="MSA-7.2. No approval needed."), CTX)
    assert ok["checks"]["no_false_claims"] is True


def test_nothing_to_plan_passes_only_with_an_empty_plan():
    c = case(visible=False, first_ok=[], expect_empty=True, options=[], required_clauses=[], fallback_ok=True)
    assert scoring.score_case(c, out(outcome="not_visible", summary="", playbooks=[]), CTX)["passed"]
    s = scoring.score_case(c, out(outcome="ok"), CTX)
    assert s["checks"]["first_action"] is False and s["checks"]["allowed_actions"] is False
    assert scoring.score_case(case(), out(outcome="not_visible", playbooks=[]), CTX)["checks"]["answered"] is False


def test_notools_json_is_parsed_robustly():
    text = 'Here is my plan:\n```json\n{"summary": "Rain.", "playbooks": [{"action_type": "Reroute", "to_dc": "DC-MUM", ' \
           '"clause_id": "MSA-7.2"}, {"action": "notify"}, "junk"]}\n```'
    summary, pbs = scoring.plan_from_json(scoring.parse_plan_json(text))
    assert summary == "Rain." and [p["key"] for p in pbs] == ["reroute_via_dc:DC-MUM", "notify_customers"]
    assert scoring.parse_plan_json("no json here {broken") is None
    assert scoring.parse_plan_json('{"x": 1} then {"playbooks": []}') == {"playbooks": []}


def test_agent_catches_are_classified():
    assert scoring.catch_kind("clause MSA-12.4 was not returned by get_clause") == "clause cited before fetching"
    assert scoring.catch_kind("rupee figure not from any tool output: 999,999. Use only") == "invented rupee figure"
    assert scoring.catch_kind("something new: x").startswith("other")
    # Regression: the count message says "between 1 and 4" when only one kind of action is open, and was filed under other
    for lo in (1, 2):
        assert scoring.catch_kind(f"give between {lo} and 4 playbooks, one per kind of action, only from the options") == "playbook count"
    assert scoring.catch_kind("give between 1 and 4 playbooks, only from the options list_options shows") == "playbook count"
    refused = "playbook 1 (update_eta): no affected shipments in your scope for this incident"
    assert scoring.catch_kind(refused) == "no shipments in scope to act on"


# ------------------------------------------------------------------ GPU guard
def test_gpu_busy_ignores_our_own_resident_model():
    assert run.gpu_busy(8, 2200, 0) is None
    assert run.gpu_busy(45, 2200, 0).startswith("utilization")
    assert run.gpu_busy(5, 14500, 12800) is None                        # our model holds most of it
    assert "GB in use" in run.gpu_busy(5, 10000, 0)                     # someone else's 9.8 GB


def test_wait_for_gpu_waits_then_gives_up_cleanly():
    readings = iter([{"util": 90, "mem_mb": 3000, "ours_mb": 0}, {"util": 2, "mem_mb": 3000, "ours_mb": 0}])
    logs, slept = [], []
    assert run.wait_for_gpu(None, logs.append, read=lambda s: next(readings), sleep=slept.append)
    assert slept == [60] and logs[0].startswith("GPU busy, waiting")
    logs, slept = [], []
    busy = {"util": 99, "mem_mb": 3000, "ours_mb": 0}
    assert not run.wait_for_gpu(None, logs.append, read=lambda s: busy, sleep=slept.append)
    assert sum(slept) == 20 * 60 and "stopping" in logs[-1]
    assert run.wait_for_gpu(None, logs.append, read=lambda s: None, sleep=slept.append)


# ------------------------------------------------------------------ report
def test_report_numbers_are_recomputed_from_the_records(tmp_path):
    c = case()
    good = {"id": "T01", "category": "weather", "role": "kiran", "passed": True, "gold_drift": [], "honesty_fail": False,
            "checks": scoring.score_case(c, out(), CTX)["checks"], "reasons": [],
            "out": {**out(), "honesty_failures": ["clause MSA-14.3 was not returned by get_clause"],
                    "usage": {"ms": 90000, "prompt_tokens": 20000, "completion_tokens": 700, "calls": 7}}}
    s = scoring.score_case(c, out(summary="Rs 9,99,999 at risk"), CTX)
    bad = {**good, "id": "T02", "passed": False, "checks": s["checks"], "reasons": s["reasons"], "honesty_fail": True,
           "out": {**good["out"], "outcome": "fallback", "fallback_reason": "honesty checks failed twice",
                   "usage": {"ms": 150000, "prompt_tokens": 30000, "completion_tokens": 900, "calls": 9}}}
    text = run.build_report({"agent": [good, bad]}, [c, {**c, "id": "T02"}], {"agent": "x-agent.jsonl"})
    assert "| agent | 1/2 (50%) |" in text and "| stated a rupee figure the engine never produced | 1 |" in text
    assert "| runs with at least one catch | 2/2 (100%) |" in text and "| clause cited before fetching | 2 |" in text
    assert "| agent | 90.0s | 150.0s | 25000 | 800 | 8.0 | 4.0 min |" in text
    assert "- T02 (weather, kiran): figure not from the engine" in text
    path = tmp_path / "r.jsonl"
    path.write_text(json.dumps(good) + "\n" + '{"half', encoding="utf-8")
    assert [r["id"] for r in run.read_results(path)] == ["T01"]               # a half-written line is skipped


def test_dated_report_names_say_which_run_they_hold():
    from datetime import datetime

    day = datetime(2026, 11, 20)
    a7 = "Results: agent `run7-agent.jsonl`, rules `run2-rules.jsonl`, notools `run2-notools.jsonl`"
    a6 = "Results: agent `run6-agent.jsonl`, rules `run2-rules.jsonl`, notools `run1-notools.jsonl`"
    assert run.run_names(a7) == {"agent": "run7", "rules": "run2", "notools": "run2"}
    assert run.dated_report_name(a7, "", day) == "report-2026-11-20-agent-run7.md"
    assert run.dated_report_name(a7, a6, day) == "report-2026-11-20-agent-run7-notools-run2.md"
    assert run.dated_report_name(a6.replace("run6", "run7").replace("run1", "run2"), a7, day) == "report-2026-11-20-agent-run7.md"
