"""`python scripts/dev.py`: everything the UI needs, in one command (milestone 3).

1. Postgres: checked, started with docker compose if it is not answering.
2. Migrations: db/schema.sql and db/migrations/*.sql (idempotent).
3. Scenario: staged with the milestone 2 demo's own steps (sim.demo2.prepare and sim.demo2.stage: the 05:00 to 10:40
   morning, contracts, bills of lading, impact and exposure; no model calls) when the database has no staged morning
   yet, or when --stage / --fresh asks for it. Otherwise the current state is kept (agent runs and executed plans
   included) and only the scenario clock is set back to 10:40 IST, running in real time.
4. The mock carrier API (:9100), the tower (:8500) and the Next.js UI (:4400). Ctrl+C stops what this script started.

Options: --stage (restage from the milestone 1 state), --fresh (rerun the milestone 1 scenario too), --prod (next build,
then next start instead of next dev), --no-web (backend only), --no-llm (no Ollama call of any kind while starting: no
reachability check, contract clauses staged without embeddings). Starting never runs the triage agent either way; the
briefs show the agent runs already in the database.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from sim.demo import ensure_db, start, up  # noqa: E402
from sim.demo2 import NOW0, Api, prepare, stage  # noqa: E402
from tower.clock import ScenarioClock, clear_clock, write_clock  # noqa: E402
from tower.config import Settings, load_settings  # noqa: E402
from tower.db import apply_schema, open_pool  # noqa: E402

WEB = ROOT / "web"
WEB_PORT = 4400


def staged(settings: Settings) -> bool:
    """A staged morning: the milestone 1 shipments, the contract chunks, the bills of lading and computed exposure."""
    with psycopg.connect(settings.db_url, row_factory=dict_row, autocommit=True) as c:
        n = c.execute("""SELECT (SELECT count(*) FROM shipment) AS ships, (SELECT count(*) FROM contract_chunk) AS chunks,
                                (SELECT count(*) FROM shipment WHERE bol_no IS NOT NULL) AS bols,
                                (SELECT count(*) FROM incident_exposure) AS exposure""").fetchone()
    return n["ships"] >= 100 and n["chunks"] > 0 and n["bols"] > 0 and n["exposure"] > 0


def start_web(settings: Settings, prod: bool) -> subprocess.Popen:
    next_bin = WEB / "node_modules" / "next" / "dist" / "bin" / "next"
    if not next_bin.exists():
        raise SystemExit("web/node_modules is missing: run `npm install` in web/ once, then start again")
    node = shutil.which("node") or "node"
    env = {**os.environ, "CT_ENV_FILE": str(ROOT / ".env"), "CT_TOWER_URL": f"http://{settings.host}:{settings.port}",
           "NEXT_TELEMETRY_DISABLED": "1"}
    if prod:
        print("  web: next build ...")
        subprocess.run([node, str(next_bin), "build"], cwd=WEB, env=env, check=True)
    log = open(settings.data_dir / "logs" / "web.log", "wb")  # noqa: SIM115 - closed with the process
    cmd = [node, str(next_bin), "start" if prod else "dev", "-H", "127.0.0.1", "-p", str(WEB_PORT)]
    proc = subprocess.Popen(cmd, cwd=WEB, env=env, stdout=log, stderr=log)
    url = f"http://127.0.0.1:{WEB_PORT}/"
    for _ in range(240):
        if up(url):
            print(f"  web: started (pid {proc.pid})")
            return proc
        if proc.poll() is not None:
            raise SystemExit("the UI did not start; see data/logs/web.log")
        time.sleep(0.5)
    stop_tree(proc)
    raise SystemExit("the UI did not answer in 2 minutes; see data/logs/web.log")


def stop_tree(proc: subprocess.Popen) -> None:
    """Next starts worker processes of its own: stop the whole tree."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", action="store_true", help="restage the morning even if one is staged")
    ap.add_argument("--fresh", action="store_true", help="rerun the milestone 1 scenario first, then stage")
    ap.add_argument("--prod", action="store_true", help="next build and next start instead of next dev")
    ap.add_argument("--no-web", action="store_true", help="start the backend only")
    ap.add_argument("--no-llm", action="store_true", help="no Ollama call while starting (no embeddings when staging)")
    args = ap.parse_args()
    settings = load_settings()
    print("Tarangini control tower: milestone 3 launcher")

    ensure_db(settings)
    print(f"database up; migrations applied ({', '.join(apply_schema(settings.db_url))})")
    restage = args.stage or args.fresh or not staged(settings)
    if restage:
        print("staging the morning (milestone 2 demo steps 1 to 3; the milestone 1 scenario first when needed)")
        prepare(settings, fresh=args.fresh, use_llm=not args.no_llm)
    else:
        print("a staged morning is in the database: keeping it; scenario clock set to 10:40 IST")
        write_clock(settings.clock_file, ScenarioClock(NOW0, 1.0))

    procs: list[subprocess.Popen] = []
    web: subprocess.Popen | None = None
    try:
        for p in (start("carrier_api", "sim.carrier_api", f"http://127.0.0.1:{settings.carrier_api_port}/health", settings),
                  start("tower", "tower", f"http://{settings.host}:{settings.port}/health", settings)):
            if p:
                procs.append(p)
        if restage:
            pool = open_pool(settings.db_url, size=4)
            try:
                stage(settings, pool, Api(settings), embed=not args.no_llm)
            finally:
                pool.close()
        if not args.no_web:
            web = start_web(settings, args.prod)
        print(f"\nready: UI http://127.0.0.1:{WEB_PORT}/   tower http://{settings.host}:{settings.port}   "
              f"carrier API http://127.0.0.1:{settings.carrier_api_port}")
        print("logs in data/logs/ (tower.log, carrier_api.log, web.log). Ctrl+C stops everything this script started.")
        while all(p.poll() is None for p in procs) and (web is None or web.poll() is None):
            time.sleep(1)
        print("a process stopped on its own; shutting down the rest")
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        if web:
            stop_tree(web)
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
        clear_clock(settings.clock_file)
        print("stopped the processes this script started")


if __name__ == "__main__":
    main()
