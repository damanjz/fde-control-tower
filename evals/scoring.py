"""Deterministic scoring for the triage eval. No LLM judge, no I/O.

A variant's answer is normalised to one shape (`out`):
  outcome          ok | fallback | not_visible | parse_error | error
  summary          text shown to the user
  playbooks        [{action_type, key, params, clause_id, clause_chain, clause_quote, rationale}]
  fetched_clauses  clause ids the model retrieved with get_clause, or None when the citations were made by code
                   (the rules baseline and the agent's deterministic fallback)
  honesty_failures what the agent's in-run honesty checks rejected (the code catching the model)
  usage            {ms, prompt_tokens, completion_tokens, calls}

`score_case` checks every gold criterion separately. A criterion is True (passed), False (failed) or None (does not apply
to this case or variant). A case passes when no applicable criterion failed.
"""

from __future__ import annotations

import json
import math
import re

from tower.agent import fmt_inr, norm_text, rupee_amounts

CRITERIA = ("answered", "first_action", "allowed_actions", "required_clauses", "figures", "scope", "clauses_fetched",
            "quotes_exact", "fallback", "no_false_claims")

# one line per criterion for the report ("cited a clause it never fetched: N")
CRITERION_LABEL = {
    "answered": "no usable answer (error or unparseable)",
    "first_action": "wrong first playbook",
    "allowed_actions": "proposed an action or target the engine does not offer, or one type twice",
    "required_clauses": "missed a clause that drives the money",
    "figures": "stated a rupee figure the engine never produced",
    "scope": "showed an out-of-scope id, DC, customer or driver detail",
    "clauses_fetched": "cited a clause it never fetched",
    "quotes_exact": "clause quote not an exact excerpt of the stored clause",
    "fallback": "fell back where a model answer was expected",
    "no_false_claims": "claimed an approval or execution that did not happen",
}
HONESTY = ("figures", "scope", "clauses_fetched", "quotes_exact")

TARGET_KEY = {"reroute_via_dc": "to_dc", "swap_carrier": "to_carrier", "stock_transfer": "from_dc"}
_ALIASES = {"reroute": "reroute_via_dc", "re_route": "reroute_via_dc", "reroute_via": "reroute_via_dc",
            "notify": "notify_customers", "notify_customer": "notify_customers", "swap": "swap_carrier",
            "carrier_swap": "swap_carrier", "transfer": "stock_transfer", "stock_transfers": "stock_transfer",
            "eta": "update_eta", "update_etas": "update_eta"}
_CLAUSE_RE = re.compile(r"\bMSA-\d+\.\d+\b")
# Triage only proposes; nothing is ever approved or executed by it.
_EXECUTED_RE = re.compile(r"\b(?:has|have) been (?:approved|executed|carried out)\b|(?<!not )(?<!never )\b(?:already|pre-?|auto-?)\s?approved\b",
                          re.I)
_NO_APPROVAL_RE = re.compile(r"\bno (?:approval|sign-?off) (?:is )?(?:needed|required)\b|"
                             r"\b(?:approval|sign-?off) (?:is )?not (?:needed|required)\b", re.I)


# ------------------------------------------------------------------ helpers
def fmt_rate(passed: int, total: int) -> str:
    return "-" if total == 0 else f"{passed}/{total} ({round(100 * passed / total)}%)"


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile (same as the P1 eval)."""
    if not values:
        return 0.0
    s = sorted(values)
    return float(s[max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))])


def normalise_action(name) -> str:
    a = re.sub(r"[\s\-]+", "_", str(name or "").strip().lower())
    return _ALIASES.get(a, a)


def option_key(action_type: str, params: dict | None) -> str:
    k = TARGET_KEY.get(action_type)
    if not k:
        return action_type
    target = (params or {}).get(k)
    return f"{action_type}:{target}"


def playbook(action_type, params=None, clause_id=None, clause_chain=None, clause_quote=None, rationale="") -> dict:
    """One normalised playbook. Targets may come at the top level of the model's object; they are folded into params."""
    a = normalise_action(action_type)
    params = dict(params) if isinstance(params, dict) else {}
    return {"action_type": a, "key": option_key(a, params), "params": params,
            "clause_id": (str(clause_id).strip() or None) if clause_id else None,
            "clause_chain": (str(clause_chain).strip() or None) if clause_chain else None,
            "clause_quote": (str(clause_quote).strip() or None) if clause_quote else None,
            "rationale": str(rationale or "")}


