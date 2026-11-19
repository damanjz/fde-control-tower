"""`python -m sim.demo`: reset, run the 05:00 to 10:40 IST scenario compressed, print a live table, then a summary.

Starts the carrier API and the tower as subprocesses (and stops them at the end) unless they already answer
on their ports, in which case it reuses them and leaves them running.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from sim.scenario import EXPORT_TIMES, FAULT_SCHEDULE, Scenario, make_clock
from sim.world import SCENARIO_END, SCENARIO_START, World
from tower.clock import IST, ScenarioClock, clear_clock, write_clock
from tower.config import ROOT, Settings, load_settings
from tower.db import apply_schema, reset_operational
from tower.seed import seed

# the Docker CLI; set CT_DOCKER when it is not on PATH
DOCKER = os.environ.get("CT_DOCKER", "docker")


def up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_db(settings: Settings) -> None:
    try:
        psycopg.connect(settings.db_url, connect_timeout=3).close()
    except psycopg.OperationalError:
        print("database not reachable: starting it with docker compose")
        subprocess.run([DOCKER, "compose", "up", "-d", "db", "--wait"], cwd=ROOT, check=True)


def clear_dirs(settings: Settings) -> None:
    for d in (settings.inbox, settings.processed, settings.rejected):
        d.mkdir(parents=True, exist_ok=True)
        for f in d.iterdir():
            if f.is_dir():   # data/processed/bol and data/rejected/bol (milestone 2)
                shutil.rmtree(f)
            elif f.name != ".gitkeep":
                f.unlink()


def start(name: str, module: str, health_url: str, settings: Settings) -> subprocess.Popen | None:
    if up(health_url):
        print(f"  {name}: already running, reusing it")
        return None
    log = open(settings.data_dir / "logs" / f"{name}.log", "wb")  # noqa: SIM115 - closed with the process
    proc = subprocess.Popen([sys.executable, "-m", module], cwd=ROOT, stdout=log, stderr=log)
    for _ in range(60):
        if up(health_url):
            print(f"  {name}: started (pid {proc.pid})")
            return proc
        time.sleep(0.5)
    proc.kill()
    raise SystemExit(f"{name} did not come up; see data/logs/{name}.log")


def snapshot(conn, now: datetime) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()["n"]  # noqa: E731
    feeds = {r["feed"]: r for r in conn.execute("SELECT * FROM feed_health").fetchall()}
    g = feeds.get("gps", {"counters": {}, "accepted": 0, "rejected": 0})
    gc = g["counters"]
    e = feeds.get("erp", {"counters": {}})
    return {
        "scen": now.astimezone(IST).strftime("%H:%M"),
        "ships": one("SELECT count(*) n FROM shipment"),
        "upd": one("SELECT count(*) n FROM shipment WHERE version > 1"),
        "dups": e["counters"].get("duplicates", 0),
        "dlq": one("SELECT count(*) n FROM dead_letter WHERE status = 'waiting'"),
        "gps_ok": g["accepted"] - gc.get("duplicates", 0),
        "gps_reord": gc.get("reordered", 0),
        "gps_dup": gc.get("duplicates", 0),
        "gps_rej": gc.get("bad_signature", 0) + gc.get("dead_lettered", 0),
        "circ": " ".join(f"{c}:{feeds[f'carrier:{c}']['circuit_state'][0].upper()}" if f"carrier:{c}" in feeds else f"{c}:-"
                         for c in ("C1", "C2", "C3")),
        "disr": one("SELECT count(*) n FROM disruption_event"),
    }


HEAD = f"{'wall':>5} {'scen':>5} | {'ships':>5} {'upd':>4} {'dupSup':>6} {'DLQ':>4} | {'gpsOK':>5} {'reord':>5} {'dupl':>4} {'rej':>4} | {'circuits C/O/H':<20} | {'disr':>4}"


def row(wall: float, s: dict) -> str:
    return (f"{wall:4.0f}s {s['scen']:>5} | {s['ships']:5d} {s['upd']:4d} {s['dups']:6d} {s['dlq']:4d} | "
            f"{s['gps_ok']:5d} {s['gps_reord']:5d} {s['gps_dup']:4d} {s['gps_rej']:4d} | {s['circ']:<20} | {s['disr']:4d}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the compressed Tarangini scenario end to end")
    ap.add_argument("--wall", type=float, default=75.0, help="wall seconds for 05:00 to 10:40 IST (default 75)")
    ap.add_argument("--every", type=float, default=4.0, help="seconds between table rows")
    args = ap.parse_args()
    settings = load_settings()

    print("Tarangini control tower: milestone 1 demo")
    ensure_db(settings)
    apply_schema(settings.db_url)
    seed(settings)
    reset_operational(settings.db_url)
    clear_dirs(settings)
    clear_clock(settings.clock_file)
    print("reset: operational tables truncated, network and contracts kept, inbox cleared")
    # the audit log is append-only: mark where this run of the morning starts, so the UI can show this morning only
    with psycopg.connect(settings.db_url, autocommit=True) as c:
        c.execute("INSERT INTO audit_log (at, actor, action, target, detail) VALUES (%s, 'system', 'scenario.start', 'scenario', %s)",
                  (SCENARIO_START, Jsonb({"seed": settings.scenario_seed})))

    # park the clock at the scenario start until everything is up, so the mock feeds do not run on real time
    write_clock(settings.clock_file, ScenarioClock(SCENARIO_START, 1.0, wall_start=time.time() + 3600))
    procs = [p for p in (
        start("carrier_api", "sim.carrier_api", f"http://127.0.0.1:{settings.carrier_api_port}/health", settings),
        start("tower", "tower", f"http://{settings.host}:{settings.port}/health", settings),
    ) if p]
    world = World(settings.network_dir, settings.scenario_seed)
    clock = make_clock(args.wall)
    scenario = Scenario(settings, world, clock)
    conn = psycopg.connect(settings.db_url, row_factory=dict_row, autocommit=True)
    audit_start = conn.execute("SELECT COALESCE(max(id), 0) AS n FROM audit_log").fetchone()["n"]
    t0 = time.time() + 2.0
    print(f"scenario: {len(world.shipments)} shipments in the universe, {len(EXPORT_TIMES)} ERP exports, "
          f"{len(FAULT_SCHEDULE)} fault events, {clock.factor:.0f}x time\n")
    print(HEAD)
    print("-" * len(HEAD))
    last = [0.0]

    def on_tick() -> None:
        if time.time() - last[0] >= args.every and time.time() >= t0:
            last[0] = time.time()
            print(row(time.time() - t0, snapshot(conn, min(clock.now(), SCENARIO_END))), flush=True)

    scenario.on_tick = on_tick
    try:
        scenario.run()
        # stop the clock at 10:40: the drain and the summary must not stamp anything after the end of the morning
        write_clock(settings.clock_file, ScenarioClock(SCENARIO_END, 0.0))
        time.sleep(4)   # let the pollers and the ERP worker drain
        print(row(time.time() - t0, snapshot(conn, min(clock.now(), SCENARIO_END))), flush=True)
        summary(conn, settings, scenario, world, audit_start)
    finally:
        conn.close()
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
        clear_clock(settings.clock_file)
        if procs:
            print("\nstopped the subprocesses it started")


def counts(conn) -> dict:
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]  # noqa: S608
            for t in ("shipment", "shipment_line", "dead_letter", "gps_ping", "disruption_event")}


def summary(conn, settings: Settings, sc: Scenario, world: World, audit_start: int) -> None:
    print("\n=== Final summary (scenario time 10:40 IST) ===")
    erp = conn.execute("SELECT * FROM feed_health WHERE feed = 'erp'").fetchone()
    ec = erp["counters"]
    print(f"\nERP: {ec.get('files', 0)} files, {erp['received']} rows received, {erp['accepted']} accepted, {erp['rejected']} to dead letters")
    print(f"  new {ec.get('new', 0)}, updated (version+1) {ec.get('updated', 0)}, duplicates suppressed {ec.get('duplicates', 0)}, stale ignored {ec.get('stale', 0)}")
    tot = conn.execute("SELECT count(*) n, COALESCE(sum(value_inr),0) v, count(*) FILTER (WHERE version > 1) u, max(version) mv FROM shipment").fetchone()
    print(f"  shipments in DB: {tot['n']} of {len(world.shipments)}; value Rs {float(tot['v']) / 1e7:.2f} Cr; "
          f"{tot['u']} at version 2 or more (max {tot['mv']})")
    bydc = conn.execute("SELECT dc_id, count(*) n FROM shipment GROUP BY 1 ORDER BY 1").fetchall()
    print("  by DC: " + ", ".join(f"{r['dc_id']} {r['n']}" for r in bydc))

    inj = sc.erp.injected
    caught = Counter({r["reason_code"]: r["n"] for r in conn.execute(
        "SELECT reason_code, count(*) n FROM dead_letter WHERE feed = 'erp' GROUP BY 1").fetchall()})
    print("\nDirty ERP data: injected vs dead-lettered (repeats of one bad row in later exports collapse into one entry)")
    print(f"  {'mess':<22}{'injected':>9}{'dead-lettered':>15}")
    for mess, code in (("blank promised date", "missing_promised_at"), ("unknown city code", "unknown_zone"),
                       ("unreadable amount", "bad_amount"), ("conflicting duplicate", "duplicate_conflict"),
                       ("malformed line", "malformed_row")):
        key = {"missing_promised_at": "blank_promised", "unknown_zone": "unknown_city", "bad_amount": "bad_amount",
               "duplicate_conflict": "conflict_dup", "malformed_row": "malformed_lines"}[code]
        print(f"  {mess:<22}{inj.get(key, 0):>9}{caught.get(code, 0):>15}")
    print(f"  exact duplicate rows: {inj.get('exact_dup', 0)} injected, absorbed as 'duplicate' (no new row); "
          f"padded/BOM/mixed-format rows: {inj.get('padded_rows', 0)} padded, all parsed")

    gps = conn.execute("SELECT * FROM feed_health WHERE feed = 'gps'").fetchone()
    gc = gps["counters"]
    st = sc.gps.stats
    print(f"\nGPS: sim sent {st['sent']} pings ({st['duplicates']} duplicates, {st['delayed']} held back, {st['skewed']} clock-skewed) "
          f"plus {st['forged']} forged-signature requests")
    print(f"  tower: {gps['accepted']} accepted ({gc.get('reordered', 0)} out of order, stored without moving the position back), "
          f"{gc.get('duplicates', 0)} duplicates suppressed, {gc.get('dead_lettered', 0)} to dead letters (skew), "
          f"{gc.get('bad_signature', 0)} rejected for bad signature ({st['forged_rejected']} of {st['forged']} forged caught)")
    print(f"  ping rows stored: {conn.execute('SELECT count(*) n FROM gps_ping').fetchone()['n']}")

    print("\nCarrier circuit breakers:")
    for r in conn.execute("SELECT * FROM feed_health WHERE feed LIKE 'carrier:%' ORDER BY feed").fetchall():
        c = r["counters"]
        print(f"  {r['feed']}: now {r['circuit_state']}, {r['accepted']} polls ok, {r['rejected']} failed, "
              f"opened {c.get('to_open', 0)}x, closed again {c.get('to_closed', 0)}x")
    trans = conn.execute("SELECT at, target, detail FROM audit_log WHERE action = 'carrier.circuit' AND id > %s ORDER BY id", (audit_start,)).fetchall()
    for t in trans:
        print(f"    {t['at'].astimezone(IST):%H:%M} IST {t['target']}: {t['detail']['from']} -> {t['detail']['to']}")
    held = conn.execute("SELECT count(*) n FROM shipment WHERE carrier_status = 'held'").fetchone()["n"]
    print(f"  shipments the carrier API reports as held (strike): {held}")

    print("\nDisruptions received:")
    for r in conn.execute("SELECT started_at, kind, severity, title FROM disruption_event ORDER BY started_at").fetchall():
        print(f"  {r['started_at'].astimezone(IST):%H:%M} {r['kind']:<9} {r['severity']:<8} {r['title']}")

    print("\nDead letters waiting, by feed and reason:")
    for r in conn.execute("SELECT feed, reason_code, count(*) n FROM dead_letter WHERE status = 'waiting' GROUP BY 1, 2 ORDER BY 3 DESC").fetchall():
        print(f"  {r['feed']:<7}{r['reason_code']:<22}{r['n']:>4}")

    # replay one fixable dead letter through the API with the suggested fix
    dl = conn.execute("SELECT id, reason_code, suggested_fix FROM dead_letter WHERE status = 'waiting' "
                      "AND suggested_patch IS NOT NULL AND feed = 'erp' ORDER BY id LIMIT 1").fetchone()
    if dl:
        r = httpx.post(f"http://{settings.host}:{settings.port}/dead-letters/{dl['id']}/replay",
                       headers={"X-Admin-Key": settings.admin_key}, json={})
        print(f"\nReplay of dead letter {dl['id']} ({dl['reason_code']}) with its suggested fix [{dl['suggested_fix']}]: "
              f"HTTP {r.status_code} {json.dumps(r.json())}")
    audited = conn.execute("SELECT count(*) n FROM audit_log WHERE id > %s", (audit_start,)).fetchone()["n"]
    print(f"audit_log: {audited} entries written during the run (append-only)")

    # idempotency proof, two ways: (1) the same files again, caught by the file hash; (2) with that memory wiped,
    # every row is re-evaluated and must be recognised as a duplicate or as stale
    for label, wipe in (("file-level (same files again)", False), ("row-level (file memory wiped first)", True)):
        if wipe:
            conn.execute("DELETE FROM ingest_log WHERE feed = 'erp'")
        before = fingerprint(conn)
        for f in sc.erp_files:
            src = settings.processed / f.name
            if src.exists():
                shutil.copy(src, settings.inbox / f.name)
        time.sleep(5)
        after = fingerprint(conn)
        left = len(list(settings.inbox.glob("*.csv")))
        print(f"\nReprocessing check, {label}: re-dropped {len(sc.erp_files)} files ({left} left in inbox)")
        print(f"  before {before}\n  after  {after}\n  unchanged: {before == after}")


def fingerprint(conn) -> dict:
    c = counts(conn)
    r = conn.execute("SELECT COALESCE(sum(version), 0) AS v, md5(COALESCE(string_agg(id || content_hash, ',' ORDER BY id), '')) AS h FROM shipment").fetchone()
    c["sum_version"] = int(r["v"])
    c["state_md5"] = r["h"][:10]
    return c


if __name__ == "__main__":
    main()
