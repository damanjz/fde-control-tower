"""Impact engine and money at risk. Deterministic code, no LLM, no arithmetic by a model.

recompute_incident() writes incident_impact (one row per affected shipment, with its penalty) and
incident_exposure (one row per incident). It is idempotent: the same inputs give the same rows.
The numbers (delays, rates) are documented in docs/agent-and-access.md.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from psycopg import Connection
from psycopg.types.json import Jsonb

from tower.clock import IST
from tower.logging_ import log

logger = logging.getLogger("tower.risk")

SLACK_DAYS = 0.5                  # grace on the promised time before a day counts as late
LOW_STOCK_CASES = 40              # a DC with fewer cases of a SKU than this will short-ship it
KM_PER_DAY = 450.0                # truck progress used for re-route delay
DEFAULT_NOTIFY_HOURS = 6

# est_delay_days per severity for event kinds without a more specific rule
DELAY_BY_SEVERITY = {
    "weather": {"low": 0.5, "medium": 1.0, "high": 1.5, "critical": 2.5},
    "facility": {"low": 0.25, "medium": 0.5, "high": 1.5, "critical": 3.0},
    "carrier": {"low": 0.5, "medium": 1.0, "high": 2.0, "critical": 3.0},
}
FOG_DELAY_DAYS = 0.75             # trucks hold at toll plazas for part of a day
CYCLONE_DELAY_DAYS = 3.0          # coastal lanes held while the system passes
CLOSURE_DETOUR_DAYS = 1.0         # added to the stated reopening time for a closed highway
SUPPLIER_DELAY_DAYS = 0.5         # re-pick / backorder handling on an affected shipment
SHORTFALL_PER_LATE_DAY = 0.08     # share of an affected SKU that cannot be shipped, per day the supplier is late
MAX_SHORTFALL = 0.60


# ------------------------------------------------------------------ pure pricing
@dataclass
class Terms:
    """Contract terms for one customer, parsed from the chain's MSA PDF (or the seed, flagged, when no PDF)."""
    rate_per_day: float = 0.015
    cap: float = 0.10
    min_fill_rate: float = 0.95
    fill_penalty: float = 0.02
    notify_hours: int = DEFAULT_NOTIFY_HOURS
    grace_days: float = SLACK_DAYS
    clauses: dict[str, str] = field(default_factory=lambda: {"late_delivery": "MSA-7.2", "fill_rate": "MSA-9.1",
                                                              "force_majeure": "MSA-14.3"})
    unverified: bool = False


@dataclass
class Price:
    days_late: int
    late_inr: Decimal
    fill_inr: Decimal
    gross_inr: Decimal
    waived: bool
    net_inr: Decimal
    clauses: list[str]


