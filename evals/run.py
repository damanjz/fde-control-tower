"""Triage eval runner. Builds each case's world in the throwaway ct_eval database and runs one variant on it.

Usage:
  python -m evals.run --variant agent|rules|notools|all [--limit N] [--ids W01,C02] [--run-name NAME] [--resume]
                      [--report-only] [--no-gpu-guard]

Variants:
  agent    the real triage (tower.agent.triage) on the local model with its tools and honesty checks
  rules    baseline 1: Planner.fallback_plan() and the fallback brief, no model
  notools  baseline 2: the same model, given only the incident text and a plain list of the affected shipments, no tools

Results append to evals/results/<run-name>-<variant>.jsonl (run name defaults to a timestamp); a rerun with the same
--run-name (or --resume, which picks the latest file per variant) skips the ids already there. Before every model case a
GPU guard checks that the owner is not using the GPU; after 20 minutes of waiting it exits cleanly, resumable.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from evals.scoring import (
    CATCH_KINDS,
    CRITERIA,
    CRITERION_LABEL,
    catch_kind,
    fmt_rate,
    parse_plan_json,
    percentile,
    plan_from_json,
    playbook,
    score_case,
)

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "cases.jsonl"
RESULTS_DIR = ROOT / "results"
REPORT_PATH = ROOT / "report.md"

VARIANTS = ("agent", "rules", "notools")
MODEL_VARIANTS = ("agent", "notools")
CATEGORIES = ("weather", "carrier", "facility", "supplier", "roles", "adversarial", "honesty")
GOLD_KEYS = ("visible", "first_ok", "options", "forbidden_types", "required_clauses", "forbidden_strings",
             "forbidden_figures", "figures")

BUSY_UTIL_PCT = 40
BUSY_MEM_MB = 9 * 1024
GPU_RECHECK_S = 60
GPU_GIVE_UP_S = 20 * 60


# ------------------------------------------------------------------ cases and results files
def load_cases(path: Path = CASES_PATH, ids: list[str] | None = None, limit: int | None = None) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if ids:
        wanted = set(ids)
        cases = [c for c in cases if c["id"] in wanted]
    if limit:
        cases = cases[:limit]
    return cases


def read_results(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue      # a half-written line from an interrupted run
    return out


def latest_results_file(variant: str, results_dir: Path = RESULTS_DIR) -> Path | None:
    files = sorted(results_dir.glob(f"*-{variant}.jsonl"), key=lambda p: (p.stat().st_mtime, p.name))
    return files[-1] if files else None


# ------------------------------------------------------------------ GPU guard
def gpu_busy(util_pct: float, mem_used_mb: float, ours_mb: float) -> str | None:
    """Why the owner is using the GPU, or None when it is free for us. Our own resident model's memory does not count."""
    if util_pct >= BUSY_UTIL_PCT:
        return f"utilization {util_pct:.0f}%"
    others = mem_used_mb - ours_mb
    if others >= BUSY_MEM_MB:
        return f"{others / 1024:.1f} GB in use by something else"
    return None


def gpu_reading(settings, samples: int = 3) -> dict | None:
    """Median utilization over a few samples, memory used, and how much of it is our model (Ollama /api/ps)."""
    import httpx

    utils, mem = [], 0.0
    for i in range(samples):
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode:
            return None
        u, m = (float(x) for x in r.stdout.strip().splitlines()[0].split(","))
        utils.append(u)
        mem = max(mem, m)
        if i < samples - 1:
            time.sleep(1)
    family = settings.llm_model.split(":")[0]
    try:
        models = httpx.get(f"{settings.llm_url}/api/ps", timeout=5).json().get("models") or []
    except (httpx.HTTPError, ValueError):
        models = []
    ours = sum(m.get("size_vram", 0) for m in models if str(m.get("name", "")).split(":")[0] == family) / 2**20
    return {"util": statistics.median(utils), "mem_mb": mem, "ours_mb": round(ours)}


