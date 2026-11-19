"""Playbook economics, computed by code: goods moved, cost and saving for each action type.

The agent chooses an action type and a target (a DC, a carrier); everything numeric here comes from the exposure
engine in tower.risk and the cost model below. Inputs are the user's scoped impact rows, so a dispatcher can only
ever plan for his own shipments.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from tower import risk
from tower.scoped import ScopedStore

ACTION_TYPES = ("notify_customers", "reroute_via_dc", "swap_carrier", "stock_transfer", "update_eta")
# which incident kinds an action makes sense for (code rejects the rest, so the model can correct itself)
ALLOWED = {
    "notify_customers": {"weather", "facility", "carrier", "supplier"},
    "update_eta": {"weather", "facility", "carrier", "supplier"},
    "reroute_via_dc": {"weather", "facility"},
    "swap_carrier": {"carrier"},
    "stock_transfer": {"supplier"},
}
DEFAULT_CLAUSE = {"notify_customers": "MSA-14.3", "update_eta": "MSA-7.2", "reroute_via_dc": "MSA-7.2",
                  "swap_carrier": "MSA-7.2", "stock_transfer": "MSA-9.1"}

# cost model (rupees), documented in docs/agent-and-access.md
CASES_PER_TRUCK = 600
RUPEES_PER_TRUCK_KM = 38.0
ROAD_FACTOR = 1.25                 # straight line to road distance
REPICK_DAYS = 0.5                  # re-pick and re-load at the new DC
REROUTE_HANDLING_PER_TRUCK = 2500
SWAP_DELAY_DAYS = 0.5              # re-dispatch with the new carrier
SWAP_PREMIUM = 0.25                # premium over the normal freight rate
SWAP_REBOOK_PER_TRUCK = 1500
NOTIFY_COST_PER_CUSTOMER = 40
TRANSFER_RUPEES_PER_CASE_KM = 0.35
MAX_TRANSFER_SHARE = 0.5           # never move more than half of the source DC's stock of a SKU


class PlaybookError(ValueError):
    """The requested action cannot be built; the message goes back to the model or the user."""


@dataclass
class Candidate:
    action_type: str
    title: str
    params: dict
    shipment_ids: list[str]
    dc_scope: list[str]
    goods_value: Decimal
    cost: Decimal
    saving: Decimal
    notes: list[str] = field(default_factory=list)


MOVERS = ("reroute_via_dc", "swap_carrier", "stock_transfer")


def pays_for_itself(c: Candidate) -> bool:
    """A mover (re-route, carrier swap, stock transfer) spends money to cut penalties: it is worth offering only when the
    penalty it removes is larger than what it costs. Notifications and ETA updates are cheap and always allowed."""
    return c.action_type not in MOVERS or c.saving > c.cost


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _d(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


class Planner:
    """Builds and prices playbooks for one incident as one user."""

    def __init__(self, pool, store: ScopedStore, ev: dict, now: datetime):
        self.pool, self.store, self.ev, self.now = pool, store, ev, now
        self.rows = [r for r in store.impact_rows(ev["id"]) if r["status"] != "delivered"]
        net = store.network()
        self.dc = {w["id"]: (w["lat"], w["lon"]) for w in net["warehouses"]}
        self.zone = {z["id"]: (z["lat"], z["lon"], z["road_km"]) for z in net["zones"]}
        with pool.connection() as conn:
            self.terms = risk.load_terms(conn, sorted({r["customer_id"] for r in self.rows})) if self.rows else {}
            self.fleet = {(f["dc_id"], f["carrier_id"]): f["trucks_free"] for f in
                          conn.execute("SELECT dc_id, carrier_id, trucks_free FROM fleet_capacity").fetchall()}
            self.stock = {(s["dc_id"], s["sku"]): s["cases"] for s in
                          conn.execute("SELECT dc_id, sku, cases FROM dc_stock").fetchall()}
            self.prices = {p["sku"]: float(p["unit_price"]) * p["case_qty"] for p in
                           conn.execute("SELECT sku, unit_price, case_qty FROM product").fetchall()}
        self.weather = ev["kind"] == "weather"

    # ------------------------------------------------------------ helpers
    def _row_net(self, r: dict) -> Decimal:
        return Decimal(r["net_penalty_inr"])

    def _reprice(self, r: dict, *, arrival: datetime, fill: float | None = None, notified: bool = False) -> risk.Price:
        return risk.price_shipment(
            value=Decimal(r["value_inr"]), promised=r["promised_at"], arrival=arrival, terms=self.terms[r["customer_id"]],
            affected_value=Decimal(r["affected_value_inr"]), fill_rate=fill, weather=self.weather, notified=notified)

    def _lane_km(self, dc_id: str, zone_id: str) -> float:
        z = self.zone[zone_id]
        return haversine_km(self.dc[dc_id], (z[0], z[1])) * ROAD_FACTOR

    @staticmethod
    def _trucks(cases: int) -> int:
        return max(1, math.ceil(cases / CASES_PER_TRUCK))

    def _check_kind(self, action: str) -> None:
        if self.ev["kind"] not in ALLOWED[action]:
            raise PlaybookError(f"{action} does not apply to a {self.ev['kind']} incident; "
                                f"use one of: {', '.join(a for a, ks in ALLOWED.items() if self.ev['kind'] in ks)}")

    def _need_rows(self) -> list[dict]:
        if not self.rows:
            raise PlaybookError("no affected shipments in your scope for this incident")
        return self.rows

    # ------------------------------------------------------------ builders
    def build(self, action: str, params: dict | None = None) -> Candidate:
        params = params or {}
        if action not in ACTION_TYPES:
            raise PlaybookError(f"unknown action_type {action!r}; use one of: {', '.join(ACTION_TYPES)}")
        self._check_kind(action)
        return getattr(self, f"_{action}")(params)

    def _notify_customers(self, params: dict) -> Candidate:
        rows = self._need_rows()
        by_cust: dict[int, list[dict]] = {}
        for r in rows:
            by_cust.setdefault(r["customer_id"], []).append(r)
        asked = params.get("customer_ids")
        if asked is not None and not set(asked) <= set(by_cust):
            raise PlaybookError("customer_ids include customers with no affected shipment in your scope")
        chosen = sorted(asked) if asked else sorted(by_cust)
        saving = Decimal(0)
        for cid in chosen:
            for r in by_cust[cid]:
                ddl = self.ev["started_at"] + timedelta(hours=self.terms[cid].notify_hours)
                if self.weather and not r["waived"] and self.now <= ddl:
                    p = self._reprice(r, arrival=r["arrival_estimate"], fill=_f(r["est_fill_rate"]), notified=True)
                    saving += self._row_net(r) - p.net_inr
        ships = [r["shipment_id"] for cid in chosen for r in by_cust[cid]]
        custs = [{"customer_id": cid, "customer": by_cust[cid][0]["customer"],
                  "shipment_ids": [r["shipment_id"] for r in by_cust[cid]]} for cid in chosen]
        note = []
        if self.weather:
            note.append("MSA-14.3 waives penalties when the customer is told within the clause window of the event start")
        else:
            # no clause id here: naming the weather waiver on a non-weather incident made the model cite it (eval run 4, C04/H04)
            note.append("not a weather event: telling customers waives no penalty")
        return Candidate("notify_customers", f"Notify {len(chosen)} customers about: {self.ev['title']}",
                         {"customers": custs}, ships, sorted({r["dc_id"] for r in rows if r["shipment_id"] in set(ships)}),
                         Decimal(0), _d(NOTIFY_COST_PER_CUSTOMER * len(chosen)), _d(saving), note)

    def _update_eta(self, params: dict) -> Candidate:
        rows = self._need_rows()
        moves = [{"shipment_id": r["shipment_id"], "add_days": float(r["est_delay_days"])} for r in rows]
        return Candidate("update_eta", f"Update ETAs by the estimated delay for {len(rows)} shipments", {"moves": moves},
                         [r["shipment_id"] for r in rows], sorted({r["dc_id"] for r in rows}), Decimal(0), Decimal(0), Decimal(0),
                         ["ETA updates tell customers the new time; the promised time and the penalty do not change"])

    def _reroute_via_dc(self, params: dict) -> Candidate:
        to_dc = params.get("to_dc")
        if to_dc not in self.dc:
            raise PlaybookError(f"to_dc must be one of {', '.join(sorted(self.dc))}")
        rows = [r for r in self._need_rows() if r["dc_id"] != to_dc]
        if not rows:
            raise PlaybookError(f"no affected shipment can move to {to_dc} (they already leave from it)")
        rows.sort(key=lambda r: (-float(r["gross_penalty_inr"]), r["shipment_id"]))
        left = {c: n for (d, c), n in self.fleet.items() if d == to_dc}
        moves, trucks_used, notes = [], {}, []
        cost = saving = goods = Decimal(0)
        still_inside = 0
        for r in rows:
            carrier = r["carrier_id"]
            need = self._trucks(r["cases"])
            if left.get(carrier, 0) < need:
                continue
            # a weather event is matched by lane, so a new origin only helps if the new lane clears the event radius
            # (a storm at the destination still hits every lane into it); risk.select_impact applies the same test
            if self.weather:
                z = self.zone[r["zone_id"]]
                if risk.lane_distance_km(*self.dc[to_dc], z[0], z[1], self.ev["lat"], self.ev["lon"]) <= self.ev["radius_km"]:
                    still_inside += 1
                    continue
            extra_km = max(0.0, self._lane_km(to_dc, r["zone_id"]) - self._lane_km(r["dc_id"], r["zone_id"]))
            delay = round(extra_km / risk.KM_PER_DAY + REPICK_DAYS, 2)
            arrival = r["eta"] + timedelta(days=delay)
            n_at = bool(r["waived"])
            after = self._reprice(r, arrival=arrival, notified=n_at)
            saving += self._row_net(r) - after.net_inr
            cost += _d(need * (extra_km * RUPEES_PER_TRUCK_KM + REROUTE_HANDLING_PER_TRUCK))
            goods += Decimal(r["value_inr"])
            left[carrier] -= need
            trucks_used[carrier] = trucks_used.get(carrier, 0) + need
            moves.append({"shipment_id": r["shipment_id"], "from_dc": r["dc_id"], "to_dc": to_dc, "carrier_id": carrier,
                          "trucks": need, "delay_days": delay})
        if not moves and still_inside:
            raise PlaybookError(f"lanes from {to_dc} still pass through the event, so re-routing there does not avoid it")
        if not moves:
            raise PlaybookError(f"{to_dc} has no spare trucks for these shipments")
        if still_inside:
            notes.append(f"{still_inside} shipment(s) left out: their lane from {to_dc} still passes through the event")
        if len(moves) + still_inside < len(rows):
            notes.append(f"only {len(moves)} of {len(rows)} shipments fit the spare trucks at {to_dc}")
        notes.append("extra transit and re-pick delay is priced as a possible new late penalty (saving is net of it)")
        return Candidate("reroute_via_dc", f"Re-route {len(moves)} shipments via {to_dc}",
                         {"to_dc": to_dc, "moves": moves, "trucks": trucks_used}, [m["shipment_id"] for m in moves],
                         sorted({m["from_dc"] for m in moves}), _d(goods), _d(cost), _d(saving), notes)

    def _swap_carrier(self, params: dict) -> Candidate:
        to_c = params.get("to_carrier")
        carriers = {c for (_, c) in self.fleet}
        if to_c not in carriers:
            raise PlaybookError(f"to_carrier must be one of {', '.join(sorted(carriers))}")
        if to_c == self.ev["carrier_id"]:
            raise PlaybookError("to_carrier is the carrier that is on strike")
        rows = [r for r in self._need_rows() if r["carrier_id"] != to_c]
        rows.sort(key=lambda r: (-float(r["gross_penalty_inr"]), r["shipment_id"]))
        left = {d: n for (d, c), n in self.fleet.items() if c == to_c}
        moves, cost, saving, goods, notes = [], Decimal(0), Decimal(0), Decimal(0), []
        for r in rows:
            need = self._trucks(r["cases"])
            if left.get(r["dc_id"], 0) < need:
                continue
            arrival = r["eta"] + timedelta(days=SWAP_DELAY_DAYS)
            after = self._reprice(r, arrival=arrival, notified=bool(r["waived"]))
            saving += self._row_net(r) - after.net_inr
            road = float(self.zone[r["zone_id"]][2])
            cost += _d(need * (road * RUPEES_PER_TRUCK_KM * SWAP_PREMIUM + SWAP_REBOOK_PER_TRUCK))
            goods += Decimal(r["value_inr"])
            left[r["dc_id"]] -= need
            moves.append({"shipment_id": r["shipment_id"], "dc_id": r["dc_id"], "from_carrier": r["carrier_id"],
                          "to_carrier": to_c, "trucks": need, "delay_days": SWAP_DELAY_DAYS})
        if not moves:
            raise PlaybookError(f"{to_c} has no spare trucks at the origin DCs of these shipments")
        if len(moves) < len(rows):
            notes.append(f"only {len(moves)} of {len(rows)} shipments fit {to_c}'s spare trucks")
        return Candidate("swap_carrier", f"Swap {len(moves)} shipments to carrier {to_c}",
                         {"to_carrier": to_c, "moves": moves}, [m["shipment_id"] for m in moves],
                         sorted({m["dc_id"] for m in moves}), _d(goods), _d(cost), _d(saving), notes)

    def _stock_transfer(self, params: dict) -> Candidate:
        src = params.get("from_dc")
        if src not in self.dc:
            raise PlaybookError(f"from_dc must be one of {', '.join(sorted(self.dc))}")
        rows = [r for r in self._need_rows() if r["dc_id"] != src and r["est_fill_rate"] is not None]
        if not rows:
            raise PlaybookError(f"no affected shipment leaves from a DC other than {src}")
        lines = self.store.shipment_lines([r["shipment_id"] for r in rows])
        sup_skus = {ln["sku"] for ln in lines if ln["supplier_id"] == self.ev["supplier_id"]}
        row_by = {r["shipment_id"]: r for r in rows}
        need: dict[tuple[str, str], int] = {}
        for ln in lines:
            r = row_by[ln["shipment_id"]]
            if ln["sku"] in sup_skus and self.stock.get((r["dc_id"], ln["sku"]), 10**9) < risk.LOW_STOCK_CASES:
                shortfall = 1 - float(r["est_fill_rate"])
                need[(r["dc_id"], ln["sku"])] = need.get((r["dc_id"], ln["sku"]), 0) + math.ceil(shortfall * ln["cases"])
        transfers, cost, goods = [], Decimal(0), Decimal(0)
        avail = {k: v for k, v in self.stock.items() if k[0] == src}
        cover: dict[str, float] = {}
        for (to_dc, sku), n in sorted(need.items()):
            can = int(avail.get((src, sku), 0) * MAX_TRANSFER_SHARE)
            move = min(n, can)
            cover[to_dc] = min(cover.get(to_dc, 1.0), move / n if n else 1.0)
            if move > 0:
                avail[(src, sku)] -= move
                transfers.append({"from_dc": src, "to_dc": to_dc, "sku": sku, "cases": move})
                cost += _d(move * haversine_km(self.dc[src], self.dc[to_dc]) * ROAD_FACTOR * TRANSFER_RUPEES_PER_CASE_KM)
                goods += _d(move * self.prices[sku])
        if not transfers:
            raise PlaybookError(f"{src} has no spare stock of the affected SKUs")
        saving, ships = Decimal(0), []
        for r in rows:
            if r["dc_id"] not in cover:
                continue
            short = (1 - float(r["est_fill_rate"])) * (1 - cover[r["dc_id"]])
            after = self._reprice(r, arrival=r["arrival_estimate"], fill=round(1 - short, 3), notified=bool(r["waived"]))
            saving += self._row_net(r) - after.net_inr
            ships.append(r["shipment_id"])
        notes = [f"covers {min(cover.values()):.0%} or more of the shortfall at the worst DC"]
        return Candidate("stock_transfer", f"Transfer stock from {src} to {len(cover)} DC(s) for the late supplier",
                         {"from_dc": src, "transfers": transfers}, ships,
                         sorted({src, *cover}), _d(goods), _d(cost), _d(saving), notes)

    # ------------------------------------------------------------ options and the fallback plan
    def alternatives(self) -> list[Candidate]:
        """Every buildable single-step option for this incident, priced. Used by list_options and the fallback."""
        out: list[Candidate] = []

        def attempt(action: str, **params) -> None:
            try:
                c = self.build(action, params)
            except PlaybookError:
                return
            if not pays_for_itself(c):      # a move that costs more than it saves is never an option
                return
            out.append(c)

        kind = self.ev["kind"]
        if kind in ("weather", "facility"):
            for dc in sorted(self.dc):
                attempt("reroute_via_dc", to_dc=dc)
        if kind == "carrier":
            for c in sorted({c for (_, c) in self.fleet}):
                attempt("swap_carrier", to_carrier=c)
        if kind == "supplier":
            for dc in sorted(self.dc):
                attempt("stock_transfer", from_dc=dc)
        attempt("notify_customers")
        attempt("update_eta")
        return out

    def fallback_plan(self) -> list[Candidate]:
        """2 to 4 playbooks chosen by rule: the best net-saving mover, notification, ETA update."""
        alts = self.alternatives()
        movers = sorted((c for c in alts if c.action_type in MOVERS),
                        key=lambda c: (-(c.saving - c.cost), c.title))
        plan = movers[:1] + [c for c in alts if c.action_type == "notify_customers"] + \
            [c for c in alts if c.action_type == "update_eta"]
        # the first playbook is the recommendation, so order by net saving (stable: ties keep mover, notify, ETA);
        # before 2026-11-19 a mover always came first, even when notifying saved more (eval cases W03, R04, X07)
        plan.sort(key=lambda c: -(c.saving - c.cost))
        return plan[:4]


def _f(x) -> float | None:
    return None if x is None else float(x)
