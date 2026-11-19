# Engagement write-up: Tarangini control tower

Tarangini Distribution is fictional and all data is synthetic. The work, numbers and transcripts are real: every figure below comes from the project's database, `evals/report.md`, `evals/results/` and the build logs. The network (4 distribution centres, 36 cities, 40 suppliers, 200 SKUs, 3 carriers) is real project data; the customers, contracts, shipments and incidents are simulated.

## 1. The ask
Tarangini is a distributor with about 344 trucks and Rs 13.87 Cr of goods moving. It wants a control tower: take in messy feeds without losing or mangling data, rank disruptions by money at risk, have an agent triage each one from the customers' real contract clauses, and let a person approve any action that moves a lot of goods. Different staff may see different data. It must run locally with no recurring cost.

## 2. What shipped, by milestone
| Milestone | Date | What |
|---|---|---|
| 1. Ingestion and chaos | 2026-11-13 | Simulator for four dirty channels (ERP CSV files, signed GPS webhook, carrier status API, alerts feed). Pipeline with validation, deduplication and a dead-letter queue (reason code, plain reason, replayable suggested fix). Carrier circuit breaker. Append-only audit log enforced by database triggers. A 79 s demo ran 05:00 to 10:40 IST: 875 ERP rows, 86 dead letters, 3,096 GPS pings accepted, 4 of 4 forged signatures refused, reprocessing left the database unchanged |
| 2. Contracts, impact, agent, roles, approvals | 2026-11-13 | 12 contract PDFs parsed into 240 clause chunks with vectors and full text; 60 bills of lading (5 bad ones dead-lettered). Impact engine and exposure from the contract's own numbers (MSA-7.2, 9.1, 14.3). Triage agent on `fusion27b:iq3` with six tools and honesty checks, one retry, then a marked rule fallback. Three roles with row filtering and masking in one data layer. Approve-before-execute above Rs 5,00,000, one transaction, outbox and audit row |
| 3. UI | 2026-11-14 to 11-19 | Next.js UI on the real API with a server-side token proxy. First build replaced after review (see below) |
| 4. Evaluation | 2026-11-19 to 11-20 | 43 triage cases, three variants, deterministic scoring; seven agent runs, with a fix traced to its cause between runs (section 4) |
| 4b. Chaos drill | 2026-11-20 | A scripted bad morning in an isolated stack, measured; three faults found and fixed (section 5) |

The six incidents on the demo morning (scenario clock 10:40 IST):

| Incident | Exposure |
|---|---|
| Ganga Freight strike in UP | Rs 1,78,163 |
| NH44 closed near Adilabad | Rs 1,73,622 (all waivable if customers are told by 11:10) |
| Cyclone watch, Odisha coast | Rs 1,27,837 |
| Kolkata DC conveyor down | Rs 55,181 |
| Supplier 5 days late | Rs 27,865 |
| Fog on the Yamuna Expressway | Rs 4,269 |

**The UI remake.** After the first UI, the viewer was defined as a portfolio visitor with about 60 seconds and no logistics knowledge. The first build opened on a dense console with no story, a weak map and the payoff below the fold. Three structurally different layouts were mocked on real API data, then three looks for the chosen layout. Daman picked layout A ("one case at a time": strip of six incidents, a sheet with four stages, a decision rail) and look 3 ("sheet on indigo"). Two rounds of his feedback followed: too much indigo, small text at about 2000 px and a static page. The fixes were fluid sizing in rem (the same composition at 1440, 1920 and 2560), interactive evidence (replay of the recorded agent run, customers lighting trucks on the map, clauses lighting the figures they drive, a live preview of a fix before Approve) and a 5 s undo hold before Approve sends. Later audits against UI principles and workflow points added clause PDFs behind a role check, keyboard shortcuts and disabled controls with reasons.

## 3. What testing found, and the fix for each root cause
Each finding was traced to its cause and fixed there, with a regression test. No eval case or gold answer was changed to make a run pass.

