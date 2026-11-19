"""Action engine: approve-before-execute playbooks, one transaction per execution, outbox, audit, idempotency.

Rules (docs/agent-and-access.md):
  - a playbook moving goods worth <= the threshold (Rs 5,00,000) runs at once for a user who may act in its scope;
  - above it, run() parks it as pending_approval and only approve() by a user with that right executes it;
  - finance never executes; a dispatcher acts only when every DC the playbook touches is in his scope;
  - the second call for an executed playbook returns the first result (idempotency key playbook:<id>:exec).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from psycopg import Connection
from psycopg.types.json import Jsonb

from tower import risk
from tower.access import User
from tower.clock import Clock
from tower.config import Settings
from tower.logging_ import log
from tower.playbooks import Candidate
from tower.scoped import ScopedStore
from tower.store import audit

logger = logging.getLogger("tower.actions")


class ActionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def save_playbook(conn: Connection, *, incident_id: int, run_id: int | None, user: User, cand: Candidate,
                  clause_id: str | None, clause_chain: str | None, clause_quote: str | None, rationale: str,
                  source: str) -> int:
    return conn.execute(
        """INSERT INTO playbook (incident_id, run_id, role, action_type, title, params, shipment_ids, dc_scope,
               goods_value_inr, est_cost_inr, est_saving_inr, clause_id, clause_chain, clause_quote, rationale, source)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (incident_id, run_id, user.role, cand.action_type, cand.title, Jsonb(cand.params), cand.shipment_ids,
         cand.dc_scope, cand.goods_value, cand.cost, cand.saving, clause_id, clause_chain, clause_quote, rationale, source),
    ).fetchone()["id"]


def _check_may_act(user: User, pb: dict) -> None:
    if not user.can_execute:
        raise ActionError(403, f"role {user.role} cannot execute operational actions")
    if not user.covers(pb["dc_scope"]):
        raise ActionError(403, "this playbook touches DCs outside your scope")


def run_playbook(pool, settings: Settings, clock: Clock, user: User, store: ScopedStore, playbook_id: int) -> dict:
    pb = store.playbook(playbook_id)
    if pb is None:
        raise ActionError(404, "no such playbook")
    _check_may_act(user, pb)
    now = clock.now()
    with pool.connection() as conn, conn.transaction():
        row = conn.execute("SELECT * FROM playbook WHERE id = %s FOR UPDATE", (playbook_id,)).fetchone()
        if row["status"] == "executed":
            return {**_view(row), "replayed": True, "result": row["result"]}
        if row["status"] == "pending_approval":
            return {**_view(row), "message": "waiting for approval"}
        if row["goods_value_inr"] > settings.approval_threshold_inr:
            conn.execute("UPDATE playbook SET status = 'pending_approval', requested_by = %s WHERE id = %s", (user.id, playbook_id))
            audit(conn, user.id, "playbook.request_approval", f"playbook:{playbook_id}",
                  {"goods_value_inr": float(row["goods_value_inr"]), "threshold_inr": settings.approval_threshold_inr,
                   "action_type": row["action_type"]}, at=now, dc_ids=row["dc_scope"])
            row = conn.execute("SELECT * FROM playbook WHERE id = %s", (playbook_id,)).fetchone()
            return {**_view(row), "message": f"goods value is above Rs {settings.approval_threshold_inr:,}: approval needed"}
        result = _execute(conn, row, executor=user.id, approver=None, now=now)
        return {**_view(conn.execute("SELECT * FROM playbook WHERE id = %s", (playbook_id,)).fetchone()), "result": result}


def approve_playbook(pool, settings: Settings, clock: Clock, user: User, store: ScopedStore, playbook_id: int) -> dict:
    if not user.can_approve:
        raise ActionError(403, f"role {user.role} cannot approve playbooks")
    pb = store.playbook(playbook_id)
    if pb is None:
        raise ActionError(404, "no such playbook")
    now = clock.now()
    with pool.connection() as conn, conn.transaction():
        row = conn.execute("SELECT * FROM playbook WHERE id = %s FOR UPDATE", (playbook_id,)).fetchone()
        if row["status"] == "executed":
            return {**_view(row), "replayed": True, "result": row["result"]}
        if row["status"] != "pending_approval":
            raise ActionError(409, f"playbook is {row['status']}, not waiting for approval")
        conn.execute("UPDATE playbook SET approved_by = %s WHERE id = %s", (user.id, playbook_id))
        audit(conn, user.id, "playbook.approve", f"playbook:{playbook_id}",
              {"requested_by": row["requested_by"], "goods_value_inr": float(row["goods_value_inr"])}, at=now,
              dc_ids=row["dc_scope"])
        result = _execute(conn, row, executor=row["requested_by"] or user.id, approver=user.id, now=now)
        return {**_view(conn.execute("SELECT * FROM playbook WHERE id = %s", (playbook_id,)).fetchone()), "result": result}


