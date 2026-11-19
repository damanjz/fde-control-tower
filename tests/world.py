"""Builders for milestone 2 DB tests: shipments, incidents, notifications written straight into the test database."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from tower.clock import IST

NOW = datetime(2026, 11, 13, 10, 40, tzinfo=IST)
T0 = datetime(2026, 11, 13, 5, 0, tzinfo=IST)


def customer(conn, chain: str, city: str) -> int:
    return conn.execute("SELECT id FROM customer WHERE chain = %s AND name = %s", (chain, f"{chain} ({city})")).fetchone()["id"]


def ship(conn, sid: str, *, dc="DC-HYD", city="Nagpur", carrier="C1", chain="Kirana network", value=1_000_000, cases=100,
         departed=T0, eta=None, promised=None, status="in_transit", lines=None, driver=None) -> str:
    z = conn.execute("SELECT id, serving_dc FROM zone WHERE city = %s", (city,)).fetchone()
    cid = customer(conn, chain, city)
    eta = eta or NOW + timedelta(hours=10)
    promised = promised or eta + timedelta(hours=12)
    conn.execute(
        """INSERT INTO shipment (id, order_no, dc_id, zone_id, carrier_id, customer_id, cases, value_inr, status, departed_at,
               eta, promised_at, source, content_hash, source_exported_at, driver_name, driver_phone, vehicle_no, bol_no)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'erp',%s,%s,%s,%s,%s,%s)""",
        (sid, f"SO-{sid}", dc, z["id"], carrier, cid, cases, value, status, departed, eta, promised,
         hashlib.sha256(sid.encode()).hexdigest(), T0, *(driver or (None, None, None, None))))
    for sku, n, v in (lines or [("PE-001", cases, value)]):
        conn.execute("INSERT INTO shipment_line (shipment_id, sku, cases, value_inr) VALUES (%s,%s,%s,%s)", (sid, sku, n, v))
    return sid


def incident(conn, ext: str, kind: str, title: str, detail: str, severity="high", started=T0, **scope) -> int:
    cols = {"lat": None, "lon": None, "radius_km": None, "dc_id": None, "carrier_id": None, "supplier_id": None, "states": None}
    cols.update(scope)
    return conn.execute(
        """INSERT INTO disruption_event (source, external_id, kind, title, detail, lat, lon, radius_km, dc_id, carrier_id,
               supplier_id, states, severity, started_at) VALUES ('test',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (ext, kind, title, detail, cols["lat"], cols["lon"], cols["radius_km"], cols["dc_id"], cols["carrier_id"],
         cols["supplier_id"], cols["states"], severity, started)).fetchone()["id"]


def notify(conn, incident_id: int, cust_id: int, at: datetime) -> None:
    conn.execute("INSERT INTO customer_notification (incident_id, customer_id, notified_at) VALUES (%s,%s,%s)",
                 (incident_id, cust_id, at))


def run_sql(pool, sql: str, params=()) -> None:
    """A committed write from a separate connection (the `conn` fixture keeps a transaction open)."""
    with pool.connection() as c:
        c.execute(sql, params)
