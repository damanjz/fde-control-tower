"""Scenario driver: runs the three push/drop channels and the fault schedule against a running tower.

The alerts feed is pulled by the tower from the mock carrier API, which follows the same scenario clock.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx

from sim.erp_writer import ErpSim
from sim.gps_sender import GpsSender
from sim.world import SCENARIO_END, SCENARIO_START, World
from tower.clock import IST, ScenarioClock, write_clock
from tower.config import Settings


def at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, 13, h, m, tzinfo=IST)


# (scenario time, carrier, fault body). An empty body clears that carrier's fault.
FAULT_SCHEDULE: list[tuple[datetime, str, dict]] = [
    (at(6, 30), "C2", {"outage": True}),
    (at(7, 30), "C2", {}),
    (at(7, 45), "C1", {"error_rate": 0.85}),
    (at(8, 30), "C1", {}),
    (at(8, 35), "C3", {"timeout": True}),
    (at(10, 5), "C3", {}),
]
EXPORT_TIMES = [at(h) for h in range(5, 11)]   # hourly ERP exports 05:00 ... 10:00


def make_clock(wall_seconds: float, start: datetime = SCENARIO_START, end: datetime = SCENARIO_END,
               lead_s: float = 2.0) -> ScenarioClock:
    factor = (end - start).total_seconds() / wall_seconds
    return ScenarioClock(start, factor, wall_start=time.time() + lead_s)


class Scenario:
    def __init__(self, settings: Settings, world: World, clock: ScenarioClock, *, tick_s: float = 3.0,
                 end: datetime = SCENARIO_END, on_tick: Callable[[], None] | None = None):
        self.settings, self.world, self.clock, self.end, self.tick_s, self.on_tick = settings, world, clock, end, tick_s, on_tick
        self.erp = ErpSim(world, seed=settings.scenario_seed)
        self.gps = GpsSender(world, clock, f"http://{settings.host}:{settings.port}", settings.gps_secret)
        self.exports_done = 0
        self.faults_done = 0
        self.fault_client = httpx.Client(base_url=settings.carrier_api_url, timeout=5,
                                         headers={"X-Admin-Key": settings.carrier_admin_key})
        self.erp_files: list[Path] = []

    def set_fault(self, carrier: str, body: dict) -> None:
        self.fault_client.put("/admin/faults", json={"carrier": carrier, **body}).raise_for_status()

    def run(self) -> None:
        write_clock(self.settings.clock_file, self.clock)
        next_tick = 0.0
        while True:
            now = self.clock.now()
            while self.exports_done < len(EXPORT_TIMES) and now >= EXPORT_TIMES[self.exports_done]:
                path, _ = self.erp.write_export(EXPORT_TIMES[self.exports_done], self.settings.inbox)
                self.erp_files.append(path)
                self.exports_done += 1
            while self.faults_done < len(FAULT_SCHEDULE) and now >= FAULT_SCHEDULE[self.faults_done][0]:
                _, carrier, body = FAULT_SCHEDULE[self.faults_done]
                self.set_fault(carrier, body)
                self.faults_done += 1
            if time.monotonic() >= next_tick:
                self.gps.tick()
                next_tick = time.monotonic() + self.tick_s
            if self.on_tick:
                self.on_tick()
            if now >= self.end:
                break
            time.sleep(0.2)
        self.fault_client.delete("/admin/faults")