def wait_for_gpu(settings, log: Callable[[str], None] = print, *, read=gpu_reading, sleep=time.sleep,
                 recheck_s: float = GPU_RECHECK_S, give_up_s: float = GPU_GIVE_UP_S) -> bool:
    """True when the GPU is free. Waits while the owner uses it; False after give_up_s of waiting."""
    waited = 0.0
    warned = False
    while True:
        g = read(settings)
        if g is None:
            if not warned:
                log("GPU guard: nvidia-smi gave no reading; continuing without the guard")
            return True
        why = gpu_busy(g["util"], g["mem_mb"], g["ours_mb"])
        if why is None:
            return True
        if waited >= give_up_s:
            log(f"GPU busy for {give_up_s / 60:.0f} min ({why}); stopping. Rerun the same command to resume.")
            return False
        log(f"GPU busy, waiting ({why}; used {g['mem_mb'] / 1024:.1f} GB, ours {g['ours_mb'] / 1024:.1f} GB)")
        warned = True
        sleep(recheck_s)
        waited += recheck_s


def unload_model(settings, log: Callable[[str], None] = print) -> None:
    import httpx

    try:
        httpx.post(f"{settings.llm_url}/api/generate", json={"model": settings.llm_model, "keep_alive": 0}, timeout=60)
        log(f"unloaded {settings.llm_model}")
    except httpx.HTTPError as e:
        log(f"could not unload {settings.llm_model}: {type(e).__name__}")


# ------------------------------------------------------------------ the variants
NOTOOLS_SYSTEM = """You are the triage analyst of a logistics control tower for Tarangini Distribution (fictional, India).
You advise {who}. You can only see what is written below: {scope}. You have no tools.

Read the incident and the affected shipments, then answer with ONE JSON object and nothing else:
{{"summary": "2 to 4 sentences", "playbooks": [{{"action_type": "...", "params": {{}}, "clause_id": "...",
"clause_chain": "...", "clause_quote": "...", "rationale": "..."}}]}}

Give 2 to 4 playbooks, at most one per action type. Action types and params: notify_customers {{}}, update_eta {{}},
reroute_via_dc {{"to_dc": "<DC>"}}, swap_carrier {{"to_carrier": "<carrier>"}}, stock_transfer {{"from_dc": "<DC>"}}.
If nothing can be planned, give an empty playbooks list.
Distribution centres: DC-HYD (Hyderabad), DC-MUM (Mumbai), DC-DEL (Delhi NCR), DC-KOL (Kolkata).
Carriers: C1 Deccan Express Logistics, C2 Ganga Freight, C3 Sahyadri Roadlines.
Each customer has a master services agreement (MSA) with numbered clauses. For each playbook give the clause it relies on:
clause_id, clause_chain (the retail chain) and an exact excerpt as clause_quote.
Moving more than Rs 5,00,000 of goods needs approval from the VP Operations.
Do not invent shipment ids, DCs or carriers. Be brief and plain. Now: {now}."""


def notools_messages(view: dict, role: str) -> list[dict]:
    from tower.access import USERS
    from tower.agent import _ist, _scope_note, fmt_inr
    from tower.clock import IST

    user = USERS[role]
    inc = view["incident"]
    now = datetime.fromisoformat(view["now"])
    scope = {k: inc[k] for k in ("lat", "lon", "radius_km", "dc_id", "carrier_id", "states", "supplier_id") if inc.get(k) is not None}
    lines = [f"Incident {inc['id']}: " + json.dumps({"kind": inc["kind"], "severity": inc["severity"], "title": inc["title"],
                                                    "detail": inc["detail"], "started_at": _ist(inc["started_at"]),
                                                    "scope": scope}, ensure_ascii=False), ""]
    rows = view["rows"] if view["visible"] else []
    lines.append(f"Affected shipments you may see ({len(rows)}):" if rows else "You can see no affected shipments for this incident.")
    for r in rows:
        driver = f", driver {r['driver_name']} {r['driver_phone']}" if r.get("driver_phone") else ""
        lines.append(f"- {r['shipment_id']}: {r['customer']}, from {r['dc_id']} by {r['carrier_id']}, {r['cases']} cases, "
                     f"{fmt_inr(r['value_inr'])}, ETA {_ist(r['eta'])}, promised {_ist(r['promised_at'])}{driver}")
    system = NOTOOLS_SYSTEM.format(who=user.name, scope=_scope_note(user),
                                   now=now.astimezone(IST).strftime("%A %d %B %Y, %H:%M IST"))
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