def _view(pb: dict) -> dict:
    keys = ("id", "incident_id", "action_type", "title", "status", "goods_value_inr", "est_cost_inr", "est_saving_inr",
            "requested_by", "approved_by", "executed_by", "executed_at", "idempotency_key")
    return {k: pb[k] for k in keys}


# ------------------------------------------------------------------ execution
def _execute(conn: Connection, pb: dict, *, executor: str, approver: str | None, now: datetime) -> dict:
    ev = conn.execute("SELECT * FROM disruption_event WHERE id = %s", (pb["incident_id"],)).fetchone()
    before_exp = conn.execute("SELECT net_inr, gross_inr FROM incident_exposure WHERE incident_id = %s", (ev["id"],)).fetchone()
    handler = {"reroute_via_dc": _do_reroute, "swap_carrier": _do_swap, "update_eta": _do_update_eta,
               "notify_customers": _do_notify, "stock_transfer": _do_stock_transfer}[pb["action_type"]]
    changes, messages = handler(conn, pb, ev, now)
    # one summary message to the ops chat, plus whatever the handler queued per customer
    conn.execute("INSERT INTO outbox (topic, payload, playbook_id) VALUES ('chat_webhook', %s, %s)", (Jsonb({
        "kind": "playbook_executed", "playbook_id": pb["id"], "incident": ev["title"], "action": pb["action_type"],
        "title": pb["title"], "executed_by": executor, "approved_by": approver, "shipments": len(pb["shipment_ids"]),
        "text": f"Playbook {pb['id']} executed by {executor}: {pb['title']}"}), pb["id"]))
    for m in messages:
        conn.execute("INSERT INTO outbox (topic, payload, playbook_id) VALUES ('chat_webhook', %s, %s)", (Jsonb(m), pb["id"]))
    roll = risk.recompute_incident(conn, ev, now)
    result = {
        "changed": len(changes), "changes_sample": changes[:5], "messages_queued": len(messages) + 1,
        "exposure_before_inr": float(before_exp["net_inr"]) if before_exp else None,
        "exposure_after_inr": roll["net"], "incident_shipments_after": roll["shipments"],
    }
    conn.execute(
        "UPDATE playbook SET status = 'executed', executed_by = %s, executed_at = %s, idempotency_key = %s, result = %s WHERE id = %s",
        (executor, now, f"playbook:{pb['id']}:exec", Jsonb(result), pb["id"]))
    audit(conn, executor, "playbook.execute", f"playbook:{pb['id']}",
          {"action_type": pb["action_type"], "approved_by": approver, "goods_value_inr": float(pb["goods_value_inr"]),
           "before_after": changes[:50], "changed": len(changes), "exposure_before_inr": result["exposure_before_inr"],
           "exposure_after_inr": result["exposure_after_inr"]}, at=now, dc_ids=pb["dc_scope"])
    log(logger, logging.INFO, "playbook executed", playbook=pb["id"], action=pb["action_type"], changed=len(changes))
    return result