| Root cause | Example | Fix |
|---|---|---|
| A re-route was priced as escaping a weather event, but impact matches weather by lane distance, so every lane into a storm-hit destination stays inside it | The UI approved a cyclone re-route and exposure rose from Rs 1.28 L to Rs 2.14 L. The four action tests had passed only because their storm was 3,000 km wide | `_reroute_via_dc` drops shipments whose new lane still passes within the event radius and refuses the plan when none escape. Tests rewritten on an 80 km road closure; two regression tests. On the real data, all cyclone re-routes are now refused |
| Movers that lose money reached the option list, the fallback and the validator | NH44: a re-route costing Rs 1.72 L to save Rs 86,811. Supplier: a stock transfer costing Rs 1.49 L to save Rs 27,865 | `pays_for_itself` in `tower/playbooks.py`: options drop losing movers and `check_proposal` rejects them. Checked in SQL that every live mover pays for itself |
| The map refit only on window resize | A direct link or pane resize left the map tiny or off-frame | The map watches its own box (ResizeObserver, 120 ms debounce) and reframes once. Checked at 1440, 1100 and a resize without reload |
| The rule plan ordered by action type, not by value | W03, R04 and X07 in the eval: the rule plan put a mover first when notifying saved more | Rule plan orders by net saving (`tower/playbooks.py`). Found by the rules baseline at 37/43; now 43/43 |
| The rule brief cited the weather waiver for every notification | S02, S03 and X03: a supplier incident cited MSA-14.3 instead of MSA-9.1 | `_rule_clause` picks the clause by incident kind (`tower/agent.py`) |
| Audit and test residue; the scenario clock kept running after staging | Audit times out of order, late timestamps, test rows in Data health and Audit | Audit scoped to the current morning (`/audit?morning=true`); feed times capped to the clock; `sim/demo.py` now writes a "scenario start" marker and stops the clock. Fully clean after a restage and re-triage |
| The model cited clauses it never fetched | 1 of 3 runs in the first demo, 1 of 8 on 2026-11-14, 2 of 6 on 2026-11-19 (strike and supplier) | The honesty check rejected the run and fell back to the rule plan every time. Fixed at the source over runs 2 to 5 of the evaluation (section 4): unfetched-clause catches went from 13 in run 1 to 0 in runs 5 to 7 |
| Layout that held at 1440 broke below it | Between 761 and 1339 px the strip squeezed six tabs to 99 px and the sheet's composition broke | Root size stops scaling at its 15 px floor while the column keeps narrowing. Three-across chips below 1340 px; head row, map and citation stack. Measured at 1024, 1280, 1366, 1440, 1920 and 375 |
| Embeddings on the GPU | The 27B model fell from 44 to 11 tok/s | `nomic-embed-text` runs on CPU (`num_gpu 0`) |

## 4. Evaluation
43 cases across weather (10), carrier (6), facility (4), supplier (4), roles (7), adversarial (7) and honesty (5). Each builds a small world in a throwaway database. **Gold comes from the deterministic engine, not a model:** the acceptable first playbook, the options the engine offers, forbidden actions and targets, the clauses that drive the money, the engine's rupee figures, and the ids, DCs and driver data a role must not see. Scoring is code only, one result per criterion; a case passes when every criterion that applies passed. There is no LLM judge.

Because gold is the engine's own output, the rule plan (the fallback with no model) is the floor by construction and scores 43/43. That is not a result about the agent. The evaluation asks two things:
1. Does the agent reach that floor while reading the incident, fetching and quoting the exact clause, and writing a usable brief?
2. What happens without tools (the same model, given only the incident text and the affected shipments)?

**Headline: agent run 7, the first run on the final code, passed 43/43.** Every earlier run is shown below. The owner stopped the eval runs at run 7. A run 8 was stopped part-way and archived (`evals/results/archive/run8-agent-stopped.jsonl`); it is not a result.

| Variant | Passed | Median per case | Notes |
|---|---|---|---|
| Rules (no model) | 43/43 (100%) | 0.0 s (p90 0.1 s) | By construction. The first run was 37/43, which found the two rule-plan bugs in section 3 (40/43 after the first fix, 43/43 after the second) |
| Agent (`fusion27b:iq3`, tools, honesty checks), run 7 | 43/43 (100%) | 46.2 s (p90 62.9 s) | weather 10/10, carrier 6/6, facility 4/4, supplier 4/4, roles 7/7, adversarial 7/7, honesty 5/5. Every criterion 100%. 2 fallbacks, both cases with nothing to plan, where a fallback is the right answer |
| No tools (same model), run 2 | 2/43 (5%) | 11.3 s | Run 1 also 2/43. Cited a clause it never fetched in 40 of 43 cases (ids such as "C-104" or "MSA-KIRANA-4.2"); no quote an exact excerpt of a stored clause (0 of 40 that quote); an action or target the engine does not offer in 28 cases; wrong first playbook in 20; missed the clause that drives the money in 37 of 37 |

### The agent, run by run
Same 43 cases, same gold, same scoring every time. Pass counts, fallbacks and median latency are from `evals/results/run<N>-agent.jsonl`, rebuilt with `python -m evals.run --report-only`; the per-run reports are the `evals/report-2026-11-19-agent-run<N>.md` files and `evals/report-2026-11-20-agent-run7.md`.