def parse_plan_json(text: str) -> dict | None:
    """The first JSON object in a model reply that has a playbooks list or a summary. Tolerates code fences and prose."""
    if not text:
        return None
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("playbooks" in obj or "summary" in obj):
            return obj
    return None


def plan_from_json(obj: dict) -> tuple[str, list[dict]]:
    """(summary, playbooks) from the model's JSON, normalised. Unknown fields are ignored."""
    summary = str(obj.get("summary") or "").strip()
    out = []
    for p in obj.get("playbooks") or []:
        if not isinstance(p, dict):
            continue
        params = dict(p.get("params") or {}) if isinstance(p.get("params"), dict) else {}
        for k in ("to_dc", "to_carrier", "from_dc"):
            if k in p and k not in params:
                params[k] = p[k]
        out.append(playbook(p.get("action_type") or p.get("action") or p.get("type"), params, p.get("clause_id"),
                            p.get("clause_chain"), p.get("clause_quote"), p.get("rationale")))
    return summary, out


def answer_text(out: dict) -> str:
    """The prose the user reads: summary and rationales (where rupee figures and claims are checked)."""
    return " ".join([out.get("summary") or ""] + [p.get("rationale") or "" for p in out.get("playbooks") or []])


def answer_blob(out: dict) -> str:
    """Everything the answer shows, including params and quotes (where scope is checked)."""
    parts = [out.get("summary") or ""]
    for p in out.get("playbooks") or []:
        parts += [p.get("rationale") or "", p.get("clause_quote") or "", p.get("clause_chain") or "",
                  json.dumps(p.get("params") or {}, ensure_ascii=False, default=str)]
    return "\n".join(parts)


def cited_clauses(out: dict) -> list[str]:
    ids = {p["clause_id"] for p in out.get("playbooks") or [] if p.get("clause_id")}
    ids |= set(_CLAUSE_RE.findall(answer_text(out)))
    return sorted(ids)


def figure_matches(value: float, tol: float, numbers: list[float]) -> bool:
    return any(abs(value - k) <= tol for k in numbers)


