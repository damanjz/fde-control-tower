"""Carrier poller degradation, alerts idempotency and simulator determinism."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import pytest

from sim.alerts_data import scenario_alerts
from sim.erp_writer import ErpSim
from sim.world import World
from tests.helpers import row, write_export
from tower.alerts import ingest_alerts
from tower.carrier import CarrierPoller
from tower.config import load_settings
from tower.erp import process_file

NETWORK = Path(__file__).resolve().parent.parent / "data" / "network"


@pytest.mark.db
def test_carrier_poller_degrades_gracefully_and_recovers(pool, settings, clock, conn, tmp_path):
    process_file(pool, settings, clock, write_export(tmp_path / "in", "erp_export_20261113_0500.csv", [row("SH-1001")]))
    state = {"mode": "ok"}
    mark = conn.execute("SELECT COALESCE(max(id), 0) AS n FROM audit_log").fetchone()["n"]

    def handler(req: httpx.Request) -> httpx.Response:
        if state["mode"] == "down":
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json={"carrier": "C2", "as_of": clock.now().isoformat(),
                                         "consignments": [{"ref": "SH-1001", "status": "in_transit", "eta": None}]})

    client = httpx.Client(base_url="http://carrier", transport=httpx.MockTransport(handler))
    poller = CarrierPoller(pool, settings, clock, "C2", threading.Event(), client=client)
    assert poller.poll_once() == "ok"
    assert conn.execute("SELECT carrier_status FROM shipment").fetchone()["carrier_status"] == "in_transit"

    state["mode"] = "down"
    assert [poller.poll_once() for _ in range(3)] == ["failed"] * 3
    assert poller.breaker.state == "open"
    assert poller.poll_once() == "skipped"                       # no call while open
    h = conn.execute("SELECT * FROM feed_health WHERE feed = 'carrier:C2'").fetchone()
    assert h["circuit_state"] == "open" and "last known" in h["last_error"]
    assert conn.execute("SELECT carrier_status FROM shipment").fetchone()["carrier_status"] == "in_transit"   # kept

    state["mode"] = "ok"
    time.sleep(0.08)                                             # cooldown passes, probe goes out
    assert poller.poll_once() == "ok" and poller.breaker.state == "closed"
    h = conn.execute("SELECT * FROM feed_health WHERE feed = 'carrier:C2'").fetchone()
    assert h["circuit_state"] == "closed" and h["last_error"] is None
    moves = [r["detail"]["to"] for r in conn.execute("SELECT detail FROM audit_log WHERE target = 'carrier:C2' AND id > %s ORDER BY id", (mark,)).fetchall()]
    assert moves == ["open", "half_open", "closed"]


@pytest.mark.db
def test_alerts_are_ingested_idempotently(pool, clock, conn):
    alerts = scenario_alerts("S036")
    first = ingest_alerts(pool, alerts, clock.now())
    second = ingest_alerts(pool, alerts, clock.now())
    assert first["new"] == 6 and second["new"] == 0 and second["duplicate"] == 6
    assert conn.execute("SELECT count(*) AS n FROM disruption_event").fetchone()["n"] == 6
    sev = {r["severity"] for r in conn.execute("SELECT severity FROM disruption_event").fetchall()}
    assert sev <= {"low", "medium", "high", "critical"}


@pytest.mark.db
def test_alert_with_no_scope_is_dead_lettered(pool, clock, conn):
    bad = {"alert_id": "X1", "kind": "weather", "title": "t", "detail": "d", "severity": "red", "issued_at": clock.now().isoformat()}
    assert ingest_alerts(pool, [bad], clock.now())["dead_letter"] == 1
    assert conn.execute("SELECT reason_code FROM dead_letter").fetchone()["reason_code"] == "no_scope"


def test_simulator_is_deterministic_and_dirty(tmp_path):
    world = World(NETWORK, 20261113)
    s = load_settings()
    assert len(world.shipments) == 347 and 10e7 < sum(x.value for x in world.shipments) < 20e7
    from sim.scenario import EXPORT_TIMES
    a = ErpSim(world, 5).write_export(EXPORT_TIMES[0], tmp_path / "a")[0].read_bytes()
    b = ErpSim(World(NETWORK, 20261113), 5).write_export(EXPORT_TIMES[0], tmp_path / "b")[0].read_bytes()
    assert a == b and s is not None
    text = a.decode("utf-8")
    assert text.startswith("﻿") and "\r\n" in text and "\n" in text.replace("\r\n", "")


@pytest.mark.db
def test_restart_does_not_keep_a_stale_open_circuit(pool, settings, clock, conn):
    """Chaos-drill finding 1: the circuit was open when the old process died. A new process starts with a closed breaker,
    and /health/feeds must say so at startup and after every successful poll."""
    conn.execute("INSERT INTO feed_health (feed, received, accepted, rejected, last_at, last_error, circuit_state) "
                 "VALUES ('carrier:C3', 5, 2, 3, %s, 'circuit open: serving last known status (last fresh 07:40)', 'open')",
                 (clock.now(),))
    conn.commit()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"carrier": "C3", "as_of": clock.now().isoformat(), "consignments": []})

    client = httpx.Client(base_url="http://carrier", transport=httpx.MockTransport(handler))
    stop = threading.Event()
    stop.set()                                                   # run() syncs the state, then exits without polling
    poller = CarrierPoller(pool, settings, clock, "C3", stop, client=client)
    poller.start()
    poller.join(5)
    h = conn.execute("SELECT * FROM feed_health WHERE feed = 'carrier:C3'").fetchone()
    assert h["circuit_state"] == "closed" and h["last_error"] is None and h["received"] == 5
    assert conn.execute("SELECT detail FROM audit_log WHERE target = 'carrier:C3' ORDER BY id DESC LIMIT 1").fetchone()["detail"] \
        == {"from": "open", "to": "closed", "why": "restart"}

    conn.execute("UPDATE feed_health SET circuit_state = 'open' WHERE feed = 'carrier:C3'")   # a lost write, say
    conn.commit()
    assert poller.poll_once() == "ok"
    assert conn.execute("SELECT circuit_state FROM feed_health WHERE feed = 'carrier:C3'").fetchone()["circuit_state"] == "closed"
