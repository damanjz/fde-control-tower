"""The triage run's own rules, driven by a scripted model instead of Ollama: the reads that must come before a proposal,
the single nudge, the single retry, the call cap, the fallback, and what a run records. Needs Postgres, not the GPU."""

from __future__ import annotations

import json

import pytest

from tests.world import NOW
from tower import agent, llm
from tower.access import USERS
from tower.clock import FixedClock
from tower.llm import ChatResult, ToolCall
from tower.scoped import ScopedStore

pytestmark = pytest.mark.db
KIRANA = "Kirana network"
INVENTED = " This puts Rs 9,99,999 at risk."          # a rupee figure no tool printed


class Scripted:
    """Stands in for the model: answers each chat call with the next step (a ChatResult, or an exception to raise).
    Keeps the messages it was shown, so a test can read what the loop said back."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []

    def __call__(self, settings, messages, tools=None) -> ChatResult:
        self.seen.append(list(messages))
        if not self.steps:
            raise AssertionError("the loop asked the model for more answers than the script holds")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def call(name: str, **args) -> ChatResult:
    return ChatResult("", [ToolCall(name, args)], prompt_tokens=100, completion_tokens=10)


def reads() -> list[ChatResult]:
    """The six reads a good run makes before it proposes."""
    return [call("get_incident"), call("find_affected"), call("compute_exposure"),
            call("get_clause", clause_id="MSA-7.2", chain=KIRANA), call("get_clause", clause_id="MSA-14.3", chain=KIRANA),
            call("list_options")]


def summaries(out: dict) -> list[str]:
    return [s["result_summary"] for s in out["run"]["steps"]]


@pytest.fixture()
def triage(pool, settings, world):
    """Triage the weather incident as the VP Operations with the given model."""
    def go(script, **kw):
        st = ScopedStore(pool, USERS["asha"], NOW, None)
        return agent.triage(pool, settings, FixedClock(NOW), USERS["asha"], st, world["geo"], chat=script, **kw)
    return go


@pytest.fixture()
def proposal(pool, world):
    """A truthful propose_playbooks call for the weather incident: real clause excerpts and a figure the tools print."""
    st = ScopedStore(pool, USERS["asha"], NOW, None)
    late, fm = st.clause(KIRANA, "MSA-7.2")["text"], st.clause(KIRANA, "MSA-14.3")["text"]
    gross = agent.fmt_inr(st.exposure_breakdown(world["geo"])["gross_inr"])

    def make(extra: str = "") -> ChatResult:
        return call("propose_playbooks",
                    summary=f"NH44 is closed and delays shipments; the late-delivery exposure is {gross} under MSA-7.2.{extra}",
                    playbooks=[
                        {"action_type": "notify_customers", "params": {}, "clause_id": "MSA-14.3", "clause_chain": KIRANA,
                         "clause_quote": " ".join(fm.split()[:9]), "rationale": "Notify inside the force-majeure window."},
                        {"action_type": "update_eta", "params": {}, "clause_id": "MSA-7.2", "clause_chain": KIRANA,
                         "clause_quote": " ".join(late.split()[4:14]), "rationale": "Tell customers the new time."}])
    return make


# ------------------------------------------------------------------ a run that goes well
def test_a_good_run_is_accepted_recorded_and_says_what_it_fetched(triage, proposal):
    out = triage(Scripted(*reads(), proposal()))
    run = out["run"]
    assert (run["outcome"], run["fallback_reason"], run["honesty_failures"]) == ("ok", None, [])
    assert (run["llm_calls"], run["prompt_tokens"], run["completion_tokens"]) == (7, 700, 70)
    assert [s["tool"] for s in run["steps"]] == ["get_incident", "find_affected", "compute_exposure", "get_clause",
                                                 "get_clause", "list_options", "propose_playbooks"]
    assert summaries(out)[-1] == "accepted"
    assert out["fetched_clauses"] == ["MSA-14.3", "MSA-7.2"] and out["cached"] is False
    assert [(p["action_type"], p["source"], p["status"]) for p in out["playbooks"]] == [
        ("notify_customers", "agent", "proposed"), ("update_eta", "agent", "proposed")]


def test_a_new_run_retires_the_earlier_proposals_and_a_cached_read_skips_the_model(triage, proposal, pool):
    first = triage(Scripted(*reads(), proposal()))
    second = triage(Scripted(*reads(), proposal()))
    with pool.connection() as c:
        states = sorted({(r["run_id"], r["status"]) for r in c.execute("SELECT run_id, status FROM playbook").fetchall()})
    assert states == [(first["run"]["id"], "rejected"), (second["run"]["id"], "proposed")]
    never = Scripted()
    cached = triage(never, use_cache=True)
    assert cached["cached"] is True and cached["run"]["id"] == second["run"]["id"] and cached["fetched_clauses"] is None
    assert never.seen == []


# ------------------------------------------------------------------ the order of work
def test_a_proposal_before_the_required_reads_is_refused_and_does_not_use_up_the_retry(triage, proposal):
    out = triage(Scripted(proposal(), *reads(), proposal(INVENTED), proposal()))
    assert summaries(out)[0] == "rejected: call get_incident, find_affected, compute_exposure first"
    run = out["run"]
    assert run["outcome"] == "ok" and len(run["honesty_failures"]) == 1      # only the invented figure counts as a failure


def test_a_model_that_only_talks_is_nudged_once_then_the_run_falls_back(triage):
    script = Scripted(ChatResult("The road is closed."), ChatResult("It really is closed."))
    out = triage(script)
    assert script.seen[1][-1] == {"role": "user", "content": "Use the tools. Finish by calling propose_playbooks."}
    run = out["run"]
    assert (run["outcome"], run["llm_calls"]) == ("fallback", 2)
    assert run["fallback_reason"] == "the model answered without calling propose_playbooks"
    assert run["summary"].startswith("Agent fallback") and out["playbooks"]
    assert {p["source"] for p in out["playbooks"]} == {"fallback"}


def test_the_call_cap_ends_a_run_that_never_proposes(triage):
    script = Scripted(*[call("get_incident") for _ in range(agent.MAX_LLM_CALLS)])
    run = triage(script)["run"]
    assert (run["outcome"], run["fallback_reason"], run["llm_calls"]) == ("fallback", "no proposal", agent.MAX_LLM_CALLS)
    assert script.steps == [] and len(run["steps"]) == agent.MAX_LLM_CALLS


# ------------------------------------------------------------------ honesty failures and the retry
def test_a_rejected_proposal_is_handed_back_with_its_reasons_and_gets_one_retry(triage, proposal):
    script = Scripted(*reads(), proposal(INVENTED), proposal())
    run = triage(script)["run"]
    assert (run["outcome"], run["llm_calls"]) == ("ok", 8)
    assert len(run["honesty_failures"]) == 1 and "rupee figure not from any tool output: 999,999" in run["honesty_failures"][0]
    back = json.loads(script.seen[-1][-1]["content"])               # what the loop told the model before the retry
    assert back == {"rejected": run["honesty_failures"], "instruction": "Fix these and call propose_playbooks again."}


def test_a_second_rejected_proposal_ends_in_the_fallback_brief(triage, proposal):
    out = triage(Scripted(*reads(), proposal(INVENTED), proposal(INVENTED)))
    run = out["run"]
    assert (run["outcome"], run["fallback_reason"]) == ("fallback", "honesty checks failed twice")
    assert len(run["honesty_failures"]) == 2 and {p["source"] for p in out["playbooks"]} == {"fallback"}


# ------------------------------------------------------------------ things that go wrong around the model
def test_a_tool_that_crashes_is_reported_to_the_model_and_does_not_count_as_called(triage, proposal, monkeypatch):
    real, crashes = agent.TOOL_FUNCS["find_affected"], iter([True])

    def flaky(ctx, args):
        if next(crashes, False):
            raise RuntimeError("boom")
        return real(ctx, args)

    monkeypatch.setitem(agent.TOOL_FUNCS, "find_affected", flaky)
    out = triage(Scripted(call("get_incident"), call("find_affected"), call("compute_exposure"), proposal(),
                          call("find_affected"), *reads()[3:], proposal()))
    assert summaries(out)[1] == "error: find_affected failed"
    assert summaries(out)[3] == "rejected: call find_affected first"          # the crashed call was not recorded as made
    assert (out["run"]["outcome"], out["run"]["honesty_failures"]) == ("ok", [])


def test_another_incident_id_or_an_unknown_tool_comes_back_as_an_error(triage, proposal, world):
    out = triage(Scripted(call("get_incident", incident_id=world["fac"]), call("drop_tables"), *reads(), proposal()))
    assert summaries(out)[:2] == [f"error: you are triaging incident {world['geo']}", "error: unknown tool drop_tables"]
    assert out["run"]["outcome"] == "ok"


def test_an_unreachable_model_gives_the_fallback_brief(triage):
    out = triage(Scripted(llm.LLMError("chat request failed: ConnectError")))
    run = out["run"]
    assert (run["outcome"], run["llm_calls"], run["steps"]) == ("fallback", 0, [])
    assert run["fallback_reason"] == "model unavailable: chat request failed: ConnectError"
    assert 1 <= len(out["playbooks"]) <= 4 and out["fetched_clauses"] == []


def test_with_no_model_passed_in_the_run_asks_the_ollama_client(pool, settings, world, monkeypatch):
    script = Scripted(llm.LLMError("down"))
    monkeypatch.setattr(llm, "chat", script)
    st = ScopedStore(pool, USERS["asha"], NOW, None)
    out = agent.triage(pool, settings, FixedClock(NOW), USERS["asha"], st, world["geo"])
    assert len(script.seen) == 1 and out["run"]["fallback_reason"] == "model unavailable: down"
