"""Triage agent: a local model with tools, honesty checks in code, one retry, then a deterministic fallback brief.

The model reads incidents through scoped tools, never does arithmetic (compute_exposure and list_options run the
deterministic engines) and finishes with one propose_playbooks call. After it answers, code checks that every clause
it quotes is a stored chunk, every rupee figure appeared in a tool output, and no shipment id is out of scope.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from tower import actions, llm
from tower.access import User
from tower.clock import IST, Clock
from tower.config import Settings
from tower.logging_ import log
from tower.playbooks import ACTION_TYPES, DEFAULT_CLAUSE, Candidate, Planner, PlaybookError, pays_for_itself
from tower.scoped import ScopedStore

logger = logging.getLogger("tower.agent")
MAX_LLM_CALLS = 11  # 7 tools in order, up to 2 exact clause fetches, one retry of the proposal
MAX_TOOL_JSON = 4200
REQUIRED_BEFORE_PROPOSAL = ("get_incident", "find_affected", "compute_exposure")


# ------------------------------------------------------------------ formatting and number checks
def indian_group(n: int) -> str:
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join([*parts, tail])
    return ("-" if n < 0 else "") + s


def fmt_inr(x) -> str:
    return f"Rs {indian_group(round(float(x)))}"


_RUPEE_RE = re.compile(r"(?:₹|\bRs\.?|\bINR)\s*([\d,]+(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?)?\b", re.I)
_MULT = {"cr": 1e7, "crore": 1e7, "crores": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5}


def rupee_amounts(text: str) -> list[tuple[float, float]]:
    """Every rupee figure in the text as (value in rupees, tolerance). Lakh and crore forms are rounded figures."""
    out = []
    for m in _RUPEE_RE.finditer(text or ""):
        num = m.group(1).replace(",", "").rstrip(".")
        mult = _MULT.get((m.group(2) or "").lower(), 1.0)
        decimals = len(num.split(".")[1]) if "." in num else 0
        out.append((float(num) * mult, 0.5 * 10 ** (-decimals) * mult if mult > 1 else 0.51))
    return out


def numbers_in(obj) -> list[float]:
    """Every numeric leaf of a JSON-like value, plus rupee figures inside strings."""
    out: list[float] = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, int | float):
        out.append(float(obj))
    elif isinstance(obj, str):
        out += [v for v, _ in rupee_amounts(obj)]
        out += [float(x.replace(",", "")) for x in re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", obj)]
    elif isinstance(obj, dict):
        for v in obj.values():
            out += numbers_in(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            out += numbers_in(v)
    return out


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9%.]+", " ", (s or "").lower()).strip()


# ------------------------------------------------------------------ the agent's context and tools
@dataclass
class Ctx:
    pool: object
    settings: Settings
    clock: Clock
    user: User
    store: ScopedStore
    ev: dict
    run_id: int
    outputs: list = field(default_factory=list)          # every tool result, for the rupee check
    called: list[str] = field(default_factory=list)
    clauses_seen: dict = field(default_factory=dict)     # (chain, clause_id) -> text returned by get_clause
    planner: Planner | None = None

    def get_planner(self) -> Planner:
        if self.planner is None:
            self.planner = Planner(self.pool, self.store, self.ev, self.clock.now())
        return self.planner


def _ist(dt: datetime | None) -> str | None:
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST") if dt else None


def _money_pair(x) -> dict:
    return {"inr": round(float(x)), "display": fmt_inr(x)}


def _scope_note(user: User) -> str:
    return "all India" if not user.scoped else "only shipments leaving " + ", ".join(user.dcs)


TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_incident", "description": "The incident: kind, title, detail, severity, start time, scope.",
        "parameters": {"type": "object", "properties": {"incident_id": {"type": "integer"}}, "required": ["incident_id"]}}},
    {"type": "function", "function": {
        "name": "find_affected",
        "description": "Shipments affected by the incident that you may see: counts, value, by warehouse, carrier, city, "
                       "customer, and the top shipments with their est. delay and penalty.",
        "parameters": {"type": "object", "properties": {"incident_id": {"type": "integer"}}, "required": ["incident_id"]}}},
    {"type": "function", "function": {
        "name": "compute_exposure",
        "description": "Money at risk from the contracts, computed by code: the penalties, what a notification can waive "
                       "and its deadline where that applies, and clauses_that_price_this, the only clauses to cite. Never "
                       "do this arithmetic yourself.",
        "parameters": {"type": "object", "properties": {"incident_id": {"type": "integer"}}, "required": ["incident_id"]}}},
    {"type": "function", "function": {
        "name": "get_clause",
        "description": "Get a contract clause. Give clause_id (e.g. 'MSA-7.2') to fetch that exact clause, or a query to "
                       "search. Returns the clause text with chain, clause id and page. Quote clauses only from here.",
        "parameters": {"type": "object", "properties": {
            "clause_id": {"type": "string", "description": "exact clause id from compute_exposure, e.g. 'MSA-7.2'"},
            "query": {"type": "string", "description": "what you are looking for, e.g. 'late delivery penalty per day'"},
            "chain": {"type": "string", "description": "retail chain name; omit to use the chain with the most exposure"},
            "incident_id": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "list_options",
        "description": "Buildable actions for this incident, priced by code: alternative DCs and carriers with spare trucks, "
                       "stock at other DCs, each with goods value, cost and estimated saving.",
        "parameters": {"type": "object", "properties": {"incident_id": {"type": "integer"}}, "required": ["incident_id"]}}},
    {"type": "function", "function": {
        "name": "propose_playbooks",
        "description": "Finish: a short summary and 2 to 4 playbooks (1 if list_options shows only one kind of action). "
                       "Do not put costs or savings in; code computes them. "
                       "Params by action: reroute_via_dc {to_dc}, swap_carrier {to_carrier}, stock_transfer {from_dc}, "
                       "notify_customers {}, update_eta {}.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "2 to 4 sentences. Rupee figures only as returned by the tools."},
            "playbooks": {"type": "array", "items": {"type": "object", "properties": {
                "action_type": {"type": "string", "enum": list(ACTION_TYPES)},
                "params": {"type": "object"},
                "clause_id": {"type": "string", "description": "e.g. MSA-7.2, as returned by get_clause"},
                "clause_chain": {"type": "string"},
                "clause_quote": {"type": "string", "description": "an exact excerpt from that clause's text"},
                "rationale": {"type": "string"}},
                "required": ["action_type", "params", "clause_id", "clause_quote", "rationale"]}}},
            "required": ["summary", "playbooks"]}}},
]


def tool_get_incident(ctx: Ctx, args: dict) -> dict:
    inc = ctx.store.incident(ctx.ev["id"])
    if inc is None:
        return {"error": "no such incident"}
    scope = {k: inc[k] for k in ("lat", "lon", "radius_km", "dc_id", "carrier_id", "states", "supplier_id") if inc[k] is not None}
    return {"incident_id": inc["id"], "kind": inc["kind"], "severity": inc["severity"], "title": inc["title"],
            "detail": inc["detail"], "started_at": _ist(inc["started_at"]), "scope": scope, "status": inc["status"],
            "affected_shipments_you_can_see": inc["shipments"], "you_see": _scope_note(ctx.user),
            "approval_threshold_inr": ctx.settings.approval_threshold_inr}


def tool_find_affected(ctx: Ctx, args: dict) -> dict:
    iid = ctx.ev["id"]
    inc = ctx.store.incident(iid)
    if inc is None:
        return {"error": "no such incident"}
    chain = ctx.store.chain_counts(iid)
    top = ctx.store.impact_rows(iid, limit=6)
    sample = []
    for r in top:
        item = {"shipment_id": r["shipment_id"], "city": r["zone_city"], "customer": r["customer"], "from_dc": r["dc_id"],
                "carrier": r["carrier_id"], "value": _money_pair(r["value_inr"]), "est_delay_days": float(r["est_delay_days"]),
                "days_late": r["days_late"], "penalty": _money_pair(r["gross_penalty_inr"])}
        if "driver_phone" in r and r["driver_phone"]:   # masked or full according to the role; absent for finance
            item["driver"] = r.get("driver_name")
            item["driver_phone"] = r["driver_phone"]
        sample.append(item)
    return {"affected_count": inc["shipments"], "total_value": _money_pair(inc["value_inr"]),
            "by_warehouse": [{"dc": x["id"], "shipments": x["shipments"]} for x in chain["warehouses"]],
            "by_carrier": [{"carrier": x["id"], "shipments": x["shipments"]} for x in chain["carriers"]],
            "top_cities": [{"city": x["label"], "shipments": x["shipments"]} for x in chain["cities"][:5]],
            "customers_affected": len(chain["customers"]),
            "top_shipments_by_penalty": sample,
            "reason_example": top[0]["reason"] if top else None}


def tool_compute_exposure(ctx: Ctx, args: dict) -> dict:
    b = ctx.store.exposure_breakdown(ctx.ev["id"])
    now = ctx.clock.now()
    deadline = b["notify_deadline"]
    weather = ctx.ev["kind"] == "weather"
    # Name only the clauses that price this incident. Before 2026-11-19 the output always carried keys naming the
    # fill-rate clause and the weather waiver, even at zero, and the model cited them in incidents they do not cover
    # (13 honesty catches in eval run 1).
    clauses = ["MSA-7.2"] + (["MSA-9.1"] if b["fill_inr"] or ctx.ev["kind"] == "supplier" else []) + (["MSA-14.3"] if weather else [])
    out = {
        "shipments": b["shipments"], "value": _money_pair(b["value_inr"]),
        "late_delivery_penalty_MSA_7_2": _money_pair(b["late_inr"]),
        "gross_penalty": _money_pair(b["gross_inr"]),
        "net_exposure": _money_pair(b["net_inr"]), "max_days_late": b["max_days_late"],
        "top_customers": [{"customer": c["customer"], "penalty": _money_pair(c["gross_inr"])} for c in b["top_customers"][:4]],
        "chains_by_penalty": [{"chain": c["chain"], "penalty": _money_pair(c["gross_inr"])} for c in b["by_chain"][:3]],
        "contract_params_unverified_rows": b["unverified_rows"],
        "clauses_that_price_this": clauses,
        "instruction": "Fetch each of these with get_clause before citing it. Cite no other clause.",
    }
    if "MSA-9.1" in clauses:
        out["fill_rate_penalty_MSA_9_1"] = _money_pair(b["fill_inr"])
    if weather:
        out["already_waived_MSA_14_3"] = _money_pair(b["waived_inr"])
        out["waivable_if_customers_notified_in_time"] = _money_pair(b["waivable_inr"])
        out["notify_deadline"] = _ist(deadline)
        if deadline:
            left = (deadline - now).total_seconds() / 3600
            out["hours_left_to_notify"] = round(left, 1) if left > 0 else "deadline passed"
        out["customers_already_notified"] = len(b["notifications"])
    return out


def tool_get_clause(ctx: Ctx, args: dict) -> dict:
    chain = args.get("chain")
    if not chain:
        by = ctx.store.exposure_breakdown(ctx.ev["id"])["by_chain"]
        chain = by[0]["chain"] if by else None
    cid = str(args.get("clause_id") or "").strip().upper().replace(" ", "")
    if cid and not cid.startswith("MSA-"):
        cid = f"MSA-{cid}"
    # an exact id is a lookup, not a search: searching for "MSA-14.3" often ranked other clauses first (eval run 2)
    exact = ctx.store.clause(chain, cid) if (cid and chain) else None
    if exact:
        hits = [exact]
    else:
        hits = ctx.store.search_clauses(str(args.get("query") or cid), chain=chain, k=3)
        if not hits and chain:
            hits = ctx.store.search_clauses(str(args.get("query") or cid), k=3)
    for h in hits:
        ctx.clauses_seen[(h["chain"], h["clause_id"])] = h["text"]
    return {"clauses": [{"chain": h["chain"], "clause_id": h["clause_id"], "heading": h["heading"], "page": h["page"],
                         "text": h["text"]} for h in hits],
            "note": "quote from 'text' exactly" if hits else "no clause found or you may not read contracts"}


def tool_list_options(ctx: Ctx, args: dict) -> dict:
    p = ctx.get_planner()
    options = []
    # one option per kind of action, the best by net saving: with a swap to each spare carrier listed, the model proposed
    # two swaps, broke the one-per-kind rule and sometimes fell back (eval runs 3-6, C02/R05). Which carrier or DC is a
    # money decision, so the engine makes it.
    best: dict[str, Candidate] = {}
    for c in p.alternatives():
        if c.action_type not in best or (c.saving - c.cost) > (best[c.action_type].saving - best[c.action_type].cost):
            best[c.action_type] = c
    for c in best.values():
        key = {k: v for k, v in c.params.items() if k in ("to_dc", "to_carrier", "from_dc")}
        options.append({"action_type": c.action_type, **key, "shipments": len(c.shipment_ids),
                        "goods_value": _money_pair(c.goods_value), "est_cost": _money_pair(c.cost),
                        "est_saving": _money_pair(c.saving), "notes": c.notes[:2]})
    trucks: dict[str, int] = {}
    for (dc, _), n in p.fleet.items():
        trucks[dc] = trucks.get(dc, 0) + n
    return {"options": options, "spare_trucks_by_dc": trucks}


TOOL_FUNCS = {"get_incident": tool_get_incident, "find_affected": tool_find_affected,
              "compute_exposure": tool_compute_exposure, "get_clause": tool_get_clause, "list_options": tool_list_options}


def summarize(name: str, res: dict) -> str:
    if "error" in res:
        return f"error: {res['error']}"
    if name == "get_incident":
        return f"{res['kind']} / {res['severity']}: {res['title']}; {res['affected_shipments_you_can_see']} shipments visible"
    if name == "find_affected":
        return f"{res['affected_count']} shipments, {res['total_value']['display']}, {len(res['by_warehouse'])} DC(s)"
    if name == "compute_exposure":
        return f"gross {res['gross_penalty']['display']}, net {res['net_exposure']['display']}"
    if name == "get_clause":
        return "; ".join(f"{c['chain']} {c['clause_id']} p{c['page']}" for c in res["clauses"]) or "no clause"
    if name == "list_options":
        return f"{len(res['options'])} options"
    return json.dumps(res)[:200]


# ------------------------------------------------------------------ honesty checks
def check_proposal(ctx: Ctx, args: dict) -> tuple[list[Candidate], list[dict], list[str]]:
    """Validate propose_playbooks. Returns (candidates, clause info per candidate, errors). No errors = accepted."""
    errors: list[str] = []
    summary = str(args.get("summary") or "").strip()
    pbs = args.get("playbooks")
    if not summary:
        errors.append("summary is empty")
    planner = ctx.get_planner()
    # at least 2 playbooks, unless the engine offers fewer action types: demanding 2 then made the model invent one
    # (eval run 1, X06)
    kinds = sorted({c.action_type for c in planner.alternatives()})
    lo = max(1, min(2, len(kinds)))
    if not isinstance(pbs, list) or not lo <= len(pbs) <= 4:
        # name the kinds still open: a bare "give 2 to 4" made the model repeat a kind it had already used (eval run 3, C02)
        used = {p.get("action_type") for p in pbs if isinstance(p, dict)} if isinstance(pbs, list) else set()
        left = [k for k in kinds if k not in used]
        hint = f"; add a different kind of action from: {', '.join(left)}" if left and len(pbs or []) < lo else ""
        errors.append(f"give between {lo} and 4 playbooks, one per kind of action, only from the options list_options shows{hint}")
        pbs = pbs if isinstance(pbs, list) else []
    texts = [summary] + [str(p.get("rationale") or "") for p in pbs if isinstance(p, dict)]
    known = numbers_in(ctx.outputs)
    for t in texts:   # every rupee figure must be one a tool returned
        for value, tol in rupee_amounts(t):
            if not any(abs(value - k) <= tol for k in known):
                errors.append(f"rupee figure not from any tool output: {value:,.0f}. Use only figures the tools returned")
    ids = set(re.findall(r"\bSH-\d+\b", " ".join(texts + [json.dumps(p.get("params", {})) for p in pbs if isinstance(p, dict)])))
    if ids:
        visible = ctx.store.visible_shipment_ids(sorted(ids))
        for sid in sorted(ids - visible):
            errors.append(f"shipment id {sid} is not an affected shipment you may see")
    pricing = next((o["clauses_that_price_this"] for o in ctx.outputs
                    if isinstance(o, dict) and "clauses_that_price_this" in o), None)
    for cid in sorted(set(re.findall(r"MSA-\d+\.\d+", " ".join(texts)))):
        if not any(k[1] == cid for k in ctx.clauses_seen):
            # say which clauses do apply, so the retry drops an inapplicable one instead of citing it again (run 3, H03/H04)
            fix = (f" The clauses that price this incident are {', '.join(pricing)}; fetch one with get_clause clause_id, "
                   f"or remove {cid}.") if pricing else ""
            errors.append(f"clause {cid} was not returned by get_clause.{fix}")
    cands: list[Candidate] = []
    infos: list[dict] = []
    seen = set()
    for i, p in enumerate(pbs, start=1):
        if not isinstance(p, dict):
            errors.append(f"playbook {i} is not an object")
            continue
        action, params = p.get("action_type"), p.get("params") if isinstance(p.get("params"), dict) else {}
        try:
            cand = planner.build(str(action), params)
        except PlaybookError as e:
            errors.append(f"playbook {i} ({action}): {e}")
            continue
        if action in seen:
            errors.append(f"playbook {i}: one playbook per action type; {action} is already proposed")
            continue
        if not pays_for_itself(cand):
            errors.append(f"playbook {i} ({action}): it costs more than the penalty it removes; propose only options list_options shows")
            continue
        seen.add(action)
        asked = set(params.get("shipment_ids") or [])
        if asked and not asked <= set(cand.shipment_ids):
            errors.append(f"playbook {i}: shipment_ids outside the affected set in your scope")
        cid, quote = str(p.get("clause_id") or ""), str(p.get("clause_quote") or "")
        chain = str(p.get("clause_chain") or "") or _primary_chain(ctx)
        stored = ctx.store.clause(chain, cid) if cid and chain else None
        if stored is None:
            errors.append(f"playbook {i}: clause {cid or '(none)'} for chain {chain or '(unknown)'} is not a stored clause you may read")
        elif len(norm_text(quote)) < 12 or norm_text(quote) not in norm_text(stored["text"]):
            errors.append(f"playbook {i}: clause_quote is not an exact excerpt of {chain} {cid}. Copy it from get_clause")
        cands.append(cand)
        infos.append({"clause_id": cid, "clause_chain": chain, "clause_quote": quote.strip(), "rationale": str(p.get("rationale") or "")})
    return cands, infos, errors


def _primary_chain(ctx: Ctx) -> str | None:
    by = ctx.store.exposure_breakdown(ctx.ev["id"])["by_chain"]
    return by[0]["chain"] if by else None


# ------------------------------------------------------------------ fallback brief
def _rule_clause(action: str, kind: str) -> str:
    """The clause a rule-chosen playbook relies on. The weather waiver (14.3) only applies to weather; a supplier
    shortfall is priced by the fill-rate clause (9.1), so its notification and ETA update cite that, not 14.3 or 7.2."""
    if kind == "supplier" and action in ("notify_customers", "update_eta"):
        return "MSA-9.1"
    if action == "notify_customers" and kind != "weather":
        return "MSA-7.2"
    return DEFAULT_CLAUSE[action]


def fallback_brief(ctx: Ctx, reason: str) -> tuple[str, list[Candidate], list[dict]]:
    b = ctx.store.exposure_breakdown(ctx.ev["id"])
    planner = ctx.get_planner()
    plan = planner.fallback_plan()
    chain = _primary_chain(ctx)
    infos = []
    for c in plan:
        cid = _rule_clause(c.action_type, ctx.ev["kind"])
        stored = ctx.store.clause(chain, cid) if chain else None
        infos.append({"clause_id": cid if stored else None, "clause_chain": chain if stored else None,
                      "clause_quote": (stored["text"][:160] if stored else None),
                      "rationale": "Chosen by rule from the priced options (agent fallback)."})
    best = max(plan, key=lambda c: c.saving - c.cost, default=None)
    summary = (f"Agent fallback (deterministic brief, {reason}). {ctx.ev['title']}: {b['shipments']} shipments worth "
               f"{fmt_inr(b['value_inr'])} in your scope are affected, with penalty exposure of {fmt_inr(b['net_inr'])}.")
    if best:
        summary += f" Best priced option: {best.title}, estimated saving {fmt_inr(best.saving)} for a cost of {fmt_inr(best.cost)}."
    return summary, plan, infos


# ------------------------------------------------------------------ the run
SYSTEM_PROMPT = """You are the triage analyst of a logistics control tower for Tarangini Distribution (fictional, India).
You advise {who}. You can only see what the tools return: {scope}.

