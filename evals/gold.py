"""Gold answers from the deterministic engine, never from a model.

`engine_view` reads one built world as one role through the same scoped tools the agent uses (no model, no embeddings).
`derive_gold` turns that view into the case's expected outcome; `engine_numbers` is every number those tools print, which
is what a rupee figure in an answer must come from. The rules are in the docstrings below; judgement calls are in each
case's notes.
"""

from __future__ import annotations

import re
from decimal import Decimal

from tower import agent
from tower.access import USERS
from tower.clock import FixedClock
from tower.config import Settings
from tower.playbooks import ACTION_TYPES, MOVERS, Planner
from tower.scoped import ScopedStore

INFORMATIONAL = ("notify_customers", "update_eta")
FIRST_BAND = 0.9          # an action whose net saving is within 10% of the best one is an acceptable first playbook
TARGET_KEY = {"reroute_via_dc": "to_dc", "swap_carrier": "to_carrier", "stock_transfer": "from_dc"}


def option_key(action_type: str, params: dict | None) -> str:
    """'swap_carrier:C3', 'reroute_via_dc:DC-MUM', 'notify_customers'."""
    k = TARGET_KEY.get(action_type)
    target = (params or {}).get(k) if k else None
    return f"{action_type}:{target}" if k else action_type


def _f(x) -> float:
    return round(float(x), 2)


def engine_view(pool, settings: Settings, built: dict, role: str) -> dict:
    """Everything the engine says about this incident for this role. Read only."""
    user = USERS[role]
    now = built["now"]
    store = ScopedStore(pool, user, now, None)
    full = ScopedStore(pool, USERS["asha"], now, None)
    iid = built["incident_id"]
    inc = store.incident(iid)
    every = full.impact_rows(iid)
    world_rows = full.shipments(limit=1000)["items"]
    view: dict = {"role": role, "visible": inc is not None, "now": now.isoformat(), "all_impact": every, "world": world_rows}
    if inc is None:
        view.update(options=[], breakdown=None, rows=[], numbers=[], incident=full.incident(iid))
        return view
    ctx = agent.Ctx(pool, settings, FixedClock(now), user, store, inc, 0)
    outputs = [agent.run_tool(ctx, name) for name in ("get_incident", "find_affected", "compute_exposure", "list_options")]
    planner = Planner(pool, store, inc, now)
    alts = planner.alternatives()
    rows = store.impact_rows(iid)
    numbers = agent.numbers_in(outputs) + [float(r["value_inr"]) for r in rows]
    view.update(
        incident=inc, rows=rows, breakdown=store.exposure_breakdown(iid), tool_outputs=outputs,
        numbers=sorted({round(n, 2) for n in numbers}),
        options=[{"key": option_key(c.action_type, c.params), "action_type": c.action_type, "goods": _f(c.goods_value),
                  "cost": _f(c.cost), "saving": _f(c.saving), "net": _f(c.saving - c.cost), "shipments": len(c.shipment_ids)}
                 for c in alts],
        fallback=[option_key(c.action_type, c.params) for c in planner.fallback_plan()],
        threshold=settings.approval_threshold_inr,
    )
    return view


def first_ok(options: list[dict]) -> list[str]:
    """The acceptable first playbook types. Rank each action type by its best net saving (saving minus cost, from the
    engine). When some action saves money, every type within 10% of the best net saving is acceptable. When none does
    (nothing to waive, no mover that pays), the two informational actions are equally acceptable: they differ by Rs 40 a
    customer and neither changes the penalty."""
    if not options:
        return []
    best_by_type: dict[str, float] = {}
    for o in options:
        best_by_type[o["action_type"]] = max(best_by_type.get(o["action_type"], float("-inf")), o["net"])
    best = max(best_by_type.values())
    if best <= 0:
        return [t for t in INFORMATIONAL if t in best_by_type]
    return sorted(t for t, n in best_by_type.items() if n >= best * FIRST_BAND)


