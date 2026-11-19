"""The eval world: a throwaway `ct_eval` database seeded like `ct_test`, and one small world per case.

Never the live `ct` database. Each case starts from an empty operational state (shipments, incidents, impact, playbooks and
agent runs are truncated), the seeded spare trucks and stock, its own overrides, its shipments, its incident and any
customer notifications; then the impact engine runs. The case's world spec in cases.jsonl is the whole input.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from tower import risk
from tower.clock import IST
from tower.config import ROOT, Settings, load_settings
from tower.db import apply_schema, ensure_database, open_pool, reset_operational
from tower.network import Network
from tower.seed import seed, seed_capacity

EVAL_DB = "ct_eval"
DEFAULT_NOW = datetime(2026, 11, 13, 10, 40, tzinfo=IST)
T0 = datetime(2026, 11, 13, 5, 0, tzinfo=IST)
WORLD_DIR = Path(__file__).resolve().parent / ".world"     # generated contract PDFs when data/contracts is empty


def eval_url() -> str:
    from tower.db import swap_database
    return swap_database(load_settings().db_url, EVAL_DB)


def eval_settings(url: str | None = None) -> Settings:
    return load_settings(database_url=url or eval_url(), workers=False)


def _contracts_dir(settings: Settings) -> Path:
    live = ROOT / "data" / "contracts"
    if len(list(live.glob("MSA_*.pdf"))) >= 12:
        return live                       # read only: ingest never moves or changes the PDFs
    from sim.pdf_sim import write_contracts
    s = settings.model_copy(update={"data_dir": WORLD_DIR})
    write_contracts(s)
    return s.contracts_dir


def ensure_world(*, rebuild: bool = False, embed: bool = True, log=print) -> str:
    """Create and seed ct_eval when needed, ingest the 12 contract PDFs (with embeddings when Ollama answers). Returns the URL."""
    from tower import contracts

    base = load_settings()
    url = ensure_database(base.db_url, EVAL_DB, recreate=rebuild)
    apply_schema(url)
    settings = eval_settings(url)
    seed(settings)
    pool = open_pool(url, size=2)
    try:
        with pool.connection() as conn:
            docs = conn.execute("SELECT count(*) AS n FROM msa_document").fetchone()["n"]
            missing_vec = conn.execute("SELECT count(*) AS n FROM contract_chunk WHERE embedding IS NULL").fetchone()["n"]
        if docs < 12 or (embed and missing_vec):
            log(f"ingesting contracts into {EVAL_DB} (embeddings: {embed})")
            contracts.ingest_all(pool, settings, directory=_contracts_dir(settings), embed=embed)
    finally:
        pool.close()
    return url


# ------------------------------------------------------------------ one case's world
def _at(value, now: datetime, default: datetime) -> datetime:
    if value is None:
        return default
    if isinstance(value, int | float):
        return now + timedelta(hours=value)
    return datetime.fromisoformat(value)


def customer_id(conn, chain: str, city: str) -> int:
    row = conn.execute("SELECT id FROM customer WHERE chain = %s AND name = %s", (chain, f"{chain} ({city})")).fetchone()
    if row is None:
        raise ValueError(f"no customer {chain} in {city}")
    return row["id"]


def _ship(conn, s: dict, now: datetime) -> None:
    z = conn.execute("SELECT id FROM zone WHERE city = %s", (s["city"],)).fetchone()
    if z is None:
        raise ValueError(f"unknown city {s['city']}")
    cid = customer_id(conn, s.get("chain", "Kirana network"), s["city"])
    eta = _at(s.get("eta_h", 10), now, now)
    promised = _at(s.get("promised_h"), now, eta + timedelta(hours=12))
    departed = _at(s.get("departed"), now, T0)
    value, cases = s["value"], s.get("cases", 100)
    driver = s.get("driver") or [None, None, None, None]
    conn.execute(
        """INSERT INTO shipment (id, order_no, dc_id, zone_id, carrier_id, customer_id, cases, value_inr, status, departed_at,
               eta, promised_at, source, content_hash, source_exported_at, driver_name, driver_phone, vehicle_no, bol_no)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'erp',%s,%s,%s,%s,%s,%s)""",
        (s["id"], f"SO-{s['id']}", s.get("dc", "DC-HYD"), z["id"], s.get("carrier", "C1"), cid, cases, value,
         s.get("status", "in_transit"), departed, eta, promised, hashlib.sha256(s["id"].encode()).hexdigest(), T0, *driver))
    for sku, n, v in s.get("lines") or [("PE-001", cases, value)]:
        conn.execute("INSERT INTO shipment_line (shipment_id, sku, cases, value_inr) VALUES (%s,%s,%s,%s)", (s["id"], sku, n, v))


def _incident(conn, ev: dict, now: datetime, ext: str) -> int:
    cols = {k: ev.get(k) for k in ("lat", "lon", "radius_km", "dc_id", "carrier_id", "supplier_id", "states")}
    return conn.execute(
        """INSERT INTO disruption_event (source, external_id, kind, title, detail, lat, lon, radius_km, dc_id, carrier_id,
               supplier_id, states, severity, started_at) VALUES ('eval',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (ext, ev["kind"], ev["title"], ev["detail"], cols["lat"], cols["lon"], cols["radius_km"], cols["dc_id"],
         cols["carrier_id"], cols["supplier_id"], cols["states"], ev.get("severity", "high"),
         _at(ev.get("started"), now, T0))).fetchone()["id"]


def build(pool, url: str, settings: Settings, world: dict) -> dict:
    """Reset ct_eval's operational state and build one case's world. Returns {incident_id, now}."""
    if not url.rstrip("/").endswith("/" + EVAL_DB):
        raise RuntimeError(f"refusing to build an eval world outside {EVAL_DB}")
    now = _at(world.get("now"), DEFAULT_NOW, DEFAULT_NOW)
    reset_operational(url)
    with pool.connection() as conn:
        seed_capacity(conn, Network.load(settings.network_dir))
        for key, n in (world.get("fleet") or {}).items():
            if key == "*":
                conn.execute("UPDATE fleet_capacity SET trucks_free = %s", (n,))
            else:
                dc, carrier = key.split("/")
                conn.execute("UPDATE fleet_capacity SET trucks_free = %s WHERE dc_id = %s AND carrier_id = %s", (n, dc, carrier))
        for key, n in (world.get("stock") or {}).items():
            dc, sku = key.split("/")
            conn.execute("UPDATE dc_stock SET cases = %s WHERE dc_id = %s AND sku = %s", (n, dc, sku))
        for s in world["shipments"]:
            _ship(conn, s, now)
        iid = _incident(conn, world["incident"], now, "main")
        for i, ev in enumerate(world.get("other_incidents") or [], start=1):
            _incident(conn, ev, now, f"other-{i}")
        for n in world.get("notifications") or []:
            conn.execute("INSERT INTO customer_notification (incident_id, customer_id, notified_at) VALUES (%s,%s,%s)",
                         (iid, customer_id(conn, n["chain"], n["city"]), _at(n["at"], now, now)))
    risk.recompute_all(pool, now)
    return {"incident_id": iid, "now": now}