def _money(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


def days_late(arrival: datetime, promised: datetime, grace_days: float = SLACK_DAYS) -> int:
    """max(0, ceil(arrival - promised - grace)), in whole days."""
    x = (arrival - promised).total_seconds() / 86400 - grace_days
    return max(0, math.ceil(x - 1e-9))


def price_shipment(*, value: Decimal, promised: datetime, arrival: datetime, terms: Terms,
                   affected_value: Decimal | None = None, fill_rate: float | None = None,
                   weather: bool = False, notified: bool = False) -> Price:
    """Late penalty (MSA-7.2), fill-rate penalty (MSA-9.1) and the MSA-14.3 waiver for one shipment."""
    clauses: list[str] = []
    dl = days_late(arrival, promised, terms.grace_days)
    late = _money(Decimal(value) * Decimal(str(min(terms.rate_per_day * dl, terms.cap)))) if dl else Decimal("0.00")
    if dl:
        clauses.append(terms.clauses["late_delivery"])
    fill = Decimal("0.00")
    if fill_rate is not None and fill_rate < terms.min_fill_rate and affected_value:
        fill = _money(Decimal(affected_value) * Decimal(str(terms.fill_penalty)))
        clauses.append(terms.clauses["fill_rate"])
    gross = late + fill
    waived = bool(weather and notified and gross > 0)
    if waived:
        clauses.append(terms.clauses["force_majeure"])
    return Price(dl, late, fill, gross, waived, Decimal("0.00") if waived else gross, clauses)


# ------------------------------------------------------------------ contract terms from the parsed PDFs
def load_terms(conn: Connection, customer_ids: list[int] | None = None) -> dict[int, Terms]:
    """customer_id -> Terms. PDF-parsed chunk params win; a customer without an ingested MSA falls back to the
    seeded clause params and is flagged unverified."""
    where, params = ("WHERE c.id = ANY(%s)", [customer_ids]) if customer_ids is not None else ("", [])
    out: dict[int, Terms] = {}
    rows = conn.execute(
        f"""SELECT c.id AS customer_id, ch.kind, ch.clause_id, ch.params, ch.verified
            FROM customer c JOIN contract k ON k.customer_id = c.id
            JOIN contract_chunk ch ON ch.msa_id = k.msa_id AND ch.kind IS NOT NULL {where}""",  # noqa: S608
        params,
    ).fetchall()
    for r in rows:
        t = out.setdefault(r["customer_id"], Terms())
        t.clauses[r["kind"]] = r["clause_id"]
        p = r["params"]
        if r["kind"] == "late_delivery":
            t.rate_per_day, t.cap = float(p["rate_per_day"]), float(p["cap"])
            t.grace_days = float(p.get("grace_hours", 12)) / 24
        elif r["kind"] == "fill_rate":
            t.min_fill_rate, t.fill_penalty = float(p["min_fill_rate"]), float(p["penalty"])
        elif r["kind"] == "force_majeure":
            t.notify_hours = int(p["notify_within_hours"])
        if r["verified"] is False:
            t.unverified = True
    wanted = customer_ids if customer_ids is not None else [r["id"] for r in conn.execute("SELECT id FROM customer").fetchall()]
    seeded = [c for c in wanted if c not in out]
    if seeded:
        for r in conn.execute(
            "SELECT k.customer_id, cl.clause_id, cl.kind, cl.params FROM contract k JOIN contract_clause cl ON cl.contract_id = k.id "
            "WHERE k.customer_id = ANY(%s)", (seeded,)
        ).fetchall():
            t = out.setdefault(r["customer_id"], Terms(unverified=True))
            t.clauses[r["kind"]] = r["clause_id"]
            p = r["params"]
            if r["kind"] == "late_delivery":
                t.rate_per_day, t.cap = float(p["rate_per_day"]), float(p["cap"])
            elif r["kind"] == "fill_rate":
                t.min_fill_rate, t.fill_penalty = float(p["min_fill_rate"]), float(p["penalty"])
            elif r["kind"] == "force_majeure":
                t.notify_hours = int(p["notify_within_hours"])
    return out


# ------------------------------------------------------------------ the delay model
def weather_subtype(ev: dict) -> str:
    text = f"{ev['title']} {ev['detail']}".lower()
    if "fog" in text:
        return "fog"
    if "cyclone" in text:
        return "cyclone"
    if "closed" in text or "closure" in text:
        return "closure"
    return "weather"


def delay_model(ev: dict) -> tuple[str, float]:
    """(subtype, est_delay_days) for an incident, from its kind, severity and the numbers in its text."""
    kind, sev = ev["kind"], ev["severity"]
    if kind == "weather":
        sub = weather_subtype(ev)
        if sub == "fog":
            return sub, FOG_DELAY_DAYS
        if sub == "cyclone":
            return sub, CYCLONE_DELAY_DAYS
        if sub == "closure":
            m = re.search(r"(\d+)\s*hours?", ev["detail"])
            hours = int(m.group(1)) if m else 24
            return sub, round(hours / 24 + CLOSURE_DETOUR_DAYS, 2)
        return sub, DELAY_BY_SEVERITY["weather"][sev]
    if kind == "supplier":
        return "supplier", SUPPLIER_DELAY_DAYS
    return kind, DELAY_BY_SEVERITY[kind][sev]


def supplier_shortfall(ev: dict) -> float:
    m = re.search(r"(\d+)\s*days?\s*late", f"{ev['title']} {ev['detail']}")
    late_days = int(m.group(1)) if m else {"low": 2, "medium": 3, "high": 5, "critical": 7}[ev["severity"]]
    return min(MAX_SHORTFALL, SHORTFALL_PER_LATE_DAY * late_days)


def facility_window_end(ev: dict) -> datetime:
    """End of the outage: a clock time in the text ('ETA 18:00', 'until about 11:30') on the event day, else +12 h."""
    m = re.search(r"(?:ETA|until(?: about)?)\s+(\d{1,2}):(\d{2})", ev["detail"])
    start = ev["started_at"].astimezone(IST)
    if m:
        end = start.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if end > start:
            return end
    return start + timedelta(hours=12)


def lane_distance_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float, p_lat: float, p_lon: float) -> float:
    """Shortest distance from point P to the straight segment A-B, on a local flat projection (km)."""
    kx = 111.32 * math.cos(math.radians(p_lat))
    ky = 110.57
    ax, ay = (a_lon - p_lon) * kx, (a_lat - p_lat) * ky
    bx, by = (b_lon - p_lon) * kx, (b_lat - p_lat) * ky
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    return math.hypot(ax + t * dx, ay + t * dy)