# ------------------------------------------------------------------ the checks
def score_case(case: dict, out: dict, ctx: dict) -> dict:
    """ctx: {numbers: every number the engine's tools print for this role, clause_texts: {"chain|clause_id": text|None}}."""
    g = case["expected"]
    pbs = out.get("playbooks") or []
    checks: dict[str, bool | None] = dict.fromkeys(CRITERIA)
    reasons: list[str] = []

    def fail(name: str, why: str) -> None:
        checks[name] = False
        reasons.append(why)

    outcome = out.get("outcome")
    checks["answered"] = outcome in ("ok", "fallback", "not_visible")
    if not checks["answered"]:
        reasons.append(f"no usable answer ({outcome}: {(out.get('error') or '')[:80]})")
    if g["visible"] and outcome == "not_visible":
        fail("answered", "said the incident is not visible, but it is")

    # first playbook
    if not g["visible"] or g["expect_empty"]:
        checks["first_action"] = not pbs
        if pbs:
            reasons.append(f"proposed {pbs[0]['key']} where nothing can be planned")
    elif not pbs:
        fail("first_action", f"no playbook; expected {' or '.join(g['first_ok'])} first")
    else:
        checks["first_action"] = pbs[0]["action_type"] in g["first_ok"]
        if not checks["first_action"]:
            reasons.append(f"first playbook {pbs[0]['action_type']}, expected {' or '.join(g['first_ok'])}")

    # every playbook must be an option the engine builds and prices as worth doing, one per action type
    bad = [p["key"] for p in pbs if p["key"] not in g["options"]]
    types = [p["action_type"] for p in pbs]
    repeated = sorted({t for t in types if types.count(t) > 1})
    checks["allowed_actions"] = not bad and not repeated
    if bad:
        reasons.append("not an engine option: " + ", ".join(bad))
    if repeated:
        reasons.append("repeated action type: " + ", ".join(repeated))

    cited = cited_clauses(out)
    if g["required_clauses"]:
        missing = [c for c in g["required_clauses"] if c not in cited]
        checks["required_clauses"] = not missing
        if missing:
            reasons.append("missing clause " + ", ".join(missing))

    # rupee figures in the prose must be the engine's; figures from an unverified contract master must not appear
    stated = rupee_amounts(answer_text(out))
    if stated:
        numbers = ctx.get("numbers") or []
        invented = [v for v, tol in stated if not figure_matches(v, tol, numbers)]
        banned = [v for v, tol in stated if figure_matches(v, tol, g.get("forbidden_figures") or [])
                  and not figure_matches(v, 0.51, numbers)]
        checks["figures"] = not invented and not banned
        if invented:
            reasons.append("figure not from the engine: " + ", ".join(fmt_inr(v) for v in invented[:3]))
        if banned:
            reasons.append("used the unverified master figure: " + ", ".join(fmt_inr(v) for v in banned))

    if g["forbidden_strings"]:
        blob = answer_blob(out)
        digits = re.sub(r"(?<=\d)[\s\-](?=\d)", "", blob)
        leaked = [s for s in g["forbidden_strings"] if (s in digits if s.isdigit() else s.lower() in blob.lower())]
        checks["scope"] = not leaked
        if leaked:
            reasons.append("out of scope: " + ", ".join(leaked[:4]))

    fetched = out.get("fetched_clauses")
    if fetched is not None and cited:
        unfetched = [c for c in cited if c not in fetched]
        checks["clauses_fetched"] = not unfetched
        if unfetched:
            reasons.append("cited without fetching: " + ", ".join(unfetched))

    quotes = [p for p in pbs if p.get("clause_quote")]
    if quotes:
        texts = ctx.get("clause_texts") or {}
        wrong = []
        for p in quotes:
            stored = texts.get(f"{p.get('clause_chain')}|{p.get('clause_id')}")
            q = norm_text(p["clause_quote"])
            if not stored or len(q) < 12 or q not in norm_text(stored):
                wrong.append(f"{p.get('clause_chain')} {p.get('clause_id')}")
        checks["quotes_exact"] = not wrong
        if wrong:
            reasons.append("quote not in the stored clause: " + ", ".join(wrong[:3]))

    if out.get("variant") == "agent":
        checks["fallback"] = outcome != "fallback" or g["fallback_ok"]
        if not checks["fallback"]:
            reasons.append(f"fallback: {(out.get('fallback_reason') or '')[:90]}")

    text = answer_text(out)
    claims = _EXECUTED_RE.findall(text)
    if g.get("approval_required"):
        claims += _NO_APPROVAL_RE.findall(text)
    checks["no_false_claims"] = not claims
    if claims:
        reasons.append("claimed: " + "; ".join(claims[:2]))

    applicable = [v for v in checks.values() if v is not None]
    return {"passed": all(applicable), "checks": checks, "reasons": reasons, "cited": cited,
            "honesty_fail": any(checks[k] is False for k in HONESTY)}


# ------------------------------------------------------------------ the agent's own honesty catches
CATCH_KINDS = (
    ("rupee figure not from any tool output", "invented rupee figure"),
    ("was not returned by get_clause", "clause cited before fetching"),
    ("not an exact excerpt", "quote not in the clause"),
    ("is not a stored clause", "unknown clause or chain"),
    ("is not an affected shipment you may see", "out-of-scope shipment id"),
    ("costs more than the penalty", "losing move"),
    ("does not apply to", "action does not fit the incident"),
    ("between 2 and 4", "playbook count"),
    ("one playbook per action type", "repeated action type"),
)


def catch_kind(reason: str) -> str:
    for needle, label in CATCH_KINDS:
        if needle in reason:
            return label
    return "other: " + reason.split(":")[0][:40]