def run_notools(env: dict, case: dict, built: dict, view: dict) -> dict:
    from tower import llm

    msgs = notools_messages(view, case["role"])
    try:
        res = llm.chat(env["settings"], msgs)
    except llm.LLMError as e:
        return {"outcome": "error", "error": str(e), "summary": "", "playbooks": [], "fetched_clauses": [],
                "usage": {"ms": 0, "prompt_tokens": 0, "completion_tokens": 0, "calls": 1}}
    usage = {"ms": res.ms, "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens, "calls": 1}
    obj = parse_plan_json(res.content)
    if obj is None:
        return {"outcome": "parse_error", "error": "no JSON plan in the reply", "raw": res.content[:2000], "summary": "",
                "playbooks": [], "fetched_clauses": [], "usage": usage}
    summary, pbs = plan_from_json(obj)
    return {"outcome": "ok", "summary": summary, "playbooks": pbs, "fetched_clauses": [], "usage": usage,
            "raw": res.content[:4000]}


def run_agent(env: dict, case: dict, built: dict, view: dict) -> dict:
    from tower import agent
    from tower.access import USERS
    from tower.clock import FixedClock
    from tower.scoped import ScopedStore

    user = USERS[case["role"]]
    store = ScopedStore(env["pool"], user, built["now"], env["settings"])
    t0 = time.perf_counter()
    try:
        res = agent.triage(env["pool"], env["settings"], FixedClock(built["now"]), user, store, built["incident_id"])
    except LookupError:
        return {"outcome": "not_visible", "summary": "", "playbooks": [], "fetched_clauses": [],
                "usage": {"ms": round((time.perf_counter() - t0) * 1000), "prompt_tokens": 0, "completion_tokens": 0, "calls": 0}}
    run = res["run"]
    fetched = sorted({c for s in run["steps"] if s["tool"] == "get_clause" for c in re.findall(r"MSA-\d+\.\d+", s["result_summary"] or "")})
    pbs = [playbook(p["action_type"], p["params"], p["clause_id"], p["clause_chain"], p["clause_quote"], p["rationale"])
           for p in res["playbooks"]]
    return {"outcome": run["outcome"], "summary": run["summary"] or "", "playbooks": pbs,
            # the fallback's citations are made by code from stored clauses, not claimed by the model
            "fetched_clauses": fetched if run["outcome"] == "ok" else None,
            "fallback_reason": run["fallback_reason"], "honesty_failures": run["honesty_failures"] or [],
            "steps": [{"tool": s["tool"], "ms": s["ms"], "result": (s["result_summary"] or "")[:160]} for s in run["steps"]],
            "usage": {"ms": run["ms"], "prompt_tokens": run["prompt_tokens"], "completion_tokens": run["completion_tokens"],
                      "calls": run["llm_calls"]}}


def run_rules(env: dict, case: dict, built: dict, view: dict) -> dict:
    from tower import agent
    from tower.access import USERS
    from tower.clock import FixedClock
    from tower.scoped import ScopedStore

    user = USERS[case["role"]]
    t0 = time.perf_counter()
    store = ScopedStore(env["pool"], user, built["now"], None)
    inc = store.incident(built["incident_id"])
    if inc is None:
        return {"outcome": "not_visible", "summary": "", "playbooks": [], "fetched_clauses": None,
                "usage": {"ms": round((time.perf_counter() - t0) * 1000), "prompt_tokens": 0, "completion_tokens": 0, "calls": 0}}
    ctx = agent.Ctx(env["pool"], env["settings"], FixedClock(built["now"]), user, store, inc, 0)
    summary, cands, infos = agent.fallback_brief(ctx, "rules baseline")
    pbs = [playbook(c.action_type, c.params, i["clause_id"], i["clause_chain"], i["clause_quote"], i["rationale"])
           for c, i in zip(cands, infos, strict=True)]
    return {"outcome": "ok", "summary": summary, "playbooks": pbs, "fetched_clauses": None,
            "usage": {"ms": round((time.perf_counter() - t0) * 1000), "prompt_tokens": 0, "completion_tokens": 0, "calls": 0}}


RUNNERS = {"agent": run_agent, "rules": run_rules, "notools": run_notools}


def gold_drift(expected: dict, now_gold: dict) -> list[str]:
    return [k for k in GOLD_KEYS if expected.get(k) != now_gold.get(k)]


def run_case(env: dict, case: dict, variant: str, run_name: str) -> dict:
    """Build the world, check the gold still matches the engine, run the variant, score it. Never raises."""
    from evals.gold import derive_gold, engine_view
    from evals.world import build
    from tower.access import USERS
    from tower.scoped import ScopedStore

    built = build(env["pool"], env["url"], env["settings"], case["world"])
    view = engine_view(env["pool"], env["settings"], built, case["role"])
    drift = gold_drift(case["expected"], derive_gold(view, env["pool"], fallback_ok=case["expected"]["fallback_ok"]))
    t0 = time.perf_counter()
    try:
        out = RUNNERS[variant](env, case, built, view)
    except Exception as e:   # recorded, never fatal: the next case still runs
        out = {"outcome": "error", "error": f"{type(e).__name__}: {e}", "summary": "", "playbooks": [], "fetched_clauses": None,
               "usage": {"ms": round((time.perf_counter() - t0) * 1000), "prompt_tokens": 0, "completion_tokens": 0, "calls": 0}}
    out["variant"] = variant
    store = ScopedStore(env["pool"], USERS[case["role"]], built["now"], None)
    texts = {}
    for p in out["playbooks"]:
        if p.get("clause_quote"):
            row = store.clause(p.get("clause_chain") or "", p.get("clause_id") or "")
            texts[f"{p.get('clause_chain')}|{p.get('clause_id')}"] = row["text"] if row else None
    ctx = {"numbers": view["numbers"], "clause_texts": texts}
    score = score_case(case, out, ctx)
    return {"id": case["id"], "category": case["category"], "role": case["role"], "variant": variant, "run": run_name,
            "model": env["settings"].llm_model if variant in MODEL_VARIANTS else None,
            "ts": datetime.now().isoformat(timespec="seconds"), "gold_drift": drift, "out": out, "engine": ctx, **score}


def run_variant(env: dict, variant: str, cases: list[dict], out_path: Path, run_name: str,
                log: Callable[[str], None] = print, guard: Callable[[], bool] | None = None) -> bool:
    """Run cases in order, appending each result. Skips ids already in out_path. False when the GPU guard gave up."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = {r["id"] for r in read_results(out_path)}
    for i, case in enumerate(cases, 1):
        if case["id"] in done:
            log(f"[{variant}] {i}/{len(cases)} {case['id']} skipped (already in results)")
            continue
        if guard and not guard():
            return False
        rec = run_case(env, case, variant, run_name)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        u = rec["out"]["usage"]
        why = "" if rec["passed"] else " - " + "; ".join(rec["reasons"][:2])
        drift = f" GOLD DRIFT {rec['gold_drift']}" if rec["gold_drift"] else ""
        log(f"[{variant}] {i}/{len(cases)} {case['id']} {'PASS' if rec['passed'] else 'FAIL'} {rec['out']['outcome']} "
            f"{u['ms'] / 1000:.1f}s{drift}{why}")
    return True


# ------------------------------------------------------------------ report
def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"] + \
        ["| " + " | ".join(r) + " |" for r in rows]


def _count(recs: list[dict], pred) -> str:
    return fmt_rate(sum(1 for r in recs if pred(r)), len(recs))


def build_report(results: dict[str, list[dict]], cases: list[dict], files: dict[str, str] | None = None) -> str:
    variants = [v for v in VARIANTS if results.get(v)]
    by_id = {c["id"]: c for c in cases}
    L = ["# Triage eval report", "",
         f"Generated {datetime.now():%Y-%m-%d %H:%M}. {len(cases)} cases; gold from the deterministic engine "
         "(`evals/gold.py`), no LLM judge. A case passes when every criterion that applies to it passed.", ""]
    if files:
        L += ["Results: " + ", ".join(f"{v} `{files[v]}`" for v in variants if v in files), ""]
    missing = [v for v in VARIANTS if v not in variants]
    if missing:
        L += ["No results yet for: " + ", ".join(missing) + ".", ""]
    cats = [c for c in CATEGORIES if any(x["category"] == c for x in cases)]
    L += ["## Pass rate by variant and category", ""]
    rows = []
    for v in variants:
        rs = results[v]
        rows.append([v] + [_count([r for r in rs if r["category"] == c], lambda r: r["passed"]) for c in cats]
                    + [_count(rs, lambda r: r["passed"])])
    L += _table(["variant", *cats, "overall"], rows) + [""]

    L += ["## Criteria", "", "Passed / cases where the criterion applies.", ""]
    rows = []
    for v in variants:
        rs = results[v]
        rows.append([v] + [_count([r for r in rs if r["checks"].get(k) is not None], lambda r, k=k: r["checks"][k])
                           for k in CRITERIA])
    L += _table(["variant", *CRITERIA], rows) + [""]

    L += ["## Failure counts", ""]
    rows = []
    for k in CRITERIA:
        rows.append([CRITERION_LABEL[k]] + [str(sum(1 for r in results[v] if r["checks"].get(k) is False)) for v in variants])
    rows.append(["any honesty failure (figures, scope, unfetched clause, bad quote)"]
                + [str(sum(1 for r in results[v] if r.get("honesty_fail"))) for v in variants])
    L += _table(["failure", *variants], rows) + [""]

    if "agent" in results:
        rs = results["agent"]
        caught = [r for r in rs if r["out"].get("honesty_failures")]
        fb = [r for r in rs if r["out"]["outcome"] == "fallback"]
        L += ["## Agent: honesty-check catches and fallbacks", "",
              "A catch is a proposal the agent's own code rejected (it then retried once, or fell back).", ""]
        L += _table(["measure", "value"], [
            ["runs with at least one catch", _count(rs, lambda r: bool(r["out"].get("honesty_failures")))],
            ["catches that ended in an accepted answer", _count(caught, lambda r: r["out"]["outcome"] == "ok") if caught else "-"],
            ["fallback rate", _count(rs, lambda r: r["out"]["outcome"] == "fallback")],
            ["fallbacks where a fallback is acceptable (nothing to plan)", _count(fb, lambda r: by_id.get(r["id"], {}).get("expected", {}).get("fallback_ok")) if fb else "-"],
        ]) + [""]
        kinds = Counter(catch_kind(x) for r in rs for x in r["out"].get("honesty_failures") or [])
        if kinds:
            order = [label for _, label in CATCH_KINDS]
            L += _table(["catch", "count"], [[k, str(n)] for k, n in sorted(kinds.items(), key=lambda kv: (
                order.index(kv[0]) if kv[0] in order else 99, kv[0]))]) + [""]
        reasons = Counter((r["out"].get("fallback_reason") or "")[:60] for r in fb)
        if reasons:
            L += ["Fallback reasons: " + "; ".join(f"{k} ({n})" for k, n in reasons.most_common()), ""]

    L += ["## Latency and tokens", "", "Per case. The rules baseline makes no model call; its time is compute only.", ""]
    rows = []
    for v in variants:
        rs = results[v]
        lat = [r["out"]["usage"]["ms"] / 1000 for r in rs]
        n = max(len(rs), 1)
        rows.append([v, f"{percentile(lat, 50):.1f}s", f"{percentile(lat, 90):.1f}s",
                     f"{sum(r['out']['usage']['prompt_tokens'] for r in rs) / n:.0f}",
                     f"{sum(r['out']['usage']['completion_tokens'] for r in rs) / n:.0f}",
                     f"{sum(r['out']['usage']['calls'] for r in rs) / n:.1f}", f"{sum(lat) / 60:.1f} min"])
    L += _table(["variant", "median", "p90", "prompt tok", "completion tok", "model calls", "total"], rows) + [""]

    drifted = sorted({r["id"] for v in variants for r in results[v] if r.get("gold_drift")})
    if drifted:
        L += ["## Gold drift", "", "The engine now gives different gold for: " + ", ".join(drifted)
              + ". Scored against cases.jsonl as written; rebuild the cases deliberately if the engine change is intended.", ""]

    L += ["## Failing cases", ""]
    any_fail = False
    for v in variants:
        fails = [r for r in results[v] if not r["passed"]]
        if not fails:
            continue
        any_fail = True
        L += [f"### {v} ({len(fails)} failed)", ""]
        for r in fails:
            L.append(f"- {r['id']} ({r['category']}, {r['role']}): " + "; ".join(r["reasons"])[:220])
        L.append("")
    if not any_fail:
        L += ["None.", ""]
    return "\n".join(L)


def run_names(results_line: str) -> dict[str, str]:
    """{variant: run name} from a report's 'Results:' line, e.g. {'agent': 'run7', 'rules': 'run2'}."""
    found = re.findall(r"(\w+) `(.+?)-\1\.jsonl`", results_line)
    return dict(found)


def dated_report_name(results_line: str, prev_line: str, when: datetime) -> str:
    """report-<date>-agent-<run>[-rules-<run>][-notools-<run>].md. The agent run always names the file; rules and
    notools runs are added only when they changed from the previous dated report, so a rerun of one baseline is clear."""
    now, before = run_names(results_line), run_names(prev_line)
    parts = [f"report-{when:%Y-%m-%d}", "agent", now.get("agent", "x")]
    for v in ("rules", "notools"):
        if v in now and before and now[v] != before.get(v):
            parts += [v, now[v]]
    return "-".join(parts) + ".md"


def write_report(results_dir: Path, cases_path: Path, report_path: Path, log: Callable[[str], None] = print) -> Path:
    cases = load_cases(cases_path)
    results, files = {}, {}
    for v in VARIANTS:
        f = latest_results_file(v, results_dir)
        if f:
            results[v], files[v] = read_results(f), f.name
    text = build_report(results, cases, files)
    report_path.write_text(text, encoding="utf-8")
    # a dated copy once every variant has a complete run that no earlier copy reported
    ids = {c["id"] for c in cases}
    complete = all(v in results and ids <= {r["id"] for r in results[v]} for v in VARIANTS)
    stamp_line = next((ln for ln in text.splitlines() if ln.startswith("Results: ")), "")
    copies = sorted(report_path.parent.glob("report-20*.md"), key=lambda p: (p.stat().st_mtime, p.name))
    last = copies[-1].read_text(encoding="utf-8") if copies else ""
    if complete and stamp_line and stamp_line not in last:
        prev_line = next((ln for ln in last.splitlines() if ln.startswith("Results: ")), "")
        dated = report_path.parent / dated_report_name(stamp_line, prev_line, datetime.now())
        dated.write_text(text, encoding="utf-8")
        log(f"Dated copy written to {dated}")
    return report_path


# ------------------------------------------------------------------ CLI
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variant", choices=[*VARIANTS, "all"], default="agent")
    p.add_argument("--limit", type=int)
    p.add_argument("--ids", help="comma-separated case ids, e.g. W01,C02")
    p.add_argument("--run-name", help="results file prefix; rerun with the same name to resume (default: a timestamp)")
    p.add_argument("--resume", action="store_true", help="append to the latest results file per variant, skipping done ids")
    p.add_argument("--report-only", action="store_true", help="rebuild the report from existing results files")
    p.add_argument("--no-gpu-guard", action="store_true", help="skip the GPU check (only when you know the GPU is free)")
    p.add_argument("--cases", type=Path, default=CASES_PATH)
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    p.add_argument("--report", type=Path, default=REPORT_PATH)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    variants = list(VARIANTS) if args.variant == "all" else [args.variant]
    code = 0
    if not args.report_only:
        from evals.world import ensure_world, eval_settings
        from tower import llm
        from tower.db import open_pool

        cases = load_cases(args.cases, args.ids.split(",") if args.ids else None, args.limit)
        if not cases:
            print("No cases selected.", file=sys.stderr)
            return 2
        from tower.config import load_settings

        url = ensure_world(embed=llm.llm_up(load_settings()))
        settings = eval_settings(url)
        env = {"url": url, "settings": settings, "pool": open_pool(url, size=4)}
        run_name = args.run_name or datetime.now().strftime("%Y%m%d-%H%M%S")
        used_model = False
        try:
            for v in variants:
                existing = latest_results_file(v, args.results_dir) if args.resume and not args.run_name else None
                out = existing or args.results_dir / f"{run_name}-{v}.jsonl"
                print(f"== variant {v} -> {out}")
                guard = None
                if v in MODEL_VARIANTS:
                    used_model = True
                    guard = None if args.no_gpu_guard else (lambda: wait_for_gpu(settings))
                if not run_variant(env, v, cases, out, out.name.removesuffix(f"-{v}.jsonl"), guard=guard):
                    code = 3
                    break
        finally:
            env["pool"].close()
            if used_model:
                unload_model(settings)
    path = write_report(args.results_dir, args.cases, args.report)
    print(f"Report written to {path}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
