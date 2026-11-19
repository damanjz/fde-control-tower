# Impact, money, agent, roles and approvals (milestone 2)

Everything numeric is computed by code. The local model reads, chooses and explains; it never does arithmetic, and
code checks what it says before anything is stored.

## 1. Unstructured inputs

**Contracts.** One master services agreement PDF per retail chain (12 chains: the 11 named chains plus the Kirana network;
customers in different cities share their chain's MSA). `python -m sim contracts` draws them with `sim/make_pdfs.py`
(run through the reportlab interpreter; the tower never imports reportlab) in two layouts: `MSA-7.2  Late delivery` headings
(layout A) and `ARTICLE 7 / Clause 7.2 - LATE DELIVERY` (layout B), 20 numbered clauses each. `python -m tower.contracts`:

- pypdf text, header/footer lines stripped, split into one chunk per clause (clause id normalised to `MSA-n.n`, start page);
- stored in `contract_chunk` with a 768-dim `nomic-embed-text` embedding (pgvector, HNSW) and a full-text `tsvector`;
- the three money clauses (`MSA-7.2`, `MSA-9.1`, `MSA-14.3`) are also parsed into numbers (`params`);
- those numbers are compared with the `contract_clause` rows seeded in milestone 1. A difference marks the chunk
  `verified = false`, is logged, and goes to the dead-letter queue (feed `contracts`, reason `param_mismatch`). The PDF
  value is the one used for money, and the exposure rows that rely on it carry `params_unverified`.
  The simulator makes Spar's MSA-9.1 say 2.5% (the master says 2%) so this path always has something to catch.
- re-ingesting a file with the same hash and complete embeddings is a no-op apart from the verification.

**Bills of lading.** About 60 PDFs dropped by the simulator in `data/inbox/bol/` (BOL no, shipment id, carrier, vehicle, driver
name and phone, consignee, cases, declared value, date). A worker in the tower reads them with pypdf (label and value may be on
separate lines), validates against the ERP shipment and stores `bol_no`, `vehicle_no`, `driver_name`, `driver_phone` on it.
Five of the 60 are bad on purpose:

| Defect | Reason code | Suggested fix |
|---|---|---|
| garbled text; a scan with no text layer | `unreadable_pdf` | Send it to a person to type in |
| declared value more than 2% off the ERP (two files: +9%, -6%) | `value_mismatch` | confirm the value, amend ERP or get a corrected BOL |
| shipment id not in the ERP | `unknown_shipment` | the shipment with the same consignee and cases, as a replayable patch |
| cases differ from the ERP | `cases_mismatch` | count at the dock |

A 1.2% drift is inside the tolerance and is accepted. Good files move to `data/processed/bol/`, dead-lettered ones to
`data/rejected/bol/`; the file hash makes a re-dropped file a no-op. Replay of a corrected payload goes through the same checks.

## 2. Impact engine (`tower/risk.py`, no LLM)

For each `disruption_event` it writes one `incident_impact` row per affected shipment (reason, `est_delay_days`) and an
`incident_exposure` roll-up. The impact worker recomputes active incidents every 10 s; `POST /incidents/recompute` and every
executed action do it too. Re-running with the same data gives the same rows (an advisory lock serialises recomputes of one
incident). A shipment is only considered if it is not delivered and its ETA is after the event start.

| Kind | Who is affected | est_delay_days |
|---|---|---|
| weather, road closure (text says closed) | lane segment DC to city (straight line) passes within the radius | hours stated in the text / 24 + 1.0 detour day (default 24 h). NH44, 26 h: 2.08 |
| weather, fog | same geometry | 0.75 |
| weather, cyclone | same geometry | 3.0 |
| weather, other | same geometry | by severity: low 0.5, medium 1.0, high 1.5, critical 2.5 |
| facility | shipments leaving that DC between the event start and the outage end (a clock time in the text such as "ETA 18:00", else +12 h) | by severity: low 0.25, medium 0.5, high 1.5, critical 3.0 |
| carrier strike | that carrier's shipments into the named states | by severity: low 0.5, medium 1.0, high 2.0, critical 3.0 |
| supplier delay | lines of that supplier's SKUs on shipments from a DC whose stock of that SKU is under 40 cases | 0.5 (re-pick), and a fill rate of 1 minus 8% per day the supplier is late (parsed from "N days late", capped at 60% short) |

## 3. Money at risk

Per affected shipment and incident, with the customer's own MSA numbers (from the PDF):

- `arrival_estimate = eta + est_delay_days` (days already written into the ETA by an `update_eta` action are not counted twice);
- `days_late = max(0, ceil(arrival_estimate - promised_at - 0.5 day))`, the half day being the 12 hour grace in MSA-7.2;
- **MSA-7.2** late penalty = invoice value x min(1.5% x days_late, 10%);
- **MSA-9.1** (supplier incidents) if the estimated fill rate is under 95%: 2% x the value of the affected SKU lines (the week's
  SKU invoice value is approximated by those lines);
- **MSA-14.3** (weather incidents): both penalties are waived for a customer who has a recorded notification within 6 hours of the
  event start. The incident shows `notify_deadline` (the earliest such deadline), `waivable_inr` (penalty that notifying in time
  would remove, only while the deadline has not passed) and `waived_inr` (already removed).

Per incident: shipments, value, gross, waived, net (the exposure). Incidents are ranked by net exposure. The same shipment can
be hit by two incidents and then counts in both: exposures are per incident and are not meant to be added up (the demo's total
says "shipment-incident pairs").

## 4. Roles and access

Bearer tokens (`CT_TOKEN_ASHA`, `CT_TOKEN_KIRAN`, `CT_TOKEN_MEERA` in `.env`). `GET /health` and the signed GPS webhook are open;
dead-letter replay/discard and `/audit-log` keep their admin key.

| | asha, VP Operations | kiran, Dispatcher South | meera, Finance |
|---|---|---|---|
| shipments and incidents | all India | only shipments leaving DC-HYD; an incident with none of his shipments is hidden (404) | all India |
| driver name and phone | full | name; phone masked (`+91 ***** **210`) | removed |
| carrier dispatch phone | full | masked | removed |
| carrier bank account | masked (`HDFC A/c **** **** 4471`) | masked | full |
| contract clauses | all chains | chains he ships to | all chains |
| dead-letter queue | yes | no (403) | no (403) |
| audit trail | all | actions that touched his DC, and his own | all |
| execute playbooks | yes | yes, if every DC the playbook leaves from is in his scope | no (403) |
| approve | yes | no (403) | no (403) |

Enforcement: `tower/scoped.py` (`ScopedStore`) is the only place a read for a user happens. It applies the row filter in SQL and
the field masking before a row leaves it. Every endpoint in `tower/routes.py` and every agent tool calls it, and
`tests/test_rbac_db.py` fails if `routes.py`, `agent.py` or `playbooks.py` read a scoped table directly. The agent therefore only
ever sees what its user may see; the same incident can produce a different brief and different playbooks per role.

## 5. Triage agent (`tower/agent.py`)

`POST /incidents/{id}/triage` runs `fusion27b:iq3` through Ollama `/api/chat` (tools, `think: false`, no streaming, one system
message, 8k context, one model call at a time) with these tools, all scoped to the caller:

| Tool | What it returns |
|---|---|
| `get_incident` | kind, severity, text, start, scope, how many affected shipments the caller can see |
| `find_affected` | counts, value, by DC, carrier, city, and the top shipments by penalty (driver fields as masked for the role) |
| `compute_exposure` | the deterministic exposure: late and fill penalties, waived, waivable, deadline, top customers and chains |
| `get_clause` | hybrid retrieval (full text and vector, reciprocal rank fusion) over the contract chunks: chain, clause id, page, text |
| `list_options` | every buildable action priced by code: alternative DCs and carriers with spare trucks, stock at other DCs |
| `propose_playbooks` | the finish: a summary and 2 to 4 playbooks (action type, params, clause id, chain, exact quote, rationale) |

Honesty checks run in code on the final call. It is rejected, with the reasons handed back to the model, when: a quoted clause or
chain is not a stored chunk the user may read, or the quote is not an exact excerpt of it; a clause id in the text was never
returned by `get_clause`; a rupee figure in the summary or a rationale does not match a number in a tool output (lakh and crore
forms are checked at their printed precision); a shipment id is not an affected shipment the user may see; the playbook count is
not 2 to 4, an action type repeats, or an action does not fit the incident kind (for example a stock transfer for a storm).
The model gets one retry. A second failure, a model that never proposes, or an unreachable Ollama gives a deterministic
**agent fallback** brief (summary and 2 to 4 playbooks chosen by rule from the same priced options), marked `outcome = fallback`
with the reason, and the playbooks marked `source = fallback`.

Playbooks have: action type (`notify_customers`, `reroute_via_dc`, `swap_carrier`, `stock_transfer`, `update_eta`), parameters, goods
value moved, estimated cost, estimated saving and the clause relied on. Goods value, cost and saving come from the exposure engine
and this cost model, never from the model:

| Action | Goods moved | Cost | Saving | Notes |
|---|---|---|---|---|
| notify_customers | 0 | Rs 40 per customer | the waiver (weather only, before the deadline) | records `customer_notification` at the scenario clock time |
| update_eta | 0 | 0 | 0 | the promise does not change, so penalties do not |
| reroute_via_dc | value of shipments moved | trucks x (extra km x Rs 38 + Rs 2,500); trucks = ceil(cases / 600); extra km = 1.25 x (new DC to city minus old DC to city, not below 0) | penalty before minus penalty after, where the new delay is extra km / 450 + 0.5 day | limited by spare trucks at the new DC |
| swap_carrier | value of shipments moved | trucks x (road km x Rs 38 x 25% + Rs 1,500) | same, new delay 0.5 day | limited by the new carrier's spare trucks at the origin DC |
| stock_transfer | value of the cases moved | cases x km x 1.25 x Rs 0.35 | fill-rate penalty avoided by the cases that cover the shortfall | at most 50% of the source DC's stock of a SKU |

Two rules keep the options honest (added 2026-11-14, after the M3 UI showed an approved re-route raising exposure):
- A weather event hits shipments by lane, so a re-route only helps a shipment whose new lane clears the event radius (the same
  test `risk.select_impact` uses). Shipments whose new lane still passes through the event are left out; if none clear it, the
  re-route is refused. A storm at the destination therefore never gets a re-route.
- A mover (re-route, carrier swap, stock transfer) is offered only when its saving is larger than its cost (`pays_for_itself`).
  `list_options` and the fallback never show a losing move, and `propose_playbooks` rejects one.

Each run is stored in `agent_run` (incident, user, role, model, start, finish, ms, prompt and completion tokens, model calls,
outcome, fallback reason, honesty failures, summary) and `agent_step` (tool, args, result summary, ms). The latest run per
incident and role is the cache: `GET .../brief` shows it, `POST .../triage {"use_cache": true}` returns it without a model call.
A new triage marks the role's earlier un-run playbooks `rejected`.

## 6. Actions and approvals (`tower/actions.py`)

- `POST /playbooks/{id}/run`: goods value up to Rs 5,00,000 executes at once for a user who may act in the playbook's scope.
  Above it the playbook becomes `pending_approval` (audit `playbook.request_approval`).
- `POST /playbooks/{id}/approve`: VP Operations only. Executes the parked playbook in the requester's name, approver recorded.
- One database transaction per execution: shipment changes (DC or carrier, ETA, `version + 1`) after a check that each shipment is
  still as planned (otherwise 409 and nothing changes), spare trucks or stock decremented, notifications recorded with the time,
  an `outbox` row for the chat webhook (plus one per notified customer), the incident's exposure recomputed, the `audit_log`
  row (actor, approver, before and after per shipment, DCs touched) and the playbook marked `executed`.
- Idempotency key `playbook:<id>:exec`; a second run or approve returns the first result with `replayed: true`.
- The outbox worker posts to the mock chat webhook on the carrier-API mock (`POST /hooks/chat`) with retries, up to 5 attempts.

## 7. Endpoints

`GET /me`, `GET /incidents`, `GET /incidents/{id}/brief` (incident, money, ontology chain
source > warehouses > carriers > cities > customers with counts, latest agent run with steps and timing, quoted clauses, playbooks
with state, paginated affected shipments), `POST /incidents/{id}/triage`, `POST /incidents/recompute` (asha),
`GET /playbooks/{id}`, `POST /playbooks/{id}/run|approve`, `GET /audit`, plus the milestone 1 reads
(`/shipments`, `/shipments/{id}`, `/disruptions`, `/network`, `/dead-letters`, `/health/feeds`) now scoped and authenticated.
Lists take `limit` and `offset`.

## 8. Known limits

- Delay and cost numbers are stated assumptions (tables above), not learned from history. The geometry is straight lines.
- Incident exposures overlap; a re-route's own extra delay can create a late penalty that no incident tracks after execution
  (the saving is net of it when planned).
- Every weather alert is treated as a "declared weather event" for MSA-14.3.
- The week's SKU invoice value for MSA-9.1 is approximated by the affected lines of the shipment.
- A triage takes about 100 s on the local model (7 calls, about 20k prompt tokens) and the model is not deterministic, so wording and
  the chosen playbooks vary between runs; the checks and the fallback bound what can come out, not how good it is.
- Reroute and swap check capacity at the target but only require the actor to cover the origin DCs.
- Static bearer tokens in `.env`: no expiry, rotation or TLS. Local use only.
- Dead letters of the `contracts` feed cannot be replayed (fix the PDF or the master and ingest again).
- The chat webhook is a mock that records messages.