def _lock_ships(conn: Connection, ids: list[str]) -> dict[str, dict]:
    rows = conn.execute("SELECT id, dc_id, carrier_id, eta, version, status FROM shipment WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                        (ids,)).fetchall()
    return {r["id"]: r for r in rows}


def _take_trucks(conn: Connection, dc: str, carrier: str, n: int) -> None:
    ok = conn.execute("UPDATE fleet_capacity SET trucks_free = trucks_free - %s WHERE dc_id = %s AND carrier_id = %s "
                      "AND trucks_free >= %s RETURNING trucks_free", (n, dc, carrier, n)).fetchone()
    if not ok:
        raise ActionError(409, f"not enough spare trucks of {carrier} at {dc} any more; run triage again")


def _do_reroute(conn, pb, ev, now):
    cur = _lock_ships(conn, [m["shipment_id"] for m in pb["params"]["moves"]])
    changes = []
    for m in pb["params"]["moves"]:
        s = cur.get(m["shipment_id"])
        if s is None or s["dc_id"] != m["from_dc"] or s["status"] == "delivered":
            raise ActionError(409, f"{m['shipment_id']} changed since the plan was made; run triage again")
        _take_trucks(conn, m["to_dc"], m["carrier_id"], m["trucks"])
        new_eta = s["eta"] + timedelta(days=m["delay_days"])
        conn.execute("UPDATE shipment SET dc_id = %s, eta = %s, version = version + 1, updated_at = %s WHERE id = %s",
                     (m["to_dc"], new_eta, now, s["id"]))
        changes.append({"shipment": s["id"], "dc_id": [s["dc_id"], m["to_dc"]],
                        "eta": [s["eta"].isoformat(), new_eta.isoformat()], "version": [s["version"], s["version"] + 1]})
    return changes, []


def _do_swap(conn, pb, ev, now):
    cur = _lock_ships(conn, [m["shipment_id"] for m in pb["params"]["moves"]])
    changes = []
    for m in pb["params"]["moves"]:
        s = cur.get(m["shipment_id"])
        if s is None or s["carrier_id"] != m["from_carrier"] or s["status"] == "delivered":
            raise ActionError(409, f"{m['shipment_id']} changed since the plan was made; run triage again")
        _take_trucks(conn, m["dc_id"], m["to_carrier"], m["trucks"])
        new_eta = s["eta"] + timedelta(days=m["delay_days"])
        conn.execute("UPDATE shipment SET carrier_id = %s, eta = %s, version = version + 1, updated_at = %s WHERE id = %s",
                     (m["to_carrier"], new_eta, now, s["id"]))
        changes.append({"shipment": s["id"], "carrier_id": [s["carrier_id"], m["to_carrier"]],
                        "eta": [s["eta"].isoformat(), new_eta.isoformat()], "version": [s["version"], s["version"] + 1]})
    return changes, []


def _do_update_eta(conn, pb, ev, now):
    moves = pb["params"]["moves"]
    cur = _lock_ships(conn, [m["shipment_id"] for m in moves])
    changes = []
    for m in moves:
        s = cur.get(m["shipment_id"])
        if s is None or s["status"] == "delivered":
            continue
        new_eta = s["eta"] + timedelta(days=m["add_days"])
        conn.execute("UPDATE shipment SET eta = %s, version = version + 1, updated_at = %s WHERE id = %s", (new_eta, now, s["id"]))
        conn.execute(
            "INSERT INTO eta_adjustment (shipment_id, incident_id, days, playbook_id) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (shipment_id, incident_id) DO UPDATE SET days = eta_adjustment.days + EXCLUDED.days, playbook_id = EXCLUDED.playbook_id",
            (s["id"], ev["id"], m["add_days"], pb["id"]))
        changes.append({"shipment": s["id"], "eta": [s["eta"].isoformat(), new_eta.isoformat()],
                        "version": [s["version"], s["version"] + 1]})
    return changes, []


def _do_notify(conn, pb, ev, now):
    changes, messages = [], []
    for c in pb["params"]["customers"]:
        got = conn.execute(
            "INSERT INTO customer_notification (incident_id, customer_id, shipment_ids, notified_at, playbook_id) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (incident_id, customer_id) DO NOTHING RETURNING notified_at",
            (ev["id"], c["customer_id"], c["shipment_ids"], now, pb["id"])).fetchone()
        changes.append({"customer": c["customer"], "notified_at": [None, now.isoformat()] if got else "already notified"})
        if got:
            messages.append({"kind": "customer_notice", "playbook_id": pb["id"], "customer": c["customer"],
                             "incident": ev["title"], "shipments": len(c["shipment_ids"]),
                             "text": f"To {c['customer']}: {ev['title']} may delay {len(c['shipment_ids'])} of your deliveries. "
                                     "We will confirm new times as soon as we have them."})
    return changes, messages


def _do_stock_transfer(conn, pb, ev, now):
    changes = []
    for t in pb["params"]["transfers"]:
        ok = conn.execute("UPDATE dc_stock SET cases = cases - %s WHERE dc_id = %s AND sku = %s AND cases >= %s RETURNING cases",
                          (t["cases"], t["from_dc"], t["sku"], t["cases"])).fetchone()
        if not ok:
            raise ActionError(409, f"{t['from_dc']} no longer has {t['cases']} cases of {t['sku']}; run triage again")
        conn.execute("UPDATE dc_stock SET cases = cases + %s WHERE dc_id = %s AND sku = %s", (t["cases"], t["to_dc"], t["sku"]))
        conn.execute("INSERT INTO stock_transfer (playbook_id, from_dc, to_dc, sku, cases, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                     (pb["id"], t["from_dc"], t["to_dc"], t["sku"], t["cases"], now))
        changes.append({"sku": t["sku"], "cases": t["cases"], "from": t["from_dc"], "to": t["to_dc"],
                        "source_stock_after": ok["cases"]})
    return changes, []


def threshold_text(settings: Settings) -> str:
    return f"Rs {Decimal(settings.approval_threshold_inr):,.0f}"
