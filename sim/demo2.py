"""`python -m sim.demo2`: the milestone 2 demo, end to end.

Starts from the milestone 1 scenario state (runs that scenario first when the database is empty or a previous demo2
changed shipments), then: ingests the contract and bill-of-lading PDFs, computes impact and exposure, prints the
ranked incidents, runs the real triage agent on the top 3 as asha, shows the same incidents as kiran and meera,
runs one playbook under the approval threshold and approves one above it, and prints the audit tail.
Starts and stops the carrier API and the tower itself unless they already answer on their ports.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime

import httpx
import psycopg
from psycopg.rows import dict_row

from sim.demo import ensure_db, start
from sim.pdf_sim import write_bols, write_contracts
from tower import contracts, llm
from tower.agent import fmt_inr
from tower.clock import IST, FileClock, ScenarioClock, clear_clock, write_clock
from tower.config import ROOT, Settings, load_settings
from tower.db import apply_schema, open_pool
from tower.seed import seed

M2_TABLES = ("agent_step", "agent_run", "playbook", "customer_notification", "eta_adjustment", "outbox",
             "incident_impact", "incident_exposure", "stock_transfer")
NOW0 = datetime(2026, 11, 13, 10, 40, tzinfo=IST)


def head(title: str) -> None:
    print(f"\n=== {title} ===")


def ist(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(IST)


def trim(text: str, n: int = 260) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."


class Api:
    def __init__(self, settings: Settings):
        self.base = f"http://{settings.host}:{settings.port}"
        self.tokens = {"asha": settings.token_asha, "kiran": settings.token_kiran, "meera": settings.token_meera}
        self.client = httpx.Client(timeout=900)

    def call(self, user: str, method: str, path: str, **kw) -> httpx.Response:
        return self.client.request(method, self.base + path, headers={"Authorization": f"Bearer {self.tokens[user]}"}, **kw)

    def get(self, user: str, path: str, **kw) -> dict:
        r = self.call(user, "GET", path, **kw)
        r.raise_for_status()
        return r.json()


def run_m1(settings: Settings) -> None:
    print("running the milestone 1 scenario first (about 90 seconds) ...")
    res = subprocess.run([sys.executable, "-m", "sim.demo"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode:
        raise SystemExit(f"M1 demo failed:\n{res.stderr[-800:]}")
    lines = res.stdout.splitlines()
    start_i = next((i for i, ln in enumerate(lines) if ln.startswith("Dirty ERP data")), None)
    if start_i is not None:
        print("M1 dead-letter check (injected vs dead-lettered):")
        for ln in lines[start_i + 1:start_i + 8]:
            print("  " + ln.strip())


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Milestone 2 demo")
    ap.add_argument("--fresh", action="store_true", help="always rerun the milestone 1 scenario first")
    ap.add_argument("--agent-incidents", type=int, default=3, help="how many top incidents to triage (default 3)")
    args = ap.parse_args()
    settings = load_settings()
    print("Tarangini control tower: milestone 2 demo")
    prepare(settings, fresh=args.fresh)
    procs = [p for p in (
        start("carrier_api", "sim.carrier_api", f"http://127.0.0.1:{settings.carrier_api_port}/health", settings),
        start("tower", "tower", f"http://{settings.host}:{settings.port}/health", settings),
    ) if p]
    pool = open_pool(settings.db_url, size=4)
    api = Api(settings)
    try:
        story(settings, pool, api, args)
    finally:
        pool.close()
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


def prepare(settings: Settings, *, fresh: bool = False, use_llm: bool = True) -> None:
    """The milestone 2 starting state: schema and seed, the milestone 1 scenario (rerun when the database is empty, a
    playbook has changed shipments, or `fresh`), milestone 2 tables and bill-of-lading folders emptied, and the scenario
    clock at 10:40 IST running in real time. Shared with the UI launcher (scripts/dev.py). `use_llm=False` makes no call
    to Ollama at all (not even the reachability check)."""
    ensure_db(settings)
    applied = apply_schema(settings.db_url)
    seed(settings)
    print(f"migrations applied ({', '.join(applied)}); capacity and stock reset to the seed")
    if use_llm and not llm.llm_up(settings):
        print("WARNING: Ollama is not answering; the agent will fall back to the deterministic brief")

    with psycopg.connect(settings.db_url, row_factory=dict_row, autocommit=True) as c:
        ships = c.execute("SELECT count(*) n FROM shipment").fetchone()["n"]
        executed = c.execute("SELECT count(*) n FROM playbook WHERE status = 'executed'").fetchone()["n"]
    if fresh or ships < 100 or executed:
        run_m1(settings)
    else:
        print(f"using the existing milestone 1 state ({ships} shipments)")
    with psycopg.connect(settings.db_url, autocommit=True) as c:
        c.execute("TRUNCATE " + ", ".join(M2_TABLES) + " RESTART IDENTITY CASCADE")
        c.execute("UPDATE shipment SET bol_no = NULL, vehicle_no = NULL, driver_name = NULL, driver_phone = NULL, bol_received_at = NULL")
        c.execute("DELETE FROM dead_letter WHERE feed IN ('bol', 'contracts')")
        c.execute("DELETE FROM ingest_log WHERE feed = 'bol'")
    for d in (settings.bol_inbox, settings.bol_processed, settings.bol_rejected):
        d.mkdir(parents=True, exist_ok=True)
        for f in d.iterdir():
            f.unlink()
    seed(settings)

    # "now" is 10:40 IST, the end of the M1 scenario, running in real time from here
    write_clock(settings.clock_file, ScenarioClock(NOW0, 1.0))


def stage(settings: Settings, pool, api: Api, *, embed: bool = True) -> list[dict]:
    """Steps 1 to 3 of the story, no model calls: contract and bill-of-lading PDFs in, impact and exposure computed.
    Needs the tower running (its BOL worker reads the PDFs). Returns the ranked incidents as asha sees them.
    Shared with the UI launcher (scripts/dev.py). `embed=False` makes no Ollama call: a contract PDF whose hash is
    unchanged keeps its embedded chunks, a regenerated one is stored without vectors (retrieval falls back to full text).
    Staging never runs the triage agent."""
    clock = FileClock(settings.clock_file)
    me = api.get("asha", "/me")
    print(f"scenario clock: {clock.now().astimezone(IST):%Y-%m-%d %H:%M} IST; logged in as {me['name']}")

    head("1. Contracts (12 MSA PDFs, two layouts)")
    specs = write_contracts(settings)
    res = contracts.ingest_all(pool, settings, clock, embed=embed)
    tot = sum(r["chunks"] for r in res if r["ok"])
    print(f"generated {len(specs)} PDFs, ingested {sum(1 for r in res if r['ok'])}: {tot} clause chunks, "
          f"{sum(r['embedded'] for r in res if r['ok'])} embedded, {sum(r['contracts_linked'] for r in res if r['ok'])} customer contracts linked")
    for r in res:
        if r.get("mismatches"):
            print(f"  param check: {r['chain']} has {r['mismatches']} clause(s) whose PDF numbers differ from the contract master (dead-lettered)")
    with pool.connection() as conn:
        for d in conn.execute("SELECT reason FROM dead_letter WHERE feed = 'contracts'").fetchall():
            print("  dead letter: " + trim(d["reason"], 200))

    head("2. Bills of lading (60 PDFs dropped in data/inbox/bol)")
    manifest = write_bols(settings)
    bad = [m for m in manifest if m["mode"] not in ("clean", "minor_drift")]
    deadline = time.time() + 90
    while time.time() < deadline and list(settings.bol_inbox.glob("*.pdf")):
        time.sleep(1)
    with pool.connection() as conn:
        st = conn.execute("SELECT count(*) FILTER (WHERE bol_no IS NOT NULL) AS stored FROM shipment").fetchone()
        dls = conn.execute("SELECT reason_code, reason, suggested_fix FROM dead_letter WHERE feed = 'bol' ORDER BY id").fetchall()
    print(f"dropped {len(manifest)} BOLs ({len(bad)} deliberately bad, 1 with a 1.2% value drift inside the 2% tolerance); "
          f"{st['stored']} stored driver and vehicle on their shipment")
    shown: dict[str, int] = {}
    for d in dls:
        shown[d["reason_code"]] = shown.get(d["reason_code"], 0) + 1
    for code, n in shown.items():
        ex = next(d for d in dls if d["reason_code"] == code)
        print(f"  dead letter {code:<17} x{n}  e.g. {trim(ex['reason'], 70)}  -> {trim(ex['suggested_fix'] or '', 60)}")

    head("3. Impact and money at risk (deterministic engine)")
    api.call("asha", "POST", "/incidents/recompute").raise_for_status()
    inc = api.get("asha", "/incidents")["items"]
    print(f"{'rank':>4} {'id':>3} {'kind':<9} {'incident':<42} {'ships':>5} {'value':>14} {'exposure':>12} {'waivable':>11}  notify by")
    for i in inc:
        by = f"{ist(i['notify_deadline']):%H:%M} IST" if i["notify_deadline"] else "-"
        print(f"{i['rank']:>4} {i['id']:>3} {i['kind']:<9} {trim(i['title'], 42):<42} {i['shipments']:>5} {fmt_inr(i['value_inr']):>14} "
              f"{fmt_inr(i['net_inr']):>12} {fmt_inr(i['waivable_inr']):>11}  {by}")
    print(f"total exposure {fmt_inr(sum(i['net_inr'] for i in inc))} across {sum(i['shipments'] for i in inc)} shipment-incident pairs")
    return inc


def story(settings: Settings, pool, api: Api, args) -> None:
    inc = stage(settings, pool, api)

    head(f"4. Triage agent as asha, top {args.agent_incidents} incidents (local model, one call at a time)")
    runs, all_pbs = [], {}
    for i in inc[:args.agent_incidents]:
        t0 = time.time()
        r = api.call("asha", "POST", f"/incidents/{i['id']}/triage")
        r.raise_for_status()
        out = r.json()
        run = out["run"]
        runs.append((i, run))
        all_pbs[i["id"]] = out["playbooks"]
        print(f"\n[incident {i['id']}] {i['title']}")
        print(f"  outcome {run['outcome']}{' (' + run['fallback_reason'] + ')' if run['fallback_reason'] else ''}; {run['ms'] / 1000:.1f} s; "
              f"tokens {run['prompt_tokens']} prompt + {run['completion_tokens']} completion over {run['llm_calls']} model calls; "
              f"{len(run['steps'])} tool steps ({', '.join(s['tool'] for s in run['steps'])})")
        if run["honesty_failures"]:
            print("  honesty-check failures this run: " + trim("; ".join(run["honesty_failures"]), 300))
        print("  summary: " + trim(run["summary"], 420))
        for p in out["playbooks"]:
            print(f"  playbook {p['id']:>3} {p['action_type']:<16} goods {fmt_inr(p['goods_value_inr']):>13}  cost {fmt_inr(p['est_cost_inr']):>10}  "
                  f"saving {fmt_inr(p['est_saving_inr']):>11}  clause {p['clause_chain']} {p['clause_id']}  [{p['source']}]")
        print(f"  wall time of the request: {time.time() - t0:.1f} s")
    fb = [(i['id'], r['fallback_reason']) for i, r in runs if r["outcome"] != "ok"]
    hf = sum(len(r["honesty_failures"]) for _, r in runs)
    print(f"\nagent summary: {len(runs) - len(fb)} of {len(runs)} runs from the model, {len(fb)} fallback(s) {fb if fb else ''}, "
          f"{hf} honesty-check rejection(s) caught and retried; total {sum(r['ms'] for _, r in runs) / 1000:.0f} s, "
          f"{sum(r['prompt_tokens'] for _, r in runs)} prompt / {sum(r['completion_tokens'] for _, r in runs)} completion tokens")

    head("5. Same incidents as kiran (Dispatcher South, DC-HYD only) and meera (Finance)")
    for i, _ in runs:
        k = api.call("kiran", "GET", f"/incidents/{i['id']}/brief")
        if k.status_code == 404:
            print(f"incident {i['id']} ({i['kind']}): asha sees {i['shipments']} shipments / {fmt_inr(i['net_inr'])}; kiran gets 404 (hidden, no shipment of his)")
        else:
            kb = k.json()
            print(f"incident {i['id']} ({i['kind']}): asha sees {i['shipments']} shipments / {fmt_inr(i['net_inr'])}; "
                  f"kiran sees {kb['incident']['shipments']} / {fmt_inr(kb['incident']['net_inr'])} "
                  f"(DCs in his view: {[w['id'] for w in kb['chain']['warehouses']]}); playbooks visible to him: {len(kb['playbooks'])}")
    visible = next((i for i, _ in runs if api.call("kiran", "GET", f"/incidents/{i['id']}/brief").status_code == 200), None)
    if visible is None:
        visible = next((i for i in inc if api.call("kiran", "GET", f"/incidents/{i['id']}/brief").status_code == 200), None)
    if visible:
        iid = visible["id"]
        print(f"\nmasking proof on incident {iid} (first shipment with a bill of lading):")
        for u in ("asha", "kiran", "meera"):
            items = api.get(u, f"/incidents/{iid}/brief", params={"limit": 200})["shipments"]["items"]
            s = next((x for x in items if x.get("driver_phone") or x.get("bol_no")), items[0] if items else {})
            print(f"  {u:<6} {s.get('shipment_id')}: driver={s.get('driver_name', '<removed>')!s:<14} phone={s.get('driver_phone', '<removed>')}")
        for u in ("asha", "kiran", "meera"):
            c = api.get(u, "/network")["carriers"][0]
            print(f"  {u:<6} carrier {c['id']}: dispatch_phone={c.get('dispatch_phone', '<removed>')}  bank={c.get('bank_account', '<removed>')}")
    with pool.connection() as conn:
        kol = conn.execute("SELECT id FROM shipment WHERE dc_id = 'DC-KOL' ORDER BY id LIMIT 1").fetchone()["id"]
    kl = api.get("kiran", "/shipments", params={"dc": "DC-KOL"})
    d = api.call("kiran", "GET", f"/shipments/{kol}")
    print(f"  kiran asks for DC-KOL shipments: total={kl['total']}; GET /shipments/{kol}: HTTP {d.status_code}; "
          f"kiran's shipment total {api.get('kiran', '/shipments')['total']} of {api.get('asha', '/shipments')['total']}")
    print(f"  meera tries /playbooks/<id>/run: HTTP "
          f"{api.call('meera', 'POST', '/playbooks/' + str(next(p['id'] for ps in all_pbs.values() for p in ps)) + '/run').status_code} (finance cannot execute)")
    print(f"  no token: HTTP {httpx.get(api.base + '/shipments').status_code}")

    head("6. Actions: under the threshold, and above it")
    pbs = [p for ps in all_pbs.values() for p in ps]
    thr = settings.approval_threshold_inr
    small = next((p for p in pbs if float(p["goods_value_inr"]) <= thr and p["action_type"] == "notify_customers"
                  and "DC-HYD" in p["dc_scope"] and len(p["dc_scope"]) == 1), None) or next(
        (p for p in pbs if float(p["goods_value_inr"]) <= thr), None)
    big = next((p for p in pbs if float(p["goods_value_inr"]) > thr and p["action_type"] in ("swap_carrier", "reroute_via_dc")), None) or \
        next((p for p in pbs if float(p["goods_value_inr"]) > thr), None)
    if small:
        actor = "kiran" if small["dc_scope"] == ["DC-HYD"] else "asha"
        r = api.call(actor, "POST", f"/playbooks/{small['id']}/run")
        j = r.json()
        print(f"{actor} runs playbook {small['id']} ({small['action_type']}, goods {fmt_inr(small['goods_value_inr'])}, threshold {fmt_inr(thr)}): "
              f"HTTP {r.status_code} -> {j.get('status')}; exposure {fmt_inr(j['result']['exposure_before_inr'])} -> "
              f"{fmt_inr(j['result']['exposure_after_inr'])}; {j['result']['messages_queued']} messages queued")
        r2 = api.call(actor, "POST", f"/playbooks/{small['id']}/run").json()
        print(f"  same call again: replayed={r2.get('replayed')}, idempotency key {r2.get('idempotency_key')}")
    if big:
        r = api.call("asha", "POST", f"/playbooks/{big['id']}/run")
        j = r.json()
        print(f"asha runs playbook {big['id']} ({big['action_type']}, goods {fmt_inr(big['goods_value_inr'])}): HTTP {r.status_code} -> {j['status']} "
              f"({j.get('message')})")
        k = api.call("kiran", "POST", f"/playbooks/{big['id']}/approve")
        print(f"  kiran tries to approve it: HTTP {k.status_code} {k.json().get('detail')}")
        a = api.call("asha", "POST", f"/playbooks/{big['id']}/approve")
        aj = a.json()
        print(f"  asha approves: HTTP {a.status_code} -> {aj.get('status')}; exposure {fmt_inr(aj['result']['exposure_before_inr'])} -> "
              f"{fmt_inr(aj['result']['exposure_after_inr'])}; changed {aj['result']['changed']} shipments; sample {aj['result']['changes_sample'][:1]}")
    time.sleep(3)   # let the outbox worker post
    hook = httpx.get(f"http://127.0.0.1:{settings.carrier_api_port}/hooks/chat", headers={"X-Api-Key": settings.carrier_api_key}).json()
    with pool.connection() as conn:
        ob = conn.execute("SELECT status, count(*) n FROM outbox GROUP BY 1 ORDER BY 1").fetchall()
    print(f"outbox: {', '.join(f'{r['n']} {r['status']}' for r in ob)}; the mock chat webhook has recorded {hook['count']} messages")

    head("7. Audit trail (latest 8)")
    a = api.get("asha", "/audit", params={"limit": 8})
    for r in a["items"]:
        det = r["detail"]
        brief = {k: det[k] for k in list(det)[:3]}
        print(f"  #{r['id']:<5} {ist(r['at']):%H:%M:%S} {r['actor']:<7} {r['action']:<26} {r['target']:<14} dcs={r['dc_ids']} {trim(str(brief), 90)}")
    k = api.get("kiran", "/audit", params={"limit": 200})
    print(f"audit rows visible: asha {a['total']}, kiran {k['total']} (only his DC or his own actions), meera {api.get('meera', '/audit')['total']}")

    with pool.connection() as conn:
        nxt = conn.execute("SELECT count(*) n FROM dead_letter WHERE status = 'waiting'").fetchone()["n"]
    print(f"\nwaiting dead letters (all feeds): {nxt}")
    print(f"final clock: {FileClock(settings.clock_file).now().astimezone(IST):%H:%M:%S} IST")


if __name__ == "__main__":
    main()