# ------------------------------------------------------------------ who is affected
_BASE = """SELECT s.id, s.dc_id, s.zone_id, s.carrier_id, s.customer_id, s.value_inr, s.eta, s.promised_at, s.departed_at,
                  z.state, z.lat AS zlat, z.lon AS zlon, w.lat AS dlat, w.lon AS dlon
           FROM shipment s JOIN zone z ON z.id = s.zone_id JOIN warehouse w ON w.id = s.dc_id
           WHERE s.status <> 'delivered' AND s.eta > %s"""


def select_impact(conn: Connection, ev: dict) -> list[dict]:
    """Affected shipments for one incident: [{shipment, reason, est_delay_days, affected_value, fill_rate}]."""
    sub, delay = delay_model(ev)
    base = conn.execute(_BASE + " ORDER BY s.id", (ev["started_at"],)).fetchall()
    out: list[dict] = []

    def add(s: dict, reason: str, *, value=None, fill=None, d=delay) -> None:
        out.append({"shipment": s, "reason": reason, "est_delay_days": d,
                    "affected_value": Decimal(value if value is not None else s["value_inr"]), "fill_rate": fill})

    if ev["kind"] == "weather":
        for s in base:
            d = lane_distance_km(s["dlat"], s["dlon"], s["zlat"], s["zlon"], ev["lat"], ev["lon"])
            if d <= ev["radius_km"]:
                add(s, f"lane {s['dc_id']} to {s['zone_id']} passes {d:.0f} km from the event centre "
                       f"(radius {ev['radius_km']:.0f} km), {sub}")
    elif ev["kind"] == "facility":
        end = facility_window_end(ev)
        for s in base:
            if s["dc_id"] == ev["dc_id"] and ev["started_at"] <= s["departed_at"] <= end:
                add(s, f"departs {s['dc_id']} inside the outage window ending {end:%H:%M} IST")
    elif ev["kind"] == "carrier":
        states = ev["states"]
        for s in base:
            if s["carrier_id"] == ev["carrier_id"] and (not states or s["state"] in states):
                add(s, f"{s['carrier_id']} shipment into {s['state']} during the strike")
    elif ev["kind"] == "supplier":
        short = supplier_shortfall(ev)
        by_ship = {s["id"]: s for s in base}
        lines = conn.execute(
            """SELECT l.shipment_id, l.sku, l.value_inr, st.dc_id
               FROM shipment_line l JOIN product p ON p.sku = l.sku AND p.supplier_id = %s
               JOIN shipment s ON s.id = l.shipment_id
               JOIN dc_stock st ON st.dc_id = s.dc_id AND st.sku = l.sku AND st.cases < %s
               ORDER BY l.shipment_id, l.sku""",
            (ev["supplier_id"], LOW_STOCK_CASES),
        ).fetchall()
        grouped: dict[str, list[dict]] = {}
        for ln in lines:
            if ln["shipment_id"] in by_ship:
                grouped.setdefault(ln["shipment_id"], []).append(ln)
        # stock already moved in by transfer playbooks for this incident covers part of the shortfall at that DC and SKU
        received = {(r["to_dc"], r["sku"]): r["cases"] for r in conn.execute(
            "SELECT t.to_dc, t.sku, sum(t.cases)::int AS cases FROM stock_transfer t JOIN playbook pb ON pb.id = t.playbook_id "
            "WHERE pb.incident_id = %s GROUP BY 1, 2", (ev["id"],)).fetchall()}
        needed: dict[tuple[str, str], int] = {}
        for sid, lns in grouped.items():
            for x in lns:
                cases = conn.execute("SELECT cases FROM shipment_line WHERE shipment_id = %s AND sku = %s", (sid, x["sku"])).fetchone()["cases"]
                needed[(x["dc_id"], x["sku"])] = needed.get((x["dc_id"], x["sku"]), 0) + math.ceil(short * cases)
        for sid, lns in grouped.items():
            s = by_ship[sid]
            cover = min(1.0, min((received.get((x["dc_id"], x["sku"]), 0) / needed[(x["dc_id"], x["sku"])] for x in lns
                                  if needed[(x["dc_id"], x["sku"])]), default=1.0))
            skus = ", ".join(x["sku"] for x in lns)
            add(s, f"lines {skus} depend on the late supplier and {s['dc_id']} stock is below {LOW_STOCK_CASES} cases",
                value=sum(x["value_inr"] for x in lns), fill=round(1 - short * (1 - cover), 3))
    return out