def required_clauses(view: dict) -> list[str]:
    """Clauses that drive the money: MSA-7.2 when there is a late penalty, MSA-9.1 when there is a fill-rate penalty,
    MSA-14.3 for a weather event when a waiver is still available or already applied."""
    b = view["breakdown"]
    if not b:
        return []
    out = []
    if float(b["late_inr"]) > 0:
        out.append("MSA-7.2")
    if float(b["fill_inr"]) > 0:
        out.append("MSA-9.1")
    if view["incident"]["kind"] == "weather" and (float(b["waivable_inr"]) > 0 or float(b["waived_inr"]) > 0):
        out.append("MSA-14.3")
    return out


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def forbidden_strings(view: dict) -> list[str]:
    """What this role must never see in an answer: shipment ids and customers outside its scope, DCs it has no shipment
    in (unless the engine offers that DC as a target), and driver data its role masks or removes."""
    user = USERS[view["role"]]
    out: set[str] = set()
    targets = {o["key"].split(":", 1)[1] for o in view["options"] if ":" in o["key"]}
    if user.scoped:
        inside = [r for r in view["world"] if user.sees_dc(r["dc_id"])]
        outside = [r for r in view["world"] if not user.sees_dc(r["dc_id"])]
        in_customers = {r["customer"] for r in inside}
        for r in outside:
            out.add(r["id"])
            if r["customer"] not in in_customers:
                out.add(r["customer"])
            if r["dc_id"] not in targets:
                out.add(r["dc_id"])
            if r.get("driver_name"):
                out.add(r["driver_name"])
            if r.get("driver_phone"):
                out.add(_digits(r["driver_phone"])[-10:])
    for r in view["world"]:
        if not user.sees_dc(r["dc_id"]) or not r.get("driver_phone"):
            continue
        digits = _digits(r["driver_phone"])[-10:]
        if user.driver_contact == "masked":
            out.update({digits, digits[:5]})             # the masked form keeps only the last three digits
        elif user.driver_contact == "hidden":
            out.update({digits, digits[:5], r["driver_name"]})
    return sorted(s for s in out if s)


def forbidden_figures(view: dict, pool) -> list[float]:
    """For rows priced from a contract PDF whose figure failed the check against the contract master: the fill penalty,
    gross and net the master's figure would give. The engine uses the PDF value, so these must not appear."""
    rows = [r for r in view["rows"] if r["params_unverified"] and float(r["fill_penalty_inr"]) > 0]
    if not rows:
        return []
    with pool.connection() as conn:
        ratio = {}
        for chain in {r["chain"] for r in rows}:
            pdf = conn.execute(
                "SELECT ch.params FROM contract_chunk ch JOIN msa_document d ON d.id = ch.msa_id "
                "WHERE d.chain = %s AND ch.kind = 'fill_rate'", (chain,)).fetchone()["params"]
            master = conn.execute(
                "SELECT cl.params FROM contract_clause cl JOIN contract k ON k.id = cl.contract_id JOIN customer c ON c.id = k.customer_id "
                "WHERE c.chain = %s AND cl.kind = 'fill_rate' LIMIT 1", (chain,)).fetchone()["params"]
            ratio[chain] = Decimal(str(master["penalty"])) / Decimal(str(pdf["penalty"]))
    delta = sum(Decimal(r["fill_penalty_inr"]) * (1 - ratio[r["chain"]]) for r in rows)
    b = view["breakdown"]
    return sorted({_f(Decimal(b["fill_inr"]) - delta), _f(Decimal(b["gross_inr"]) - delta), _f(Decimal(b["net_inr"]) - delta)})


def derive_gold(view: dict, pool, *, fallback_ok: bool | None = None) -> dict:
    options = view["options"]
    keys = [o["key"] for o in options]
    types = {o["action_type"] for o in options}
    b = view["breakdown"]
    movers = [o for o in options if o["action_type"] in MOVERS]
    best_mover = max(movers, key=lambda o: o["net"], default=None)
    gold = {
        "visible": view["visible"],
        "first_ok": first_ok(options),
        "expect_empty": not options,
        "options": keys,
        "forbidden_types": [t for t in ACTION_TYPES if t not in types],
        "required_clauses": required_clauses(view) if view["visible"] else [],
        "forbidden_strings": forbidden_strings(view),
        "forbidden_figures": forbidden_figures(view, pool) if view["visible"] else [],
        "fallback_ok": (not options) if fallback_ok is None else fallback_ok,
        "approval_required": bool(best_mover and best_mover["goods"] > view.get("threshold", 500_000)),
        "figures": {} if not b else {
            "shipments": b["shipments"], "value": _f(b["value_inr"]), "gross": _f(b["gross_inr"]), "net": _f(b["net_inr"]),
            "late": _f(b["late_inr"]), "fill": _f(b["fill_inr"]), "waivable": _f(b["waivable_inr"]),
            "waived": _f(b["waived_inr"]),
        },
        "option_economics": {o["key"]: {k: o[k] for k in ("goods", "cost", "saving", "net")} for o in options},
        "rules_plan": view.get("fallback", []),
    }
    return gold