Work in this order, one tool at a time: get_incident, find_affected, compute_exposure, get_clause with clause_id (once for each clause in
compute_exposure's clauses_that_price_this, and only those), list_options, then finish with ONE propose_playbooks call: a
short summary and 2 to 4 playbooks (1 if list_options shows only one kind of action).

Rules:
- Never calculate. Money, delays and savings come from tools; copy rupee figures exactly as the tools print them.
- Quote contract clauses only from get_clause output: give clause_id, clause_chain and an exact excerpt as clause_quote.
- At most one playbook per action type. Use only action types and targets that list_options shows; do not invent shipment ids, DCs or carriers.
- Playbook costs and savings are computed by code after you answer; leave them out.
- Be brief and plain. Now: {now}."""


def _tool_msg(name: str, res: dict) -> dict:
    text = json.dumps(res, default=str)
    if len(text) > MAX_TOOL_JSON:
        text = text[:MAX_TOOL_JSON] + "...[truncated]"
    return {"role": "tool", "tool_name": name, "content": text}


def triage(pool, settings: Settings, clock: Clock, user: User, store: ScopedStore, incident_id: int, *,
           use_cache: bool = False) -> dict:
    """Run the agent for one incident as one user. Returns {run, playbooks}. Raises LookupError if not visible."""
    inc = store.incident(incident_id)
    if inc is None:
        raise LookupError("no such incident")
    if use_cache:
        cached = store.latest_run(incident_id)
        if cached:
            return {"run": cached, "playbooks": store.playbooks(incident_id, role_only=True), "cached": True}
    started = clock.now()
    with pool.connection() as conn:
        run_id = conn.execute(
            "INSERT INTO agent_run (incident_id, user_id, role, model, started_at) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (incident_id, user.id, user.role, settings.llm_model, started)).fetchone()["id"]
    ctx = Ctx(pool, settings, clock, user, store, inc, run_id)
    t0 = time.monotonic()
    usage = {"prompt": 0, "completion": 0, "calls": 0}
    failures: list[str] = []
    outcome, reason = "fallback", "no proposal"
    accepted: tuple[list[Candidate], list[dict], str] | None = None
    seq = 0
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(who=user.name, scope=_scope_note(user),
                                                               now=clock.now().astimezone(IST).strftime("%A %d %B %Y, %H:%M IST"))},
            {"role": "user", "content": f"Triage incident {incident_id}."},
        ]
        retried = nudged = False
        for _ in range(MAX_LLM_CALLS):
            res = llm.chat(settings, messages, TOOL_SPECS)
            usage["prompt"] += res.prompt_tokens
            usage["completion"] += res.completion_tokens
            usage["calls"] += 1
            if not res.tool_calls:
                if nudged:
                    reason = "the model answered without calling propose_playbooks"
                    break
                nudged = True
                messages.append({"role": "assistant", "content": res.content})
                messages.append({"role": "user", "content": "Use the tools. Finish by calling propose_playbooks."})
                continue
            messages.append({"role": "assistant", "content": res.content,
                             "tool_calls": [{"function": {"name": c.name, "arguments": c.args}} for c in res.tool_calls]})
            done = False
            for call in res.tool_calls:
                t1 = time.monotonic()
                if call.name == "propose_playbooks":
                    missing = [t for t in REQUIRED_BEFORE_PROPOSAL if t not in ctx.called]
                    if missing:
                        result = {"error": f"call {', '.join(missing)} first"}
                    else:
                        cands, infos, errs = check_proposal(ctx, call.args)
                        if errs:
                            failures += errs
                            result = {"rejected": errs, "instruction": "Fix these and call propose_playbooks again."}
                            if retried:
                                reason = "honesty checks failed twice"
                                done = True
                            retried = True
                        else:
                            accepted = (cands, infos, str(call.args["summary"]).strip())
                            result, done = {"accepted": True}, True
                elif call.name in TOOL_FUNCS:
                    iid = call.args.get("incident_id")
                    if iid is not None and str(iid) != str(incident_id):
                        result = {"error": f"you are triaging incident {incident_id}"}
                    else:
                        try:
                            result = TOOL_FUNCS[call.name](ctx, call.args)
                        except Exception as e:   # a tool bug must not kill the run: tell the model, log it
                            log(logger, logging.ERROR, "tool failed", tool=call.name, error=type(e).__name__)
                            result = {"error": f"{call.name} failed"}
                    if "error" not in result:
                        ctx.called.append(call.name)
                        ctx.outputs.append(result)
                else:
                    result = {"error": f"unknown tool {call.name}"}
                seq += 1
                ms = int((time.monotonic() - t1) * 1000)
                with pool.connection() as conn:
                    conn.execute("INSERT INTO agent_step (run_id, seq, tool, args, result_summary, ms) VALUES (%s,%s,%s,%s,%s,%s)",
                                 (run_id, seq, call.name, json.dumps(call.args, default=str),
                                  _step_summary(call.name, result), ms))
                messages.append(_tool_msg(call.name, result))
            if done:
                break
    except llm.LLMError as e:
        reason = f"model unavailable: {e}"
    except Exception as e:   # never leave a run hanging: fall back and say why
        log(logger, logging.ERROR, "agent run failed", error=type(e).__name__)
        reason = f"internal error: {type(e).__name__}"
    if accepted:
        cands, infos, summary = accepted
        outcome, reason, source = "ok", None, "agent"
    else:
        summary, cands, infos = fallback_brief(ctx, reason)
        outcome, source = "fallback", "fallback"
    ms_total = int((time.monotonic() - t0) * 1000)
    with pool.connection() as conn, conn.transaction():
        conn.execute("UPDATE playbook SET status = 'rejected' WHERE incident_id = %s AND role = %s AND status = 'proposed'",
                     (incident_id, user.role))
        for cand, info in zip(cands, infos, strict=True):
            actions.save_playbook(conn, incident_id=incident_id, run_id=run_id, user=user, cand=cand,
                                  clause_id=info["clause_id"], clause_chain=info["clause_chain"],
                                  clause_quote=info["clause_quote"], rationale=info["rationale"], source=source)
        conn.execute(
            "UPDATE agent_run SET finished_at = %s, ms = %s, prompt_tokens = %s, completion_tokens = %s, llm_calls = %s, "
            "outcome = %s, fallback_reason = %s, honesty_failures = %s, summary = %s WHERE id = %s",
            (clock.now(), ms_total, usage["prompt"], usage["completion"], usage["calls"], outcome, reason,
             json.dumps(failures), summary, run_id))
    log(logger, logging.INFO, "triage finished", incident=incident_id, user=user.id, outcome=outcome, ms=ms_total,
        prompt_tokens=usage["prompt"], completion_tokens=usage["completion"], failures=len(failures))
    return {"run": store.latest_run(incident_id), "playbooks": store.playbooks(incident_id, role_only=True), "cached": False}


def _step_summary(name: str, res: dict) -> str:
    if name == "propose_playbooks":
        if res.get("accepted"):
            return "accepted"
        return "rejected: " + "; ".join(res.get("rejected") or [res.get("error", "")])[:300]
    return summarize(name, res)[:300]
