"""`python -m sim.drill`: a chaos drill. One scripted bad morning against an isolated copy of the stack, measured.

Isolation: its own database (`ct_drill`, created next to the live one and dropped at the end), its own ports (tower 8601,
carrier mock 9201), its own data folder (a work dir with a copy of data/network), and no model calls of any kind
(the LLM URL points at a dead port; contracts are not ingested, so nothing embeds). The scenario clock is parked
(factor 0) and moved by hand between phases, so the run does not depend on wall time except for the breaker timings,
which are measured in wall seconds.

What it injects, in order: a carrier outage (C1 100%), two malformed ERP exports and a duplicate file, then 30% flaky
for every carrier, bad bills of lading, a GPS burst (duplicates, out of order, clock skew, bad pings, forged
signatures), a second carrier outage (C3) that heals while the tower is down, and the tower process killed and
restarted in the middle of ERP and GPS ingest. Then it clears the faults, replays the fixable dead letters with their
suggested fixes, and re-drops the same files to prove the state does not move.

Deterministic for a given --seed (what is injected and what the tower decides). The breaker timings and the number of
failed carrier polls depend on timing and vary by a few seconds / polls.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

from sim.demo import fingerprint
from sim.erp_writer import ErpSim, write_malformed_export
from sim.gps_sender import GpsSender
from sim.pdf_sim import write_bols
from sim.scenario import at
from sim.world import World
from tower.clock import ScenarioClock, write_clock
from tower.config import ROOT, load_settings
from tower.db import apply_schema, drop_database, ensure_database
from tower.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign
from tower.seed import seed as seed_db

DB_NAME = "ct_drill"
TOWER_PORT = 8601
CARRIER_PORT = 9201
LIVE_PORTS = {8500, 9100, 4400}
DEFAULT_WORKDIR = Path(os.environ.get("CT_DRILL_DIR") or Path(tempfile.gettempdir()) / "ct_drill")
CARRIERS = ("C1", "C2", "C3")
BATCH = 20
RESULTS = ("applied", "unchanged", "stale", "failed")   # what a replay did (the API's `result`)


# ---------------------------------------------------------------------------------------------- GPS burst (pure)
@dataclass
class Burst:
    batches: list[list[dict]]
    valid_ids: set[str]
    stats: Counter


def build_burst(world: World, ships: list, now: datetime, seed: int, *, per_ship: int = 4, dup_same: int = 15,
                dup_later: int = 15, skewed: int = 12, bad: int = 4) -> Burst:
    """Pings for `ships` (older ones first in time, shuffled in send order), plus duplicates, future-dated pings and
    pings that fail validation. Pure: same inputs, same burst."""
    rng = random.Random(f"burst-{seed}")
    helper = GpsSender(world, None, "http://unused", "x")   # only its _ping() is used
    valid: list[dict] = []
    for s in ships:
        for k in range(per_ship):
            valid.append(helper._ping(s, now - timedelta(minutes=10 * (per_ship - 1 - k))))
    rng.shuffle(valid)
    seq: list[tuple[str, dict]] = [("valid", p) for p in valid]
    for p in rng.sample(valid, dup_same):
        seq.insert(seq.index(("valid", p)) + 1, ("dup", dict(p)))
    for p in rng.sample(valid, dup_later):
        pos = min(len(seq), seq.index(("valid", p)) + 3 * BATCH)
        seq.insert(pos, ("dup", dict(p)))
    for i in range(skewed):
        s = ships[i % len(ships)]
        p = helper._ping(s, now)
        p = {**p, "ping_id": f"{p['ping_id']}-skew{i}", "event_ts": (now + timedelta(minutes=rng.randrange(20, 46))).isoformat()}
        seq.insert(rng.randrange(len(seq) + 1), ("skewed", p))
    for i in range(bad):
        p = {**helper._ping(ships[i], now), "ping_id": f"{ships[i].id}-bad{i}", "lat": 999.0}
        seq.insert(rng.randrange(len(seq) + 1), ("bad", p))
    stats: Counter = Counter()
    newest: dict[str, str] = {}
    seen: set[str] = set()
    for kind, p in seq:
        stats[kind] += 1
        if kind in ("valid", "dup") and p["ping_id"] not in seen:   # first arrival of a valid ping
            seen.add(p["ping_id"])
            if p["shipment_id"] in newest and p["event_ts"] < newest[p["shipment_id"]]:
                stats["out_of_order"] += 1
            newest[p["shipment_id"]] = max(newest.get(p["shipment_id"], ""), p["event_ts"])
    pings = [p for _, p in seq]
    batches = [pings[i:i + BATCH] for i in range(0, len(pings), BATCH)]
    return Burst(batches, {p["ping_id"] for p in valid}, stats)


# ---------------------------------------------------------------------------------------------- output helpers
def table(head: list[str], rows: list[list]) -> str:
    cells = [[str(c) for c in r] for r in rows]
    w = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(head)]
    line = lambda r: "| " + " | ".join(c.ljust(w[i]) for i, c in enumerate(r)) + " |"  # noqa: E731
    return "\n".join([line(head), "|" + "|".join("-" * (x + 2) for x in w) + "|", *(line(r) for r in cells)])


def fmt_s(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f} s"


# ---------------------------------------------------------------------------------------------- observer
class Observer(threading.Thread):
    """Polls what the UI reads: /health/feeds every 0.25 s, and /live every 4th poll to compare the circuits."""

    def __init__(self, base: str, token: str, t0: float):
        super().__init__(name="observer", daemon=True)
        self.client = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=3)
        self.t0, self.stop_event = t0, threading.Event()
        self.samples: list[tuple[float, dict | None]] = []
        self.errors: Counter = Counter()
        self.compared = self.mismatched = 0

    @staticmethod
    def _circuits(feeds: dict) -> dict:
        return {f.split(":", 1)[1]: v[1] for f, v in feeds.items() if f.startswith("carrier:")}

    def _health(self) -> dict | None:
        try:
            r = self.client.get("/health/feeds")
        except httpx.HTTPError:
            self.errors["unreachable"] += 1
            return None
        if r.status_code != 200:
            self.errors[f"http_{r.status_code}"] += 1
            return None
        return {f["feed"]: (f["status"], f["circuit_state"]) for f in r.json()["feeds"]}

    def run(self) -> None:
        n = 0
        while not self.stop_event.is_set():
            before = self._health()
            self.samples.append((time.monotonic() - self.t0, before))
            n += 1
            if before is not None and n % 4 == 0:
                try:
                    live = self.client.get("/live").json().get("circuits", {})
                    after = self._health()
                    if after is not None and self._circuits(before) == self._circuits(after):
                        self.compared += 1
                        self.mismatched += int(live != self._circuits(after))
                except (httpx.HTTPError, ValueError):
                    self.errors["live_failed"] += 1
            self.stop_event.wait(0.25)

    def latest(self) -> dict | None:
        for _, f in reversed(self.samples):
            if f is not None:
                return f
        return None

    def first(self, feed: str, after: float, pred) -> float | None:
        for t, f in self.samples:
            if t >= after and f is not None and feed in f and pred(f[feed]):
                return t
        return None

    def circuit_log(self, feed: str) -> list[tuple[float, str]]:
        out: list[tuple[float, str]] = []
        for t, f in self.samples:
            if f is not None and feed in f and (not out or out[-1][1] != f[feed][1]):
                out.append((t, f[feed][1]))
        return out


# ---------------------------------------------------------------------------------------------- the drill
class Drill:
    def __init__(self, args):
        self.args = args
        self.workdir = Path(args.workdir)
        self.t0 = time.monotonic()
        self.events: list[tuple[float, str, str]] = []
        self.procs: dict[str, subprocess.Popen] = {}
        self.status_at: dict[str, dict] = {}
        self.gps_stats: Counter = Counter()
        self.R: dict = {}

    # ---- plumbing
    def ev(self, label: str) -> float:
        t = time.monotonic() - self.t0
        self.events.append((t, datetime.now().strftime("%H:%M:%S"), label))
        print(f"  [{t:6.1f}s] {label}", flush=True)
        return t

    def wait(self, pred, timeout: float, step: float = 0.1) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(step)
        return False

    def q(self, sql: str, params=None):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=None):
        return self.conn.execute(sql, params).fetchone()

    def setup(self) -> None:
        a = self.args
        base = load_settings()
        for p in (TOWER_PORT, CARRIER_PORT):
            if p in LIVE_PORTS or self._in_use(p):
                raise SystemExit(f"port {p} is in use or reserved: nothing started")
        self.db_url = ensure_database(base.db_url, DB_NAME, recreate=True)
        apply_schema(self.db_url)
        if self.workdir.exists():
            shutil.rmtree(self.workdir)
        data = self.workdir / "data"
        (data / "logs").mkdir(parents=True)
        (self.workdir / "stage").mkdir()
        shutil.copytree(ROOT / "data" / "network", data / "network")
        env = {"CT_DATABASE_URL": self.db_url, "CT_PORT": str(TOWER_PORT), "CT_CARRIER_API_PORT": str(CARRIER_PORT),
               "CT_CARRIER_API_URL": f"http://127.0.0.1:{CARRIER_PORT}",
               "CT_CHAT_WEBHOOK_URL": f"http://127.0.0.1:{CARRIER_PORT}/hooks/chat",
               "CT_DATA_DIR": str(data), "CT_LLM_URL": "http://127.0.0.1:9", "CT_SCENARIO_SEED": str(a.seed)}
        os.environ.update(env)
        self.s = s = load_settings()
        # the guard: refuse to run unless everything points at the drill's own resources
        assert s.db_url == self.db_url and s.db_url.rsplit("/", 1)[-1] == DB_NAME
        assert s.port == TOWER_PORT and s.carrier_api_port == CARRIER_PORT
        assert str(CARRIER_PORT) in s.carrier_api_url and str(CARRIER_PORT) in s.chat_webhook_url
        assert Path(s.data_dir).resolve() != (ROOT / "data").resolve() and "11434" not in s.llm_url
        seed_db(s)
        self.world = World(s.network_dir, a.seed)
        self.erp = ErpSim(self.world, seed=a.seed)
        self.conn = psycopg.connect(self.db_url, row_factory=dict_row, autocommit=True)
        self.set_clock(at(7, 30))
        self.tower_url = f"http://127.0.0.1:{TOWER_PORT}"
        self.carrier_url = f"http://127.0.0.1:{CARRIER_PORT}"
        self.admin = httpx.Client(base_url=self.carrier_url, headers={"X-Admin-Key": s.carrier_admin_key}, timeout=5)
        self.api = httpx.Client(base_url=self.tower_url, timeout=15)
        self.gps_client = httpx.Client(base_url=self.tower_url, timeout=15)
        self.sender = GpsSender(self.world, self.clock, self.tower_url, s.gps_secret)

    @staticmethod
    def _in_use(port: int) -> bool:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return True
        except OSError:
            return False

    def set_clock(self, when: datetime) -> None:
        self.clock = ScenarioClock(when, 0.0, wall_start=time.time())
        write_clock(self.s.clock_file, self.clock)
        time.sleep(1.4)   # the tower and the mock re-read the clock file at most once a second

    def start(self, name: str, module: str, health: str) -> None:
        log = open(self.s.data_dir / "logs" / f"{name}.log", "ab")  # noqa: SIM115 - closed with the process
        self.procs[name] = subprocess.Popen([sys.executable, "-m", module], cwd=ROOT, stdout=log, stderr=log)
        if not self.wait(lambda: self._up(health), 60, 0.3):
            raise SystemExit(f"{name} did not come up; see {self.s.data_dir / 'logs' / (name + '.log')}")

    @staticmethod
    def _up(url: str) -> bool:
        try:
            return httpx.get(url, timeout=1.5).status_code == 200
        except httpx.HTTPError:
            return False

    @staticmethod
    def kill_tree(proc: subprocess.Popen) -> None:
        """Hard kill of the process and its children (a venv python.exe is a launcher with the interpreter as its child)."""
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        proc.wait(timeout=10)

    def stop_all(self) -> None:
        for p in self.procs.values():
            if p.poll() is None:
                self.kill_tree(p)

    # ---- drops
    def drop(self, path: Path) -> Path:
        dest = self.s.inbox / path.name
        self.s.inbox.mkdir(parents=True, exist_ok=True)
        os.replace(path, dest)
        return dest

    def stage_export(self, hour: int) -> Path:
        path, _ = self.erp.write_export(at(hour), self.workdir / "stage")
        return path

    def inbox_empty(self) -> bool:
        return not list(self.s.inbox.glob("*.csv"))

    def malformed(self, minute_at: datetime, rows: int, seed: int) -> tuple[Path, list[str]]:
        path, _ = write_malformed_export(self.world, minute_at, self.workdir / "stage", rows=rows, seed=seed)
        text = path.read_text(encoding="utf-8").lstrip("﻿")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()][1:]   # everything after the header is broken
        return path, lines

    def faults(self, carrier: str, body: dict) -> None:
        self.admin.put("/admin/faults", json={"carrier": carrier, **body}).raise_for_status()

    def snap(self, name: str) -> None:
        self.status_at[name] = dict(self.obs.latest() or {})

    # ---- stages
    def baseline(self) -> None:
        print("\n[1] baseline: a normal morning", flush=True)
        for h in (5, 6, 7):
            self.drop(self.stage_export(h))
        self.ev("baseline: ERP exports 05:00, 06:00, 07:00 dropped (ambient mess as the generator injects it)")
        assert self.wait(self.inbox_empty, 180), "baseline ERP files did not drain"
        db_ids = {r["id"] for r in self.q("SELECT id FROM shipment")}
        ships = [s for s in self.world.in_transit(self.clock.now()) if s.id in db_ids][:40]
        pings = [self.sender._ping(s, self.clock.now()) for s in ships]
        code = self.post_gps(pings)
        self.ev(f"baseline: {len(pings)} clean GPS pings posted (HTTP {code})")
        ok = self.wait(lambda: all(r["accepted"] >= 2 for r in self.q("SELECT accepted FROM feed_health WHERE feed LIKE 'carrier:%'"))
                       and len(self.q("SELECT 1 FROM feed_health WHERE feed LIKE 'carrier:%'")) == 3, 40)
        self.ev(f"baseline: every carrier polled ok at least twice ({ok})")
        time.sleep(1.5)
        self.snap("baseline")
        self.R["baseline"] = self.counts_now()
        self.R["baseline"]["fp"] = self.fp()
        self.set_clock(at(8, 15))

    def counts_now(self) -> dict:
        one = lambda sql: self.one(sql)["n"]  # noqa: E731
        return {"shipments": one("SELECT count(*) n FROM shipment"), "lines": one("SELECT count(*) n FROM shipment_line"),
                "gps_pings": one("SELECT count(*) n FROM gps_ping"),
                "dead_letters_waiting": one("SELECT count(*) n FROM dead_letter WHERE status = 'waiting'"),
                "dead_letters_total": one("SELECT count(*) n FROM dead_letter"),
                "bol_applied": one("SELECT count(*) n FROM shipment WHERE bol_no IS NOT NULL")}

    def fp(self) -> dict:
        f = fingerprint(self.conn)
        f.pop("disruption_event", None)
        return f

    def post_gps(self, pings: list[dict], secret: str | None = None, retry: bool = True) -> int:
        body = json.dumps({"pings": pings}).encode()
        ts = int(self.clock.now().timestamp())
        deadline = time.monotonic() + 90
        while True:
            try:
                r = self.gps_client.post("/ingest/gps", content=body, headers={
                    "Content-Type": "application/json", TIMESTAMP_HEADER: str(ts),
                    SIGNATURE_HEADER: sign(secret or self.s.gps_secret, ts, body)})
                return r.status_code
            except httpx.HTTPError:
                self.gps_stats["retries"] += 1
                if not retry or time.monotonic() > deadline:
                    raise
                time.sleep(0.4)

    def chaos(self) -> None:
        print("\n[2] chaos", flush=True)
        a, s = self.args, self.s
        cf = lambda: self.q("SELECT feed, counters, rejected, accepted FROM feed_health")  # noqa: E731
        self.gps_before = {r["feed"]: r["counters"] for r in cf()}.get("gps", {})
        # 2a. C1 hard down
        self.t_c1_on = self.ev("C1: outage on (100% 503)")
        self.faults("C1", {"outage": True})
        self.wait(lambda: self.obs.first("carrier:C1", self.t_c1_on, lambda v: v[1] != "closed") is not None, 40)
        self.t_c1_open = self.obs.first("carrier:C1", self.t_c1_on, lambda v: v[1] != "closed")
        self.ev(f"C1: circuit seen open after {fmt_s(None if self.t_c1_open is None else self.t_c1_open - self.t_c1_on)}")
        self.snap("c1_down")
        # 2b. ERP: ambient 08:00 export + malformed export #1 (+ a duplicate of it)
        self.mal = []
        p1, lines1 = self.malformed(at(8, 10), 40, a.seed + 101)
        self.mal.append((p1.name, lines1))
        dup = self.workdir / "stage" / p1.name.replace(".csv", "_resent.csv")
        shutil.copy(p1, dup)
        self.drop(self.stage_export(8))
        self.drop(p1)
        self.ev(f"ERP: ambient 08:00 export and malformed export #1 ({len(lines1)} broken lines) dropped")
        assert self.wait(self.inbox_empty, 120)
        self.drop(dup)
        self.ev("ERP: byte-identical copy of malformed export #1 dropped (a duplicate file)")
        assert self.wait(self.inbox_empty, 60)
        self.snap("erp_bad_files")
        # 2c. bills of lading, 5 of 20 bad on purpose
        self.bol_manifest = write_bols(s, seed=a.seed, count=20)
        self.ev(f"BOL: {len(self.bol_manifest)} PDFs dropped, 5 bad on purpose (2 unreadable, 2 wrong value, 1 unknown shipment)")
        bol_empty = lambda: not list(s.bol_inbox.glob("*.pdf"))  # noqa: E731
        assert self.wait(bol_empty, 90)
        self.ev("BOL: inbox drained")
        # 2d. keep C1 down until the breaker has backed off to its longest cooldown, then C1 recovers and everything goes 30% flaky
        opens = lambda: self.one("SELECT COALESCE((counters->>'to_open')::int, 0) n FROM feed_health WHERE feed = 'carrier:C1'")["n"]  # noqa: E731
        self.wait(lambda: opens() >= 3, 60)
        self.ev(f"C1: circuit has opened {opens()} times (failed probes double the cooldown 4 -> 8 -> 16 s)")
        self.t_c1_clear = self.ev("C1: outage cleared; every carrier now 30% flaky")
        self.faults("C1", {})
        self.faults("*", {"error_rate": 0.3})
        self.wait(lambda: self.obs.first("carrier:C1", self.t_c1_clear, lambda v: v[1] == "closed") is not None, 60)
        self.t_c1_closed = self.obs.first("carrier:C1", self.t_c1_clear, lambda v: v[1] == "closed")
        self.ev(f"C1: circuit seen closed {fmt_s(None if self.t_c1_closed is None else self.t_c1_closed - self.t_c1_clear)} after the fault cleared")
        self.snap("flaky_window")
        # 2e. C3 hard down, then the tower is killed mid-ingest and C3 heals while it is down
        self.t_c3_on = self.ev("C3: outage on (on top of the 30% flaky)")
        self.faults("C3", {"outage": True})
        self.wait(lambda: self.obs.first("carrier:C3", self.t_c3_on, lambda v: v[1] != "closed") is not None, 40)
        self.t_c3_open = self.obs.first("carrier:C3", self.t_c3_on, lambda v: v[1] != "closed")
        self.ev(f"C3: circuit seen open after {fmt_s(None if self.t_c3_open is None else self.t_c3_open - self.t_c3_on)}")
        self.kill_phase()

    def kill_phase(self) -> None:
        a, s = self.args, self.s
        p2, lines2 = self.malformed(at(9, 30), 30, a.seed + 102)
        self.mal.append((p2.name, lines2))
        files = [self.stage_export(9), p2, self.stage_export(10)]
        db_ids = {r["id"] for r in self.q("SELECT id FROM shipment")}
        ships = [x for x in self.world.in_transit(self.clock.now()) if x.id in db_ids][:40]
        self.burst = build_burst(self.world, ships, self.clock.now(), a.seed)
        n0 = len(list(s.processed.glob("*.csv")))
        for f in files:
            self.drop(f)
        self.ev(f"ERP: 09:00 export, malformed export #2 ({len(lines2)} broken lines), 10:00 export dropped together")
        self.sent = [0]
        self.burst_done = threading.Event()
        threading.Thread(target=self._send_burst, daemon=True).start()
        self.ev(f"GPS: burst of {sum(len(b) for b in self.burst.batches)} pings in {len(self.burst.batches)} batches starts "
                f"(+{self.burst.stats['dup']} duplicates, {self.burst.stats['skewed']} clock-skewed, {self.burst.stats['bad']} bad, "
                f"{self.burst.stats['out_of_order']} out of order)")
        t_wait = time.monotonic()
        while time.monotonic() - t_wait < 40:
            done = len(list(s.processed.glob("*.csv"))) - n0
            left = len(list(s.inbox.glob("*.csv")))
            if (done >= 1 and left >= 1 and self.sent[0] >= 2) or (done >= 1 and left >= 1 and time.monotonic() - t_wait > 6):
                break
            time.sleep(0.03)
        self.R["kill"] = {"erp_done": done, "erp_left": left, "gps_batches_sent": self.sent[0]}
        self.t_kill = self.ev(f"TOWER KILLED (hard): {left} ERP files still in the inbox, {done} done, "
                              f"{self.sent[0]} of {len(self.burst.batches)} GPS batches sent")
        self.kill_tree(self.procs["tower"])
        assert self.wait(lambda: not self._in_use(TOWER_PORT), 10), "the tower is still listening after the kill"
        time.sleep(2.0)
        self.faults("C3", {})
        self.ev("C3: outage cleared while the tower is down")
        time.sleep(2.0)
        t_cmd = time.monotonic()
        self.start("tower", "tower", f"{self.tower_url}/health")
        self.tower_start_s = time.monotonic() - t_cmd
        self.t_restart = self.ev("TOWER RESTARTED and answering /health")
        assert self.wait(self.inbox_empty, 180), "ERP inbox did not drain after the restart"
        self.ev("ERP: inbox drained after the restart")
        self.burst_done.wait(120)
        self.ev(f"GPS: burst finished (retried requests: {self.gps_stats['retries']})")
        time.sleep(1.0)
        self.snap("after_restart")
        # let the pollers settle with only the 30% flakiness left, then lift it
        time.sleep(12)
        self.t_all_clear = self.ev("all carrier faults cleared")
        self.admin.delete("/admin/faults").raise_for_status()
        self.wait(lambda: all((self.obs.latest() or {}).get(f"carrier:{c}", ("", "x"))[1] == "closed" for c in CARRIERS), 25)
        time.sleep(6)
        self.snap("faults_cleared")

    def _send_burst(self) -> None:
        for i, batch in enumerate(self.burst.batches):
            if i in (1, 3, 5, 7, 9):   # a sender with the wrong secret
                code = self.post_gps(batch[:1], secret="not-the-secret")
                self.gps_stats["forged"] += 1
                self.gps_stats["forged_rejected"] += int(code == 401)
            code = self.post_gps(batch)
            self.gps_stats[f"http_{code}"] += 1
            self.sent[0] = i + 1
            time.sleep(0.6)
        self.burst_done.set()

    # ---- measurement
    def measure(self) -> None:
        print("\n[3] measure", flush=True)
        R = self.R
        dl = self.q("SELECT id, feed, status, reason_code, payload, suggested_patch IS NOT NULL AS fixable FROM dead_letter")
        R["dl_snapshot"] = [{"id": r["id"], "feed": r["feed"], "reason": r["reason_code"], "fixable": r["fixable"],
                             "file": r["payload"].get("_file")} for r in dl]
        feeds = {r["feed"]: r for r in self.q("SELECT * FROM feed_health")}
        R["feeds"] = {k: {"received": v["received"], "accepted": v["accepted"], "rejected": v["rejected"],
                          "circuit": v["circuit_state"], "counters": v["counters"]} for k, v in feeds.items()}
        R["after_chaos"] = self.counts_now()
        R["fp_chaos"] = self.fp()

        # ERP ambient mess: injected by the generator vs dead-lettered, per kind (by source file)
        ambient = {f"erp_export_20261113_{h:02d}00.csv" for h in (5, 6, 7, 8, 9, 10)}
        inj = self.erp.injected
        by_reason = Counter(r["reason_code"] for r in dl if r["feed"] == "erp" and r["payload"].get("_file") in ambient)
        kinds = [("blank promised date", "blank_promised", "missing_promised_at"), ("unknown city code", "unknown_city", "unknown_zone"),
                 ("unreadable amount", "bad_amount", "bad_amount"), ("conflicting duplicate row", "conflict_dup", "duplicate_conflict"),
                 ("malformed line (cut, junk, quote)", "malformed_lines", "malformed_row")]
        R["erp_ambient"] = [{"kind": k, "injected": inj.get(i, 0), "dead_lettered": by_reason.get(c, 0)} for k, i, c in kinds]
        R["erp_absorbed"] = {"exact_dup_rows": inj.get("exact_dup", 0), "padded_rows": inj.get("padded_rows", 0)}

        # outage files: every broken line, matched by text, to a dead letter
        raw = Counter(r["payload"].get("raw_line", "").strip() for r in dl if r["feed"] == "erp" and r["payload"].get("_file") not in ambient)
        files = []
        for name, lines in self.mal:
            distinct = set(lines)
            files.append({"file": name, "lines": len(lines), "distinct": len(distinct), "dead_lettered": sum(1 for ln in distinct if raw[ln]),
                          "missing": sum(1 for ln in distinct if not raw[ln])})
        R["erp_outage_files"] = files
        audited = self.one("SELECT COALESCE(sum((detail->>'dead_letter')::int),0) d, COALESCE(sum((detail->>'dead_letter_repeat')::int),0) r "
                           "FROM audit_log WHERE action = 'erp.file_processed'")
        injected_bad = sum(x["injected"] for x in R["erp_ambient"]) + sum(f["lines"] for f in files)
        R["erp_total"] = {"injected_bad_rows": injected_bad, "dead_letter_rows": int(audited["d"]), "repeats_collapsed": int(audited["r"]),
                          "lost": injected_bad - int(audited["d"]) - int(audited["r"])}
        # every exported shipment is applied or sits in a dead letter
        in_db = {r["id"] for r in self.q("SELECT id FROM shipment")}
        in_dl: set[str] = set()
        for r in dl:
            if r["feed"] == "erp":
                text = " ".join(str(v) for k, v in r["payload"].items() if not k.startswith("_") or k == "_x")
                in_dl.update(m.upper() for m in re.findall(r"SH-\d+", text, re.I))
        exported = set(self.erp.state)
        R["erp_ships"] = {"exported": len(exported), "applied": len(exported & in_db), "only_in_dead_letters": len((exported - in_db) & in_dl),
                          "lost": len(exported - in_db - in_dl), "in_db_not_exported": len(in_db - exported)}

        # idempotency
        R["idem"] = {
            "shipment_rows": self.one("SELECT count(*) n FROM shipment")["n"],
            "distinct_shipment_ids_upper": self.one("SELECT count(DISTINCT upper(id)) n FROM shipment")["n"],
            "order_no_repeated": self.one("SELECT count(*) n FROM (SELECT order_no FROM shipment GROUP BY 1 HAVING count(*) > 1) x")["n"],
            "line_rows": self.one("SELECT count(*) n FROM shipment_line")["n"],
            "line_distinct": self.one("SELECT count(*) n FROM (SELECT DISTINCT shipment_id, sku FROM shipment_line) x")["n"],
            "gps_rows": self.one("SELECT count(*) n FROM gps_ping")["n"],
            "gps_distinct_ping_id": self.one("SELECT count(DISTINCT ping_id) n FROM gps_ping")["n"],
            "gps_same_ship_same_time": self.one("SELECT count(*) n FROM (SELECT 1 FROM gps_ping GROUP BY shipment_id, event_ts HAVING count(*) > 1) x")["n"],
            "position_regressions": self.one("SELECT count(*) n FROM shipment s WHERE last_position_at IS NOT NULL AND last_position_at <> "
                                             "(SELECT max(event_ts) FROM gps_ping p WHERE p.shipment_id = s.id)")["n"],
        }
        # GPS burst: injected vs outcome
        g = feeds["gps"]["counters"]
        b = self.gps_before
        d = lambda k: g.get(k, 0) - b.get(k, 0)  # noqa: E731
        stored = self.one("SELECT count(*) n FROM gps_ping WHERE ping_id = ANY(%s)", (sorted(self.burst.valid_ids),))["n"]
        gdl = Counter(r["reason_code"] for r in dl if r["feed"] == "gps")
        R["gps"] = {"valid_unique": len(self.burst.valid_ids), "stored": stored, "dup_injected": self.burst.stats["dup"],
                    "dup_suppressed": d("duplicates"), "ooo_injected": self.burst.stats["out_of_order"], "ooo_stored": d("reordered"),
                    "skew_injected": self.burst.stats["skewed"], "skew_dead_lettered": gdl.get("clock_skew_future", 0),
                    "bad_injected": self.burst.stats["bad"], "bad_dead_lettered": gdl.get("bad_ping", 0),
                    "forged_sent": self.gps_stats["forged"], "forged_rejected": d("bad_signature"), "retries": self.gps_stats["retries"]}
        # BOL
        bdl = Counter(r["reason_code"] for r in dl if r["feed"] == "bol")
        modes = Counter(m["mode"] for m in self.bol_manifest)
        R["bol"] = {"files": len(self.bol_manifest), "applied": R["after_chaos"]["bol_applied"],
                    "unreadable": (modes["garbled"] + modes["blank"], bdl.get("unreadable_pdf", 0)),
                    "wrong_value": (modes["mismatch_high"] + modes["mismatch_low"], bdl.get("value_mismatch", 0)),
                    "unknown": (modes["wrong_id"], bdl.get("unknown_shipment", 0))}
        # circuits
        lc = {c: self.obs.circuit_log(f"carrier:{c}") for c in CARRIERS}
        R["circuits"] = {c: [(round(t, 1), st) for t, st in v] for c, v in lc.items()}
        R["timings"] = {
            "c1_open_s": None if self.t_c1_open is None else round(self.t_c1_open - self.t_c1_on, 1),
            "c1_closed_s": None if self.t_c1_closed is None else round(self.t_c1_closed - self.t_c1_clear, 1),
            "c3_open_s": None if self.t_c3_open is None else round(self.t_c3_open - self.t_c3_on, 1),
            "downtime_s": round(self.t_restart - self.t_kill, 1), "tower_start_s": round(self.tower_start_s, 1),
        }
        R["observer"] = {"samples": len(self.obs.samples), "errors": dict(self.obs.errors), "live_compared": self.obs.compared,
                         "live_mismatched": self.obs.mismatched}
        R["final_status"] = {f: list(v) for f, v in (self.obs.latest() or {}).items()}

    # ---- recovery
    def recover(self) -> None:
        print("\n[4] recover: replay the fixable dead letters with their suggested fixes", flush=True)
        snap = lambda: {r["id"]: r for r in self.q("SELECT id, version, promised_at FROM shipment")}  # noqa: E731
        before = snap()
        rows = self.q("SELECT id, feed, reason_code FROM dead_letter WHERE status = 'waiting' AND suggested_patch IS NOT NULL ORDER BY id")
        res: Counter = Counter()
        results: Counter = Counter()       # (feed, reason, result): applied, unchanged, stale or failed
        for r in rows:
            resp = self.api.post(f"/dead-letters/{r['id']}/replay", headers={"X-Admin-Key": self.s.admin_key}, json={})
            key = (r["feed"], r["reason_code"])
            res[key] += 1
            results[(*key, resp.json().get("result", "failed") if resp.status_code in (200, 422) else "failed")] += 1
        left = self.q("SELECT feed, reason_code, count(*) n FROM dead_letter WHERE status = 'waiting' GROUP BY 1, 2 ORDER BY 1, 2")
        total_dl = self.one("SELECT count(*) n FROM dead_letter")["n"]
        after = snap()
        moved = [k for k in before if after[k]["promised_at"] != before[k]["promised_at"]]
        self.R["recovery"] = {
            "by_kind": [{"feed": f, "reason": c, "replayed": n, **{k: results[(f, c, k)] for k in RESULTS}} for (f, c), n in sorted(res.items())],
            "left": [{"feed": r["feed"], "reason": r["reason_code"], "n": r["n"]} for r in left],
            "outcomes": dict(Counter(r["o"] for r in self.q("SELECT detail->'outcome' #>> '{}' AS o FROM audit_log WHERE action = 'dead_letter.replay'"))),
            "existing_updated": len(moved), "promised_earlier": sum(1 for k in moved if after[k]["promised_at"] < before[k]["promised_at"]),
            "total_dead_letters": total_dl, "attempted": sum(res.values()),
            "results": {k: sum(n for (_, _, x), n in results.items() if x == k) for k in RESULTS}}
        self.R["after_recovery"] = self.counts_now()
        self.R["fp_recovered"] = self.fp()
        time.sleep(1.0)
        self.snap("recovered")

    def reprocess(self) -> None:
        print("\n[5] reprocess: the same files and pings again", flush=True)
        s, R = self.s, self.R
        steps = []

        def rows() -> dict:
            return {r["id"]: r for r in self.q("SELECT id, version, source, content_hash, source_exported_at, eta, promised_at, value_inr, cases, "
                                                "status FROM shipment ORDER BY id")}

        def redrop(label: str, wipe: bool) -> None:
            if wipe:
                self.conn.execute("DELETE FROM ingest_log WHERE feed = 'erp'")
            before, before_rows = self.fp(), rows()
            for f in files:
                shutil.copy(f, s.inbox / f"{f.stem}.again{f.suffix}")
            assert self.wait(self.inbox_empty, 240)
            time.sleep(1.5)
            after, after_rows = self.fp(), rows()
            changed = [k for k in after_rows if k in before_rows and after_rows[k]["content_hash"] != before_rows[k]["content_hash"]]
            diff = []
            for k in changed[:5]:
                b, a_ = before_rows[k], after_rows[k]
                diff.append({"id": k, "fields": {f: [str(b[f]), str(a_[f])] for f in b if f != "content_hash" and b[f] != a_[f]}})
            steps.append({"step": label, "before": before, "after": after, "unchanged": before == after, "changed": len(changed), "diff": diff})
            self.ev(f"reprocess: {label}: state unchanged = {before == after} ({len(changed)} shipments with other content)")

        files = sorted(s.processed.glob("erp_export_*.csv"))
        redrop("ERP files again (file hash seen)", False)
        redrop("ERP files again, file memory wiped (row level)", True)
        before = self.fp()
        pings = [p for batch in self.burst.batches for p in batch]
        for i in range(0, len(pings), BATCH):
            self.post_gps(pings[i:i + BATCH])
        after = self.fp()
        steps.append({"step": "whole GPS burst again", "before": before, "after": after, "unchanged": before == after})
        self.ev(f"reprocess: whole GPS burst again: state unchanged = {before == after}")
        before = self.fp()
        for d in (s.bol_processed, s.bol_rejected):
            for f in d.glob("*.pdf"):
                shutil.copy(f, s.bol_inbox / f.name)
        n = len(list(s.bol_inbox.glob("*.pdf")))
        self.wait(lambda: not list(s.bol_inbox.glob("*.pdf")), 90)
        time.sleep(1)
        after = self.fp()
        steps.append({"step": f"{n} BOL PDFs again", "before": before, "after": after, "unchanged": before == after})
        self.ev(f"reprocess: {n} BOL PDFs again: state unchanged = {before == after}")
        R["reprocess"] = steps

    # ---- report
    def report(self) -> str:
        R, out = self.R, []
        w = out.append
        w("## Timeline (wall seconds from the start of the run)\n")
        w(table(["t", "clock", "event"], [[f"{t:.1f}", c, e] for t, c, e in self.events]))
        b, a, ac, ar = R["baseline"], R["after_chaos"], R["after_chaos"], R["after_recovery"]
        w("\n## Baseline and after the chaos\n")
        w(table(["", "baseline", "after chaos", "after recovery"], [
            ["shipments", b["shipments"], a["shipments"], ar["shipments"]],
            ["shipment lines", b["lines"], a["lines"], ar["lines"]],
            ["GPS rows", b["gps_pings"], a["gps_pings"], ar["gps_pings"]],
            ["dead letters, waiting", b["dead_letters_waiting"], a["dead_letters_waiting"], ar["dead_letters_waiting"]],
            ["dead letters, all", b["dead_letters_total"], ac["dead_letters_total"], ar["dead_letters_total"]],
            ["shipments with a bill of lading", b["bol_applied"], a["bol_applied"], ar["bol_applied"]],
            ["state md5 (shipments)", R["baseline"]["fp"]["state_md5"], R["fp_chaos"]["state_md5"], R["fp_recovered"]["state_md5"]]]))
        w("\n## Injected against outcome\n")
        w("ERP rows with ambient mess (the generator's fixed rates, six exports), and the two broken exports:\n")
        w(table(["kind", "injected", "dead-lettered", "difference"],
                [[x["kind"], x["injected"], x["dead_lettered"], x["injected"] - x["dead_lettered"]] for x in R["erp_ambient"]]
                + [[f"broken export {f['file']}: lines (distinct)", f"{f['lines']} ({f['distinct']})", f["dead_lettered"], f["missing"]] for f in R["erp_outage_files"]]))
        e = R["erp_total"]
        w(f"\nAbsorbed by design, not dead-lettered: {R['erp_absorbed']['exact_dup_rows']} exact duplicate rows, {R['erp_absorbed']['padded_rows']} padded or mixed-format rows.\n")
        w(table(["ERP accounting", "count"], [
            ["bad rows injected (ambient kinds above + broken-export lines)", e["injected_bad_rows"]],
            ["dead letters written (audit log)", e["dead_letter_rows"]],
            ["repeats of an already filed bad payload (audit log)", e["repeats_collapsed"]],
            ["lost (injected - dead letters - repeats)", e["lost"]],
            ["shipments exported at least once", R["erp_ships"]["exported"]],
            ["... applied", R["erp_ships"]["applied"]],
            ["... not applied, with a dead letter naming them", R["erp_ships"]["only_in_dead_letters"]],
            ["... lost (not applied, no dead letter)", R["erp_ships"]["lost"]]]))
        g = R["gps"]
        w("\n")
        w(table(["GPS burst", "injected", "outcome"], [
            ["valid unique pings", g["valid_unique"], f"{g['stored']} stored"],
            ["duplicates", g["dup_injected"], f"{g['dup_suppressed']} suppressed (includes {g['dup_suppressed'] - g['dup_injected']} from retried requests)"],
            ["out of order (older ping after a newer one)", g["ooo_injected"], f"{g['ooo_stored']} stored without moving the position back"],
            ["clock-skewed (20-45 min ahead)", g["skew_injected"], f"{g['skew_dead_lettered']} dead letters"],
            ["failing validation (lat 999)", g["bad_injected"], f"{g['bad_dead_lettered']} dead letters"],
            ["forged-signature requests", g["forged_sent"], f"{g['forged_rejected']} rejected (401)"]]))
        bl = R["bol"]
        w("\n")
        w(table(["Bills of lading", "injected", "outcome"], [
            [f"{bl['files']} PDFs, good ones", bl["files"] - 5, f"{bl['applied']} applied to a shipment"],
            ["unreadable (garbled, blank)", bl["unreadable"][0], f"{bl['unreadable'][1]} dead letters"],
            ["declared value 6-9% off", bl["wrong_value"][0], f"{bl['wrong_value'][1]} dead letters"],
            ["unknown shipment", bl["unknown"][0], f"{bl['unknown'][1]} dead letters"]]))
        i = R["idem"]
        w("\n## Idempotency\n")
        w(table(["check", "result"], [
            ["shipment rows / distinct ids (case-folded)", f"{i['shipment_rows']} / {i['distinct_shipment_ids_upper']}"],
            ["order numbers on more than one shipment", i["order_no_repeated"]],
            ["shipment lines / distinct (shipment, sku)", f"{i['line_rows']} / {i['line_distinct']}"],
            ["GPS rows / distinct ping ids", f"{i['gps_rows']} / {i['gps_distinct_ping_id']}"],
            ["shipments with two pings at the same instant", i["gps_same_ship_same_time"]],
            ["shipments whose position is older than their newest ping", i["position_regressions"]]]))
        w("\n## Circuit breakers\n")
        t = R["timings"]
        w(table(["measure", "value"], [
            ["C1 outage on -> circuit open", fmt_s(t["c1_open_s"])],
            ["C1 fault cleared -> circuit closed", fmt_s(t["c1_closed_s"])],
            ["C3 outage on -> circuit open", fmt_s(t["c3_open_s"])],
            ["tower down (kill -> answering /health)", fmt_s(t["downtime_s"])],
            ["... of which the process took to start and answer", fmt_s(t["tower_start_s"])]]))
        w("\n")
        w(table(["carrier", "polls", "failed polls", "opened", "half-open", "closed", "state at the end"], [
            [c, R["feeds"][f"carrier:{c}"]["received"], R["feeds"][f"carrier:{c}"]["counters"].get("failed_polls", 0),
             R["feeds"][f"carrier:{c}"]["counters"].get("to_open", 0), R["feeds"][f"carrier:{c}"]["counters"].get("to_half_open", 0),
             R["feeds"][f"carrier:{c}"]["counters"].get("to_closed", 0), R["feeds"][f"carrier:{c}"]["circuit"]] for c in CARRIERS]))
        w("\nCircuit state changes seen by the 0.25 s observer (seconds from the start of the run):\n")
        w(table(["carrier", "changes"], [[c, ", ".join(f"{st} @ {t}" for t, st in R["circuits"][c])] for c in CARRIERS]))
        w("\n## /health/feeds as the UI sees it\n")
        feeds = ["erp", "gps", "bol", "alerts", *[f"carrier:{c}" for c in CARRIERS]]
        phases = [("baseline", "baseline"), ("c1_down", "C1 down"), ("erp_bad_files", "bad ERP files"), ("flaky_window", "30% flaky"),
                  ("after_restart", "after restart"), ("faults_cleared", "faults cleared"), ("recovered", "after replay")]
        cell = lambda ph, f: "-" if f not in self.status_at.get(ph, {}) else f"{self.status_at[ph][f][0]}/{self.status_at[ph][f][1][0]}"  # noqa: E731
        w(table(["feed", *[p[1] for p in phases]], [[f, *[cell(p[0], f) for p in phases]] for f in feeds]))
        o = R["observer"]
        w(f"\nstatus/circuit; c = closed, o = open, h = half open. Observer: {o['samples']} samples, errors {o['errors'] or 'none'}; "
          f"/live circuits compared with /health/feeds {o['live_compared']} times, {o['live_mismatched']} mismatches.\n")
        w("\n## Recovery\n")
        rc = R["recovery"]
        w(table(["feed", "reason", "replayed with suggested fix", "applied", "unchanged", "stale", "failed again"],
                [[x["feed"], x["reason"], x["replayed"], *[x[k] for k in RESULTS]] for x in rc["by_kind"]]))
        rs = rc["results"]
        w(f"\nReplayed {rc['attempted']} of {rc['total_dead_letters']} dead letters: {rs['applied']} applied (changed the data), "
          f"{rs['unchanged']} unchanged (content already held), {rs['stale']} stale (older than the data held, not applied), "
          f"{rs['failed']} failed again. Handler outcomes: {rc['outcomes']}. "
          f"Replays that moved the promised date of a shipment already in the database: {rc['existing_updated']} "
          f"({rc['promised_earlier']} earlier than before). Left waiting, with no automatic fix:\n")
        w(table(["feed", "reason", "waiting"], [[x["feed"], x["reason"], x["n"]] for x in rc["left"]]))
        w("\n## Reprocessing the same input\n")
        w(table(["step", "state md5 before", "after", "unchanged"],
                [[x["step"], x["before"]["state_md5"], x["after"]["state_md5"], x["unchanged"]] for x in R["reprocess"]]))
        for x in R["reprocess"]:
            if x.get("diff"):
                w(f"\n{x['step']}: {x['changed']} shipments ended with other content. First ones (field: before -> after):")
                for d in x["diff"]:
                    w(f"- {d['id']}: " + "; ".join(f"{f}: {v[0]} -> {v[1]}" for f, v in d["fields"].items()))
        w(f"\nFull counts before the last step: {json.dumps(R['reprocess'][-1]['before'])}")
        return "\n".join(out)

    # ---- run
    def run(self) -> None:
        print(f"Chaos drill, seed {self.args.seed}. Database {DB_NAME}, tower :{TOWER_PORT}, carrier mock :{CARRIER_PORT}, "
              f"work dir {self.workdir}", flush=True)
        try:
            self.setup()
            self.start("carrier_api", "sim.carrier_api", f"{self.carrier_url}/health")
            self.start("tower", "tower", f"{self.tower_url}/health")
            self.ev("stack up (isolated)")
            self.obs = Observer(self.tower_url, self.s.token_asha, self.t0)
            self.obs.start()
            self.baseline()
            self.chaos()
            self.measure()
            self.recover()
            self.reprocess()
            self.obs.stop_event.set()
            text = self.report()
            print("\n" + "=" * 100 + "\n# Chaos drill results\n\n" + text, flush=True)
            out = self.workdir.parent / "ct_drill_results.json"
            out.write_text(json.dumps(self.R, indent=2, default=str), encoding="utf-8")
            if self.args.report:
                Path(self.args.report).write_text(text + "\n", encoding="utf-8")
        finally:
            if hasattr(self, "obs"):
                self.obs.stop_event.set()
            self.stop_all()
            if hasattr(self, "conn"):
                self.conn.close()
            if not self.args.keep:
                try:
                    drop_database(self.db_url, DB_NAME)
                except Exception as e:   # noqa: BLE001
                    print(f"could not drop {DB_NAME}: {e}")
                shutil.rmtree(self.workdir, ignore_errors=True)
                print(f"\nstopped the tower and the mock, dropped {DB_NAME}, removed {self.workdir}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m sim.drill", description=__doc__.split("\n\n")[0])
    ap.add_argument("--seed", type=int, default=20261113)
    ap.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    ap.add_argument("--report", help="also write the result tables (markdown) to this file")
    ap.add_argument("--keep", action="store_true", help="keep ct_drill and the work dir afterwards")
    Drill(ap.parse_args()).run()


if __name__ == "__main__":
    main()
