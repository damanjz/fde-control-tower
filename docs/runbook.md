# Runbook: Tarangini control tower

For the person running the project after hand-off. Everything runs locally. Tarangini Distribution is fictional and all data is synthetic.

## Before you start
- Docker Desktop running, with `docker` on PATH (or set `CT_DOCKER` to the CLI).
- The project's virtual environment (`pip install -e .`). `python` below means its interpreter. For the simulator's PDFs, reportlab in the same environment, or `CT_PDF_PYTHON` pointing at an interpreter that has it.
- A `.env` in the project root (copy `.env.example`, change every value). It holds the database password, the GPS secret, the admin keys and the three user tokens.
- `npm install` in `web/` once.
- Ollama with `fusion27b:iq3` and `nomic-embed-text`, only when you run the agent or restage with embeddings.

## Start
`python scripts/dev.py`

| Step | What it does |
|---|---|
| 1 | Checks Postgres on 127.0.0.1:5436; starts it with docker compose if it does not answer |
| 2 | Applies `db/schema.sql` and `db/migrations/*.sql` (idempotent) |
| 3 | Stages the morning if the database has none (05:00 to 10:40 IST scenario, contracts, bills of lading, impact and exposure; no model calls); otherwise keeps the state and sets the scenario clock to 10:40 IST |
| 4 | Starts the mock carrier API (:9100), the tower (:8500) and the UI (:4400) |

- UI: http://127.0.0.1:4400/. Deep link to a case: `?incident=4`. Logs: `data/logs/` (`tower.log`, `carrier_api.log`, `web.log`).
- Ctrl+C stops what the script started.
- Starting never runs the triage agent. The briefs show the agent runs already in the database; "Run the agent" in a case is the only thing that calls the model.
- Flags: `--stage` restage, `--fresh` rerun the milestone 1 scenario too, `--prod` use `next build` and `next start`, `--no-web` backend only, `--no-llm` no Ollama call while starting (contracts staged without embeddings; retrieval falls back to full text).

## Restage
`python scripts/dev.py --stage` (add `--prod` for the built UI).

- Resets the operational tables to the staged morning. Contracts, capacity and the audit log stay.
- Total exposure after a restage is Rs 5,66,938 across six incidents.
- Agent runs are cleared with the operational tables. To fill the briefs again, run the agent on each incident (about 30 to 55 s each on the local model) as asha.
- Restage when audit or data-health rows from tests or earlier demos should be gone. The UI's audit view already shows only the current scenario morning (`/audit?morning=true`).

## Running the agent (GPU rules)
The RTX 5080 has 16 GB and is shared with whatever else runs on the machine.

- One model call at a time (a lock in `tower/llm.py`). A triage takes about 30 to 60 s, 7 model calls and about 20k prompt tokens.
- Check the GPU first: `nvidia-smi`. Do not run the agent while something else uses it.
- Embeddings (`nomic-embed-text`) run on CPU (`num_gpu 0`). On the GPU the embedding model pushed the 27B model out of VRAM and speed fell from 44 to 11 tok/s.
- Keep context at 8k. Unload the models when you are done (`ollama stop fusion27b:iq3`).
- Without Ollama the run does not fail: an unreachable model gives a marked deterministic fallback brief.

## Roles and tokens
Three demo roles. The tokens live in `.env` as `CT_TOKEN_ASHA`, `CT_TOKEN_KIRAN` and `CT_TOKEN_MEERA`. Do not print them, paste them into logs or commit them. The UI server attaches the selected role's token itself; the browser never sees one.

| Role | Sees | Can do |
|---|---|---|
| asha, VP Operations | All India, full driver data, dead letters, audit | Run playbooks, approve, switch the outage |
| kiran, Dispatcher South | Shipments leaving DC-HYD; phones masked; other incidents return 404 | Run playbooks inside his DCs; cannot approve |
| meera, Finance | All India; driver fields removed; bank account in full | Read only; 403 on running a playbook |

API calls use `Authorization: Bearer <token>` with the token read from `.env` into a shell variable. Admin endpoints (dead-letter replay and discard, `/audit-log`) take `X-Admin-Key` (`CT_ADMIN_KEY`). To rotate a value, edit `.env` and restart.

## Evaluation
The scoring is described in the milestone 4 section of the README; results are in `evals/report.md`.

```
python -m evals.build_cases                 # rebuild worlds and gold; only after a deliberate engine change
python -m evals.run --variant rules         # baseline 1: no model, under a minute
python -m evals.run --variant agent --run-name run8   # the agent, about 45 s a case; name every run
python -m evals.run --variant notools       # baseline 2, about 12 s a case
python -m evals.run --report-only           # rebuild evals/report.md from the latest results per variant
```