| Run | Passed | Fell back | Clause cited before fetching (caught) | Median / p90 | Change made before the run |
|---|---|---|---|---|---|
| 1 | 40/43 | 5 | 13 | 38.3 s / 48.2 s | None: the first run (failures F04, X01, X06) |
| 2 | 33/43 | 12 | 9 | 35.8 s / 47.3 s | `clauses_that_price_this`: the exposure tool lists only the clauses that apply and the prompt says to fetch those. The minimum plan size became min(2, kinds offered) |
| 3 | 40/43 | 5 | 7 | 33.6 s / 43.6 s | `get_clause` takes an exact `clause_id`; model-call budget 9 to 11 |
| 4 | 41/43 | 4 | 8 | 32.7 s / 40.9 s | Rejection messages name the fix (the open action kinds; the clauses that do apply) |
| 5 | 43/43 | 2 | 0 | 30.6 s / 36.9 s | No clause id shown where it does not apply (notify note, tool description) |
| 6 | 41/43 | 4 | 0 | 33.8 s / 41.2 s | None: a rerun of run 5's code (failures C02, R05) |
| 7 | 43/43 | 2 | 0 | 46.2 s / 62.9 s | The option list shows one option per kind of action, the best priced; the engine picks the carrier or DC |

- **Run 2 was a regression, found and fixed at the cause.** Telling the model to fetch the listed clauses made it call `get_clause` with a search for "MSA-14.3". The hybrid search often ranked other clauses first, so citations failed the check and 7 runs ended with no proposal and 5 more failed the checks twice. The exact lookup in run 3 removed that.
- **No run had an honesty failure in the final answer.** In all seven runs the agent stated no figure the engine did not produce, showed no out-of-scope detail, cited no clause it never fetched and quoted no clause inexactly (the failure counts are 0 in each report). The catches are the agent's own code rejecting a draft before it reached the user: unfetched-clause catches fell from 13 in run 1 to 7 in run 3 and to 0 in runs 5, 6 and 7. Every failed case in every run was a fallback to the rule plan (the brief says so), not a wrong answer.
- **Runs 5 and 6 ran the same code and differ by two cases** (43 and 41; C02 and R05 passed once and fell back once). One run moves by a case or two without any change, so run 7's 43/43 is one run on the final code, not proof that the agent is perfect. Over the last three runs the agent passed 127 of 129 case runs (98%).
- **Latency.** Run 7's median is 46.2 s because it shared the GPU with the owner for part of the run (the runner's GPU guard logged one pause). The clean-GPU figures are runs 5 and 6: 30.6 s and 33.8 s. Run 1 was 38.3 s. Prompt tokens per case were about 14.6k in run 7 over 6.6 model calls, and a full 43-case run takes 22 to 35 minutes. Cost per run is Rs 0.00: one local RTX 5080, no paid API.

Scoring and gold were fixed before any model run, and no case or gold answer was changed after seeing a result. Results files and run names are in `evals/results/` (not committed; the reports are).

## 5. Chaos drill
A scripted bad morning in an isolated stack (`python -m sim.drill`, no model): every feed is broken in a fixed order, a carrier is down twice, the tower is killed mid-burst, and the result is checked against what was injected. Full write-up and tables: [docs/chaos-drill.md](chaos-drill.md).

- **0 lost, 0 duplicates.** 158 bad ERP rows injected, 155 dead letters written, 3 repeats of an already filed payload, 0 unaccounted for. Shipment, line and GPS rows are all distinct after the burst, the retries and the re-drops; reprocessing the same input left the database unchanged in all four checks.
- **Circuit breakers open and close.** A circuit opened 4.3 to 5.2 s after an outage started; the first carrier took 16.8 s to close because its cooldown had backed off to 16 s.
- **The drill found three faults, all fixed at the cause with regression tests.** Before and after, same seed:

| Measure | Before the fixes | After |
|---|---|---|
| Carrier C3 on `/health/feeds` after a tower restart | degraded, open (to the end of the run) | closed from the first sample |
| Existing shipments whose promised date a replay moved | 8 (all earlier) | 0 |
| Row-level re-drop of the ERP files | 1 shipment changed | unchanged |
| Replays reported as recovered | 70 of 176 (36 of them changed nothing, not shown) | 28 applied of 176 |
| Replay outcomes | not split | applied 28, unchanged 4, stale 38, failed again 1 (71 replays) |

- The drill does not cover model calls, the UI, a Postgres kill or the time-dependent risk maths (`chaos-drill.md` section 13).

## 6. Limits and what is next
- The rule plan scores 100% because the gold is the engine's. The eval cannot say whether the engine's delay and cost model is right: those are stated assumptions (straight-line lanes, fixed rupee rates), not learned from history.
- Incident exposures overlap, so they are not additive. Every weather alert is treated as a declared event for MSA-14.3. A re-route's own extra delay can create a penalty no incident tracks afterwards.
- A triage takes 30 to 60 s on one local GPU (46 s median in run 7 with the GPU shared) and the model is not deterministic: the checks and the fallback bound what can come out, not how good it is. Runs 5 and 6 ran the same code and scored 43 and 41. Each variant was run once on the final code.
- Static bearer tokens in `.env`, no expiry, rotation or TLS. Local use only. The chat webhook is a mock.
- No real customers, feeds or production deployment. Never claimed.

Next:
- More agent runs on the final code to put a spread around 43/43 (stopped at run 7 by the owner's decision).
- Gold answers reviewed by someone other than the engine's author, so the floor itself is checked.
- A chaos drill that includes the model path and a Postgres kill.