# ------------------------------------------------------------------ recompute
def recompute_incident(conn: Connection, ev: dict, now: datetime) -> dict:
    """Rebuild impact and exposure for one incident. Returns the roll-up."""
    # the impact worker and an action's own recompute must not interleave on one incident
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (7_000_000 + ev["id"],))
    sub, _ = delay_model(ev)
    hits = select_impact(conn, ev)
    terms = load_terms(conn, sorted({h["shipment"]["customer_id"] for h in hits})) if hits else {}
    adjusted = {(r["shipment_id"]): float(r["days"]) for r in conn.execute(
        "SELECT shipment_id, days FROM eta_adjustment WHERE incident_id = %s", (ev["id"],)).fetchall()}
    notified = {r["customer_id"]: r["notified_at"] for r in conn.execute(
        "SELECT customer_id, notified_at FROM customer_notification WHERE incident_id = %s", (ev["id"],)).fetchall()}
    weather = ev["kind"] == "weather"
    rows, tot = [], {"value": Decimal(0), "gross": Decimal(0), "waivable": Decimal(0), "net": Decimal(0)}
    deadline = None
    unverified = 0
    for h in hits:
        s = h["shipment"]
        t = terms[s["customer_id"]]
        remaining = max(0.0, h["est_delay_days"] - adjusted.get(s["id"], 0.0))
        arrival = s["eta"] + timedelta(days=remaining)
        n_at = notified.get(s["customer_id"])
        ddl = ev["started_at"] + timedelta(hours=t.notify_hours)
        in_window = bool(n_at and n_at <= ddl)
        p = price_shipment(value=s["value_inr"], promised=s["promised_at"], arrival=arrival, terms=t,
                           affected_value=h["affected_value"], fill_rate=h["fill_rate"], weather=weather,
                           notified=in_window)
        if weather:
            deadline = ddl if deadline is None else min(deadline, ddl)
            if not p.waived and now <= ddl:
                tot["waivable"] += p.gross_inr
        unverified += t.unverified
        tot["value"] += s["value_inr"]
        tot["gross"] += p.gross_inr
        tot["net"] += p.net_inr
        rows.append((ev["id"], s["id"], h["reason"], round(h["est_delay_days"], 2), h["affected_value"], h["fill_rate"],
                     arrival, p.days_late, p.late_inr, p.fill_inr, p.gross_inr, p.waived, p.net_inr, p.clauses,
                     t.unverified, now))
    conn.execute("DELETE FROM incident_impact WHERE incident_id = %s", (ev["id"],))
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO incident_impact (incident_id, shipment_id, reason, est_delay_days, affected_value_inr,
                   est_fill_rate, arrival_estimate, days_late, late_penalty_inr, fill_penalty_inr, gross_penalty_inr,
                   waived, net_penalty_inr, clauses, params_unverified, computed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
    notes = {"subtype": sub, "delay_days": delay_model(ev)[1], "unverified_rows": unverified}
    conn.execute(
        """INSERT INTO incident_exposure (incident_id, shipments, value_inr, gross_inr, waivable_inr, net_inr,
                                          notify_deadline, notes, computed_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (incident_id) DO UPDATE SET shipments = EXCLUDED.shipments, value_inr = EXCLUDED.value_inr,
               gross_inr = EXCLUDED.gross_inr, waivable_inr = EXCLUDED.waivable_inr, net_inr = EXCLUDED.net_inr,
               notify_deadline = EXCLUDED.notify_deadline, notes = EXCLUDED.notes, computed_at = EXCLUDED.computed_at""",
        (ev["id"], len(rows), tot["value"], tot["gross"], tot["waivable"], tot["net"], deadline, Jsonb(notes), now),
    )
    return {"incident_id": ev["id"], "shipments": len(rows), **{k: float(v) for k, v in tot.items()}}


def recompute_all(pool, now: datetime, status: str = "active") -> list[dict]:
    """Recompute every incident with the given status. Safe to call repeatedly."""
    out = []
    with pool.connection() as conn:
        for ev in conn.execute("SELECT * FROM disruption_event WHERE status = %s ORDER BY id", (status,)).fetchall():
            with conn.transaction():
                out.append(recompute_incident(conn, ev, now))
    log(logger, logging.DEBUG, "impact recomputed", incidents=len(out), shipments=sum(o["shipments"] for o in out))
    return out
