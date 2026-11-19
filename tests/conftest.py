"""Shared fixtures. DB tests run against a throwaway `ct_test` database on the compose Postgres and are
skipped cleanly when it is not running."""

from __future__ import annotations

import os
from datetime import datetime

import psycopg
import pytest

from tower.clock import IST, FixedClock
from tower.config import load_settings
from tower.db import apply_schema, drop_database, ensure_database, open_pool, reset_operational
from tower.seed import seed

NOW = datetime(2026, 11, 13, 8, 0, tzinfo=IST)


def pytest_collection_modifyitems(config, items):
    """@pytest.mark.llm tests drive the real local model: skipped unless CT_LLM_TESTS=1."""
    if os.environ.get("CT_LLM_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set CT_LLM_TESTS=1 to run tests that call the local LLM")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def test_db_url():
    base = load_settings()
    try:
        url = ensure_database(base.db_url, "ct_test", recreate=True)
    except (psycopg.OperationalError, RuntimeError) as e:
        pytest.skip(f"compose Postgres is not available: {type(e).__name__}")
    apply_schema(url)
    seed(load_settings(database_url=url))
    yield url
    drop_database(base.db_url, "ct_test")


@pytest.fixture()
def settings(test_db_url, tmp_path):
    return load_settings(
        database_url=test_db_url, gps_secret="test-gps-secret", admin_key="test-admin", carrier_api_key="k",
        carrier_admin_key="ka", token_asha="test-asha", token_kiran="test-kiran", token_meera="test-meera",
        data_dir=tmp_path, workers=False, carrier_retries=0, breaker_threshold=3,
        breaker_cooldown_s=0.05, breaker_max_cooldown_s=0.2,
    )


@pytest.fixture()
def clock():
    return FixedClock(NOW)


@pytest.fixture()
def pool(test_db_url):
    reset_operational(test_db_url)
    p = open_pool(test_db_url, size=4)
    yield p
    p.close()


@pytest.fixture()
def conn(pool):
    with pool.connection() as c:
        yield c


@pytest.fixture()
def client(settings, clock, pool):
    from fastapi.testclient import TestClient

    from tower.main import create_app

    with TestClient(create_app(settings, clock=clock, start_workers=False)) as c:
        yield c


@pytest.fixture(scope="session")
def msa_loaded(test_db_url, tmp_path_factory):
    """The 12 contract PDFs generated (reportlab interpreter) and ingested into ct_test without embeddings."""
    import tower.contracts as contracts
    from sim.pdf_sim import has_reportlab, write_contracts

    if not has_reportlab():
        pytest.skip("the reportlab interpreter is not available")
    s = load_settings(database_url=test_db_url, data_dir=tmp_path_factory.mktemp("pdfdata"))
    write_contracts(s)
    pool = open_pool(test_db_url, size=2)
    try:
        with pool.connection() as conn:
            conn.execute("DELETE FROM dead_letter WHERE feed = 'contracts'")
        res = contracts.ingest_all(pool, s, embed=False)
    finally:
        pool.close()
    return res


H1_DRIVER = ("Ravi Kumar", "+91 98765 43210", "TS09AB1234", "BOL-1001-111")
AS_ASHA = {"Authorization": "Bearer test-asha"}
AS_KIRAN = {"Authorization": "Bearer test-kiran"}
AS_MEERA = {"Authorization": "Bearer test-meera"}


@pytest.fixture()
def world(pool, msa_loaded):
    """Shipments across three DCs and two incidents: a big weather event touching all of them, and a Kolkata DC outage."""
    from datetime import timedelta

    from tests.world import NOW as WNOW
    from tests.world import T0, incident, ship
    from tower import risk

    with pool.connection() as c:
        c.execute("UPDATE fleet_capacity SET trucks_free = 10")
        ids = {
            "H1": ship(c, "SH-9101", city="Nagpur", carrier="C1", value=400_000, cases=100, driver=H1_DRIVER),
            "H2": ship(c, "SH-9102", city="Nagpur", carrier="C1", value=300_000, cases=100),
            "H3": ship(c, "SH-9103", city="Nagpur", carrier="C3", value=300_000, cases=100),
            "K1": ship(c, "SH-9201", dc="DC-KOL", city="Patna", carrier="C2", value=500_000, cases=150,
                       departed=T0 + timedelta(hours=1), driver=("Imran Das", "+91 91234 56789", "WB02XY9999", "BOL-1002-222")),
            "K2": ship(c, "SH-9202", dc="DC-KOL", city="Kolkata", carrier="C2", value=200_000, cases=80, departed=T0 + timedelta(hours=1)),
            "D1": ship(c, "SH-9301", dc="DC-DEL", city="Lucknow", carrier="C2", value=350_000, cases=90,
                       driver=("Suresh Singh", "+91 94444 55555", "DL1CAB1111", "BOL-1003-333")),
        }
        geo = incident(c, "geo", "weather", "NH44 closed near Adilabad", "Heavy rain closed the road. Reopening estimate 26 hours.",
                       lat=21.0, lon=79.0, radius_km=3000)
        fac = incident(c, "fac", "facility", "Kolkata DC dock down", "Outbound at 40% capacity until the part arrives (ETA 18:00).",
                       started=T0 + timedelta(minutes=40), dc_id="DC-KOL")
    risk.recompute_all(pool, WNOW)
    return {"ships": ids, "geo": geo, "fac": fac}
