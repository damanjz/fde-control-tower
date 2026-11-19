"""The distribution network: CSV loading and the fixed lists (carriers, chains, contract clauses).

Shared by the seed, the ERP parser's reference data and the simulator, so they never disagree.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

CARRIERS = [
    {"id": "C1", "name": "Deccan Express Logistics", "phone": "+91 98490 22713", "bank": "HDFC A/c 5010 0033 4471"},
    {"id": "C2", "name": "Ganga Freight", "phone": "+91 94150 61182", "bank": "SBI A/c 3318 7740 0938"},
    {"id": "C3", "name": "Sahyadri Roadlines", "phone": "+91 98220 47306", "bank": "ICICI A/c 0291 0517 5520"},
]

CHAINS = [
    "Ratnadeep", "Vijetha", "Spencer's", "More Retail", "Big Basket", "Reliance Smart",
    "DMart", "Star Bazaar", "Nilgiris", "Spar", "Easyday",
]
KIRANA = "Kirana network"

# Contract clauses every customer contract references (ids and texts as in the UI concepts).
CLAUSES = [
    {
        "clause_id": "MSA-7.2", "kind": "late_delivery",
        "text": "Late delivery: 1.5% of invoice value per day late, capped at 10% of invoice.",
        "params": {"rate_per_day": 0.015, "cap": 0.10},
    },
    {
        "clause_id": "MSA-9.1", "kind": "fill_rate",
        "text": "Fill rate below 95% for a listed SKU in a week: 2% of that week's SKU invoice value.",
        "params": {"min_fill_rate": 0.95, "penalty": 0.02},
    },
    {
        "clause_id": "MSA-14.3", "kind": "force_majeure",
        "text": "Force majeure (declared weather events): penalties waived if the customer is notified within 6 hours.",
        "params": {"notify_within_hours": 6},
    },
]


def clean_name(raw: str) -> str:
    """'NARMADA BEVERAGES  ' -> 'Narmada Beverages' (collapse spaces, title case)."""
    return " ".join(raw.split()).title()


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


@dataclass
class Network:
    warehouses: list[dict]
    zones: list[dict]
    suppliers: list[dict]
    products: list[dict]

    @classmethod
    def load(cls, network_dir: Path) -> Network:
        wh = [
            {"id": r["dc_id"], "city": r["dc_city"], "lat": float(r["lat"]), "lon": float(r["lon"])}
            for r in read_csv(network_dir / "warehouses.csv")
        ]
        zones = [
            {
                "id": r["zone_id"], "city": r["city"], "state": r["state"], "lat": float(r["lat"]),
                "lon": float(r["lon"]), "serving_dc": r["serving_dc"], "road_km": float(r["road_km"]),
                "std_transit_days": int(r["std_transit_days"]),
            }
            for r in read_csv(network_dir / "delivery_zones.csv")
        ]
        sup = [
            {
                "id": r["supplier_id"], "name": clean_name(r["supplier"]), "category": r["category"],
                "hub": r["hub"], "state": r["state"], "lat": float(r["lat"]), "lon": float(r["lon"]),
                "quoted_lead_days": int(r["quoted_lead_days"]),
            }
            for r in read_csv(network_dir / "suppliers.csv")
        ]
        prod = [
            {
                "sku": r["sku"], "category": r["category"], "supplier_id": r["supplier_id"],
                "unit_cost": float(r["unit_cost"]), "unit_price": float(r["unit_price"]),
                "case_qty": int(r["case_qty"]),
            }
            for r in read_csv(network_dir / "products.csv")
        ]
        return cls(wh, zones, sup, prod)

    def customers(self) -> list[dict]:
        """A customer is a chain at a city. Every city has the Kirana network plus four chains,
        picked deterministically so the list is the same on every run."""
        out = []
        for z in self.zones:
            ranked = sorted(CHAINS, key=lambda c: hashlib.sha256(f"{c}|{z['id']}".encode()).hexdigest())
            for chain in [KIRANA, *ranked[:4]]:
                out.append({"chain": chain, "zone_id": z["id"], "name": f"{chain} ({z['city']})"})
        return out