- Each case builds a small world in a throwaway `ct_eval` database. The live `ct` database is never touched.
- Results append to `evals/results/<run-name>-<variant>.jsonl` (`evals/results/` is not committed). Without `--run-name` the name is a timestamp, so always pass one (`run8`, `run2` for a rules or notools rerun). Rerun with the same `--run-name`, or pass `--resume`, to skip ids already done.
- `evals/report.md` is rewritten from the latest results file per variant. Once all three variants have a full run that no earlier copy reported, a dated copy is written, named from the runs it holds: `evals/report-<date>-agent-run7.md`, and `-rules-run2` or `-notools-run2` added when that baseline changed since the previous copy.
- Seven agent runs exist (run 1 to run 7); the headline is run 7, the first on the final code. After any prompt, tool, data or model change, rerun the agent and make the README numbers match `evals/report.md`.
- Useful flags: `--ids W01,C02`, `--limit N`.
- Run one model variant at a time. The runs are GPU-bound and measure latency.

**GPU guard.** Before every model case the runner reads `nvidia-smi`. At 40% utilization or more, or 9 GB or more used by anything other than the resident model, it waits and rechecks every 60 s. After 20 minutes it stops cleanly and the run resumes later. `--no-gpu-guard` skips the check; use it only when the GPU is known to be free. The model is unloaded at the end.

## Outage drill
Shows how the tower behaves when feeds break.

1. Open Data health as asha and switch Outage on (or `POST /outage {"on": true}` with asha's token; other roles get 403).
2. The simulator now fails 30% of carrier API calls and drops one malformed ERP export (40 broken rows) into the inbox. Audit row: `sim.outage`.
3. Watch: carrier circuits open and trucks keep their last known status; the dead-letter count rises (about 7 normally, 48 during the outage); a banner on the map says what is failing.
4. Open a held-back record from the Data health line, then replay it with its suggested fix or discard it. Both are audited. A replay says what it did: `applied` (the data changed; the page says "Replayed"), `unchanged` ("Replayed, nothing to change"), `stale` ("Not applied: newer data held"; older than what the tower holds, so refused) or `failed`. Only `applied` counts as a recovery.
5. Switch Outage off. Circuits recover on their own after the cooldown (4 s, doubling to 16 s on a failed probe).

From the command line: `python -m sim faults C2 --outage`, and `--clear` to remove it.

### Chaos drill (scripted, measured)
`python -m sim.drill --report drill-report.md` runs a whole bad morning in an isolated stack (database `ct_drill`, tower on 8601, mock on 9201, no model) and prints before-and-after tables: injected against dead-lettered, idempotency checks, breaker timings, `/health/feeds` per phase, replay outcomes, and four reprocessing checks. About 2 minutes; needs Postgres up and ports 8601 and 9201 free. It never touches the live `ct` database. Flags: `--seed N`, `--keep`, `--workdir`. Results and what it found: `docs/chaos-drill.md`.

## Troubleshooting
| Symptom | Cause | Fix |
|---|---|---|
| `dev.py` stops at the database step, or connection refused on 5436 | Docker Desktop was not running after a reboot | Start Docker Desktop, wait for the engine, run `python scripts/dev.py` again (it starts Postgres itself) |
| `dev.ps1` is blocked as an unsigned script | Windows script policy | Use `python scripts/dev.py`, or `powershell -ExecutionPolicy Bypass -File scripts\dev.ps1` |
| "web/node_modules is missing" | UI dependencies not installed | `npm install` in `web/` |
| Triage takes minutes, or 11 tok/s | The embedding model or another app is holding VRAM | `ollama ps`; unload `nomic-embed-text`; embeddings must stay on CPU (`tower/llm.py`); close other GPU apps |
| Run ends as `fallback` | The model cited a clause it never fetched, or failed the honesty checks twice, or Ollama was down | Expected behaviour. The brief says why and carries the rule plan. Check `agent_run.fallback_reason` |
| Audit or feed times look wrong, or later than the clock | The scenario clock kept running after staging (fixed in `sim/demo.py`, which now writes a "scenario start" marker) | Restage; the clock is set to 10:40 IST and the audit view shows only the current morning |
| Test rows or old demo actions in Data health and Audit | Residue from earlier runs | Restage with `python scripts/dev.py --stage` |
| Staging without Ollama leaves contracts with no embeddings | `--no-llm` | Retrieval uses full text. Restage without the flag when the model is available |
| Nothing at :4400 and no error | Next did not start | `data/logs/web.log` |
| Eval waits and prints that the GPU is busy | The guard is working | Free the GPU, or let it stop after 20 minutes and resume with `--resume` |

## Checks
`python -m ruff check .`, `python -m pytest -q` (LLM tests skipped unless `CT_LLM_TESTS=1`), and in `web/`: `npm run lint`, `npm run typecheck`, `npm run build`. DB tests use a throwaway `ct_test` database and skip when Postgres is down.
