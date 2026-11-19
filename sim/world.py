"""The simulated world: a deterministic universe of shipments over the real network.

Everything the channels emit (ERP rows, GPS pings, carrier statuses) is derived from this one universe, so the
three feeds agree with each other before the simulator makes them dirty.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from tower.clock import IST
from tower.network import CARRIERS, Network

SCENARIO_START = datetime(2026, 11, 13, 5, 0, tzinfo=IST)
SCENARIO_END = datetime(2026, 11, 13, 10, 40, tzinfo=IST)
LAST_DEPARTURE = datetime(2026, 11, 13, 9, 50, tzinfo=IST)   # leaves before the 10:00 export
CARRIER_NAME = {c["id"]: c["name"] for c in CARRIERS}


@dataclass
class SimShipment:
    id: str
    order_no: str
    dc: str
    zone: str
    carrier: str
    customer: str
    lines: list[tuple[str, int, int]]      # sku, cases, value (whole rupees)
    departed: datetime
    eta: datetime
    promised: datetime
    status: str = "in_transit"
    extra: dict = field(default_factory=dict)

    @property
    def cases(self) -> int:
        return sum(c for _, c, _ in self.lines)

    @property
    def value(self) -> int:
        return sum(v for _, _, v in self.lines)


class World:
    def __init__(self, network_dir: Path, seed: int = 20261113):
        self.net = Network.load(network_dir)
        self.seed = seed
        self.zone = {z["id"]: z for z in self.net.zones}
        self.dc = {w["id"]: w for w in self.net.warehouses}
        self.product = {p["sku"]: p for p in self.net.products}
        rng = random.Random(seed)
        customers_by_zone: dict[str, list[dict]] = {}
        for c in self.net.customers():
            customers_by_zone.setdefault(c["zone_id"], []).append(c)
        self.shipments: list[SimShipment] = []
        n = 4812
        for z in self.net.zones:
            count = 6 if z["road_km"] < 60 else 7 + rng.randrange(8)
            for _ in range(count):
                skus = rng.sample(self.net.products, 3 + rng.randrange(5))
                lines = []
                for p in skus:
                    cases = 8 + rng.randrange(40)
                    lines.append((p["sku"], cases, round(cases * p["case_qty"] * p["unit_price"])))
                hours = max(6, z["std_transit_days"] * 24)
                progress = (rng.randrange(5, 96)) / 100
                departed = SCENARIO_END - timedelta(hours=progress * hours)
                if departed > LAST_DEPARTURE:  # exports are hourly: nothing leaves after the last one
                    departed = LAST_DEPARTURE - timedelta(minutes=rng.randrange(0, 240))
                departed = departed.replace(second=0, microsecond=0)
                eta = departed + timedelta(hours=hours)
                self.shipments.append(SimShipment(
                    id=f"SH-{n}", order_no=f"SO-{880000 + rng.randrange(99999)}", dc=z["serving_dc"], zone=z["id"],
                    carrier=rng.choice(CARRIERS)["id"], customer=rng.choice(customers_by_zone[z["id"]])["name"],
                    lines=lines, departed=departed, eta=eta, promised=eta + timedelta(hours=12),
                ))
                n += 1
        self.by_id = {s.id: s for s in self.shipments}

    # ---- derived state
    def in_transit(self, t: datetime) -> list[SimShipment]:
        return [s for s in self.shipments if s.departed <= t < s.eta]

    def position(self, s: SimShipment, t: datetime) -> tuple[float, float, float]:
        """(lat, lon, speed_kmph) along the straight DC -> city lane, with a small deterministic wobble."""
        a, b = self.dc[s.dc], self.zone[s.zone]
        span = (s.eta - s.departed).total_seconds()
        p = min(1.0, max(0.0, (t - s.departed).total_seconds() / span))
        wob = math.sin(p * 37 + int(s.id[3:]) % 11) * 0.03
        lat = a["lat"] + (b["lat"] - a["lat"]) * p + wob
        lon = a["lon"] + (b["lon"] - a["lon"]) * p - wob
        km = self.zone[s.zone]["road_km"]
        return round(lat, 5), round(lon, 5), round(km / (span / 3600), 1)

    def carrier_view(self, s: SimShipment, t: datetime) -> dict | None:
        """What the carrier API would say about this consignment at time t (None: not handed over yet)."""
        if t < s.departed:
            return None
        eta, status = s.eta, "in_transit"
        state = self.zone[s.zone]["state"]
        if s.carrier == "C2" and state == "Uttar Pradesh" and t >= datetime(2026, 11, 13, 6, 5, tzinfo=IST):
            status, eta = "held", s.eta + timedelta(hours=48)   # strike: trucks hold at the border
        elif t >= s.eta:
            status = "delivered"
        return {"ref": s.id, "status": status, "eta": eta.isoformat()}


def total_value(world: World, t: datetime) -> int:
    return sum(s.value for s in world.in_transit(t))
