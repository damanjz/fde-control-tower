"""Postgres helpers (sync psycopg 3: no event-loop issues on Windows)."""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
MIGRATIONS_DIR = SCHEMA_PATH.parent / "migrations"

# Operational tables cleared by a scenario reset. The network, customers, contracts and audit_log stay.
OPERATIONAL_TABLES = (
    "shipment_line",
    "shipment",
    "gps_ping",
    "disruption_event",
    "feed_health",
    "dead_letter",
    "ingest_log",
    "outbox",
)


def open_pool(url: str, size: int = 16) -> ConnectionPool:
    pool = ConnectionPool(url, min_size=2, max_size=size, kwargs={"row_factory": dict_row}, open=False, timeout=15)
    pool.open(wait=True, timeout=15)
    return pool


def swap_database(url: str, dbname: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{dbname}"


def ensure_database(url: str, dbname: str, *, recreate: bool = False) -> str:
    """Create (or recreate) a database next to the one in `url`; return its URL."""
    with psycopg.connect(swap_database(url, "postgres"), autocommit=True, connect_timeout=3) as conn:
        if recreate:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    return swap_database(url, dbname)


def drop_database(url: str, dbname: str) -> None:
    with psycopg.connect(swap_database(url, "postgres"), autocommit=True, connect_timeout=3) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))


def apply_schema(url: str) -> list[str]:
    """Apply db/schema.sql, then every db/migrations/*.sql in name order. All idempotent. Returns the file names."""
    files = [SCHEMA_PATH, *sorted(MIGRATIONS_DIR.glob("*.sql"))]
    with psycopg.connect(url, autocommit=True, connect_timeout=3) as conn:
        for f in files:
            conn.execute(f.read_text(encoding="utf-8"))
    return [f.name for f in files]


def reset_operational(url: str) -> None:
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        conn.execute("TRUNCATE " + ", ".join(OPERATIONAL_TABLES) + " RESTART IDENTITY CASCADE")
