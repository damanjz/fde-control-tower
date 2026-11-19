"""`python -m tower.seed`: load the network, carriers, customers and one contract per customer. Idempotent."""

from __future__ import annotations

import hashlib
import json

import psycopg

from tower.config import Settings, load_settings
from tower.db import apply_schema
from tower.network import CARRIERS, CLAUSES, Network


def seed(settings: Settings) -> dict[str, int]:
    url = settings.db_url
    apply_schema(url)
    net = Network.load(settings.network_dir)
    with psycopg.connect(url) as conn:
        for w in net.warehouses:
            conn.execute(
                "INSERT INTO warehouse (id, city, lat, lon) VALUES (%(id)s, %(city)s, %(lat)s, %(lon)s) "
                "ON CONFLICT (id) DO UPDATE SET city = EXCLUDED.city, lat = EXCLUDED.lat, lon = EXCLUDED.lon",
                w,
            )
        for z in net.zones:
            conn.execute(
                "INSERT INTO zone (id, city, state, lat, lon, serving_dc, road_km, std_transit_days) "
                "VALUES (%(id)s, %(city)s, %(state)s, %(lat)s, %(lon)s, %(serving_dc)s, %(road_km)s, %(std_transit_days)s) "
                "ON CONFLICT (id) DO UPDATE SET city = EXCLUDED.city, state = EXCLUDED.state, lat = EXCLUDED.lat, "
                "lon = EXCLUDED.lon, serving_dc = EXCLUDED.serving_dc, road_km = EXCLUDED.road_km, "
                "std_transit_days = EXCLUDED.std_transit_days",
                z,
            )
        for c in CARRIERS:
            conn.execute(
                "INSERT INTO carrier (id, name, dispatch_phone, bank_account) VALUES (%(id)s, %(name)s, %(phone)s, %(bank)s) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, dispatch_phone = EXCLUDED.dispatch_phone, "
                "bank_account = EXCLUDED.bank_account",
                c,
            )
        for s in net.suppliers:
            conn.execute(
                "INSERT INTO supplier (id, name, category, hub, state, lat, lon, quoted_lead_days) "
                "VALUES (%(id)s, %(name)s, %(category)s, %(hub)s, %(state)s, %(lat)s, %(lon)s, %(quoted_lead_days)s) "
                "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, category = EXCLUDED.category, hub = EXCLUDED.hub, "
                "state = EXCLUDED.state, lat = EXCLUDED.lat, lon = EXCLUDED.lon, quoted_lead_days = EXCLUDED.quoted_lead_days",
                s,
            )
        for p in net.products:
            conn.execute(
                "INSERT INTO product (sku, category, supplier_id, unit_cost, unit_price, case_qty) "
                "VALUES (%(sku)s, %(category)s, %(supplier_id)s, %(unit_cost)s, %(unit_price)s, %(case_qty)s) "
                "ON CONFLICT (sku) DO UPDATE SET category = EXCLUDED.category, supplier_id = EXCLUDED.supplier_id, "
                "unit_cost = EXCLUDED.unit_cost, unit_price = EXCLUDED.unit_price, case_qty = EXCLUDED.case_qty",
                p,
            )
        for c in net.customers():
            cid = conn.execute(
                "INSERT INTO customer (chain, zone_id, name) VALUES (%(chain)s, %(zone_id)s, %(name)s) "
                "ON CONFLICT (chain, zone_id) DO UPDATE SET name = EXCLUDED.name RETURNING id",
                c,
            ).fetchone()[0]
            contract_id = conn.execute(
                "INSERT INTO contract (customer_id, contract_no, effective_from, effective_to) "
                "VALUES (%s, %s, '2026-04-01', '2027-03-31') "
                "ON CONFLICT (customer_id) DO UPDATE SET contract_no = EXCLUDED.contract_no RETURNING id",
                (cid, f"TD-MSA-{cid:04d}"),
            ).fetchone()[0]
            for cl in CLAUSES:
                conn.execute(
                    "INSERT INTO contract_clause (contract_id, clause_id, kind, text, params) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (contract_id, clause_id) "
                    "DO UPDATE SET kind = EXCLUDED.kind, text = EXCLUDED.text, params = EXCLUDED.params",
                    (contract_id, cl["clause_id"], cl["kind"], cl["text"], json.dumps(cl["params"])),
                )
        seed_capacity(conn, net)
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608 - fixed table names
            for t in ("warehouse", "zone", "carrier", "supplier", "product", "customer", "contract", "contract_clause",
                      "fleet_capacity", "dc_stock")
        }
    return counts


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


def seed_capacity(conn, net: Network) -> None:
    """Deterministic spare trucks per DC and carrier, and stock per DC and SKU (reset to these values on every seed).

    DC-KOL is short of trucks. The late supplier's SKUs (Prakash) are nearly out at Mumbai and Delhi and plentiful
    at Hyderabad and Kolkata, so a stock transfer has somewhere to come from."""
    prakash = {s["id"] for s in net.suppliers if "prakash" in s["name"].lower()}
    for w in net.warehouses:
        for c in CARRIERS:
            trucks = 1 + _h("fleet", w["id"], c["id"]) % 2 if w["id"] == "DC-KOL" else 4 + _h("fleet", w["id"], c["id"]) % 6
            conn.execute("INSERT INTO fleet_capacity (dc_id, carrier_id, trucks_free) VALUES (%s, %s, %s) "
                         "ON CONFLICT (dc_id, carrier_id) DO UPDATE SET trucks_free = EXCLUDED.trucks_free", (w["id"], c["id"], trucks))
        for p in net.products:
            if p["supplier_id"] in prakash:
                cases = 8 + _h("stock", w["id"], p["sku"]) % 22 if w["id"] in ("DC-MUM", "DC-DEL") else 180 + _h("stock", w["id"], p["sku"]) % 220
            else:
                cases = 60 + _h("stock", w["id"], p["sku"]) % 340
            conn.execute("INSERT INTO dc_stock (dc_id, sku, cases) VALUES (%s, %s, %s) "
                         "ON CONFLICT (dc_id, sku) DO UPDATE SET cases = EXCLUDED.cases", (w["id"], p["sku"], cases))


def main() -> None:
    counts = seed(load_settings())
    print("seeded: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
