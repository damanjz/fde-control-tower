# Chaos drill: one bad morning, measured

One scripted run that breaks every feed of the control tower in a fixed order, then checks what the tower did with each bad record. Everything is synthetic. The numbers below come from `python -m sim.drill` (seed 20261113); the same command reprints them. They are from the run after the fixes in section 12; the first run's numbers are kept in section 12.5.

## 1. The ask
Show, with counts, that bad input never disappears: every bad record is either applied or sits in the dead-letter queue with a reason, nothing is duplicated, the carrier circuit breakers open and close, the status the UI reads stays truthful, and the fixable dead letters can be replayed.

## 2. How the drill is isolated
It never touches the live stack or its database.

| Thing | Live stack | Drill |
|---|---|---|
| Database | `ct` | `ct_drill`, created next to it and dropped at the end |
| Tower | 8500 | 8601 |
| Carrier mock | 9100 | 9201 (the chat webhook points there too) |
| Data folder | `data/` | a work dir with a copy of `data/network`; inbox, processed, rejected and the clock file live there |
| Model | Ollama | none: the LLM URL is a dead port, contracts are not ingested, nothing embeds, no triage |

The script asserts all of this before it starts anything. The scenario clock is parked (factor 0) and moved by hand between phases, so only the breaker timings depend on wall time.

## 3. Run it
From the project root, with Postgres up (`docker compose up -d db`) and nothing on 8601 or 9201 (the drill works in a temp folder; set `CT_DRILL_DIR` to move it):

```
python -m sim.drill --report drill-report.md
```

About 2 minutes. `--seed N` changes what is injected, `--keep` leaves `ct_drill` and the work dir for inspection, `--workdir` moves the work dir. It starts the mock and the tower as subprocesses and stops both at the end. `tests/test_drill.py` checks only that the GPS burst is a pure function of its seed.

Deterministic for a seed: what is injected, and every count below except the breaker timings, the failed-poll counts and where the kill lands, which depend on timing. Across three runs C1 took 16.8 to 17.7 s to close and 4.4 to 5.6 s to open.

## 4. The morning
| t (s) | Event |
|---|---|
| 7 | Baseline: ERP exports 05:00, 06:00, 07:00 (with the generator's ambient mess), 40 clean GPS pings, every carrier polled twice |
| 13 | C1 outage, 100% HTTP 503 |
| 18 | C1 circuit open. ERP 08:00 export and malformed export 1 (41 cut-off or banner lines) dropped |
| 20 | Byte-identical copy of malformed export 1 dropped |
| 21 | 20 bills of lading dropped: 2 unreadable, 2 with the wrong value, 1 for an unknown shipment |
| 33 | C1 has opened 3 times (cooldown 4, 8, 16 s). Outage cleared; every carrier 30% flaky |
| 50 | C1 circuit closed. C3 outage on top of the flakiness |
| 54 | C3 circuit open. ERP 09:00, malformed export 2 (31 lines) and 10:00 dropped together; GPS burst of 206 pings starts |
| 55 | Tower killed (hard, process tree) with 2 of 3 files still in the inbox and 2 of 11 GPS batches sent |
| 58 | C3 outage cleared while the tower is down |
| 62 | Tower restarted and answering (2.2 s of the 7.0 s down time was the process starting); C3 reads closed |
| 68 | GPS burst finished; the sender retried 3 requests |
| 81 | All carrier faults cleared |
| 81 to 98 | Measure, replay the fixable dead letters, re-drop the same input |

The GPS burst has 160 valid pings in shuffled order, 30 duplicates (15 adjacent, 15 three batches later), 12 pings 20 to 45 minutes in the future, 4 that fail validation (lat 999), and 5 requests signed with the wrong secret.

## 5. State before and after
| | Baseline | After chaos | After recovery |
|---|---|---|---|
| Shipments | 313 | 343 | 347 |
| Shipment lines | 1,535 | 1,694 | 1,716 |
| GPS rows | 40 | 200 | 212 |
| Dead letters, waiting | 49 | 176 | 106 |
| Dead letters, all | 49 | 176 | 176 |
| Shipments with a bill of lading | 0 | 15 | 16 |
| State md5 (shipments) | d30a31a8d0 | 68dc48b81e | 66da6ceb92 |

The baseline already holds 49 dead letters: the generator injects ambient mess in every export, and that is part of a normal morning here.

## 6. Injected against outcome
ERP, ambient mess (six exports) and the two broken exports:

| Kind | Injected | Dead-lettered |
|---|---|---|
| Blank promised date | 22 | 22 |
| Unknown city code | 24 | 24 |
| Unreadable amount | 12 | 12 |
| Conflicting duplicate row | 12 | 12 |
| Malformed line (cut, junk, quote) | 16 | 14 |
| Broken export 1, lines | 41 | 41 |
| Broken export 2, lines | 31 | 31 |

The 2 missing malformed lines are junk banners: the same text appears in several files, the tower files one dead letter per distinct payload and counts the repeat (3 repeats in all, from the audit log). Exact duplicate rows (11) and padded or mixed-format rows (93) are absorbed by design.

| ERP accounting | Count |
|---|---|
| Bad rows injected | 158 |
| Dead letters written | 155 |
| Repeats of an already filed payload | 3 |
| Lost (injected minus dead letters minus repeats) | 0 |
| Shipments exported at least once | 347 |
| Applied | 343 |
| Not applied, a dead letter names them | 4 |
| Not applied and no dead letter (lost) | 0 |

The duplicate file added no rows and no dead letters: its hash was already in `ingest_log`.

| GPS burst | Injected | Outcome |
|---|---|---|
| Valid unique pings | 160 | 160 stored |
| Duplicates | 30 | 30 suppressed, plus 3 from requests the sender retried |
| Out of order (older ping after a newer one) | 80 | 80 stored; no shipment position moved back |
| Clock-skewed | 12 | 12 dead letters (`clock_skew_future`) |
| Failing validation | 4 | 4 dead letters (`bad_ping`) |
| Forged signature | 5 | 5 rejected with 401 |

| Bills of lading | Injected | Outcome |
|---|---|---|
| Good | 15 | 15 applied |
| Unreadable | 2 | 2 dead letters |
| Declared value 6 to 9% off | 2 | 2 dead letters |
| Unknown shipment | 1 | 1 dead letter |

## 7. Idempotency
| Check | Result |
|---|---|
| Shipment rows, distinct case-folded ids | 343 and 343 |
| Order numbers on more than one shipment | 0 |
| Shipment lines, distinct (shipment, sku) | 1,694 and 1,694 |
| GPS rows, distinct ping ids | 200 and 200 |
| Shipments with two pings at the same instant | 0 |
| Shipments whose position is older than their newest ping | 0 |

## 8. Circuit breakers
| Measure | Value |
|---|---|
| C1 outage on, circuit open | 5.2 s (3 failed polls, one retry each) |
| C1 fault cleared, circuit closed | 16.8 s (the cooldown had backed off to 16 s) |
| C3 outage on, circuit open | 4.3 s |
| Tower down, kill to answering | 7.0 s |

C1 as the 0.25 s observer saw it (seconds from the start): open 18.2, half open 23.1, open 23.4, half open 32.4, open 32.9, half open 49.3, closed 49.5. Each failed probe doubled the cooldown, 4 to 8 to 16 s. C3: open 53.9, closed 62.2, the first sample after the restart.

| Carrier | Polls | Failed polls | Opened | Closed again | State at the end |
|---|---|---|---|---|---|
| C1 | 31 | 7 | 3 | 1 | closed |
| C2 | 48 | 4 | 0 | 0 | closed |
| C3 | 46 | 6 | 1 | 0 | closed (reset at the restart, see 12.1) |

"Closed again" counts probes that closed a circuit. C3 never needed one: the new process starts with a closed breaker and now writes that at startup (audit `carrier.circuit`, `why: restart`).

## 9. What the UI reads
`/health/feeds` as asha, at the end of each phase:

| Feed | Baseline | C1 down | Bad ERP files | 30% flaky | After restart | Faults cleared | After replay |
|---|---|---|---|---|---|---|---|
| erp | warning | warning | warning | warning | warning | warning | warning |
| gps | ok | ok | ok | ok | warning | warning | ok |
| bol | - | - | - | warning | warning | warning | warning |
| alerts | ok | ok | ok | ok | ok | ok | ok |
| carrier C1 | ok | degraded, open | degraded, open | warning | warning | warning | warning |
| carrier C2 | ok | ok | ok | ok | warning | warning | warning |
| carrier C3 | ok | ok | ok | warning | warning | warning | warning |

Every circuit not marked open read closed. 272 observer samples. While the tower was down the read failed (3 samples; a refused localhost connect takes about 2 s on Windows). `/live` carried the same circuits as `/health/feeds` in all 67 comparisons.

## 10. Recovery
Replay of every waiting dead letter that has a suggested fix, through `POST /dead-letters/{id}/replay`. The response now says what the replay did: `applied` (changed the data), `unchanged` (the content was already held), `stale` (older than the data held, so not applied) or `failed`.

| Feed | Reason | Replayed | Applied | Unchanged | Stale | Failed again |
|---|---|---|---|---|---|---|
| bol | unknown_shipment | 1 | 1 | 0 | 0 | 0 |
| erp | bad_amount | 12 | 6 | 0 | 6 | 0 |
| erp | missing_promised_at | 22 | 6 | 3 | 13 | 0 |
| erp | unknown_zone | 24 | 3 | 1 | 19 | 1 |
| gps | clock_skew_future | 12 | 12 | 0 | 0 | 0 |
| **All** | | **71** | **28** | **4** | **38** | **1** |

28 of 176 dead letters recovered: 4 new shipments, 11 updated ones, 12 GPS pings, 1 BOL. 42 replays were closed as no-ops (4 unchanged, 38 stale), which is right, but they are not recoveries. No replay moved the promised date of a shipment already in the database. 106 left waiting, none with an automatic fix: 85 malformed ERP lines, 12 conflicting duplicates, 1 missing promised date (no ETA to copy), 4 unreadable or wrong-value BOLs, 4 bad pings.

## 11. Reprocessing the same input
| Step | State md5 before | After | Unchanged |
|---|---|---|---|
| ERP files again (file hash seen) | 66da6ceb92 | 66da6ceb92 | yes |
| ERP files again, file memory wiped (row level) | 66da6ceb92 | 66da6ceb92 | yes |
| Whole GPS burst again | 66da6ceb92 | 66da6ceb92 | yes |
| 20 BOL PDFs again | 66da6ceb92 | 66da6ceb92 | yes |

## 12. What broke or surprised me, and what changed
Nothing was lost and nothing was duplicated, in either run. The first run found three faults and one thing that only looked like one. All three are fixed; the tables above are the run after the fixes.

1. **A restart left a stale circuit state.** Fixed.
   - Cause: `feed_health.circuit_state` was written only on a breaker transition. C3 was open when the tower died and healed while it was down. The new process starts with a closed breaker and its polls succeeded, but nothing wrote `closed`, so `/health/feeds` said degraded and open to the end of the run.
   - Fix (`tower/carrier.py`): each poller writes its fresh breaker state at startup (and clears the old "circuit open" note, audited with `why: restart`), and every poll now writes the breaker state, not only a transition.
   - Test: `tests/test_feeds_db.py::test_restart_does_not_keep_a_stale_open_circuit`.
2. **A replay regressed good data.** Fixed.
   - What the drill saw: replays moved the promised date of 8 existing shipments, all earlier, and the row-level re-drop changed 1 shipment (SH-5103: the 05:00 row said 14 Nov 17:23, the replayed 08:00 row set 05:23, the re-dropped 10:00 export put it back).
   - Cause: the replay did go through the staleness check, but the check compares with `source_exported_at`, and a duplicate row never moved it. The 10:00 export carried the same content as 05:00, so it was filed as a duplicate and the shipment still said 05:00. The replayed 08:00 row then looked newer than the data, and its suggested fix (departed + the lane's transit) replaced a real promised date. The re-drop change was the same fault seen from the other side.
   - Fix (`tower/erp.py`, `tower/replay.py`): a duplicate from a newer export moves `source_exported_at` forward; an older row is `stale` whatever its content; and a suggested fix that fills a blank field keeps the value the shipment already has. On a new shipment it still fills the blank.
   - Tests: `tests/test_replay_db.py` (stale replay and row-level re-drop, blank fill keeps the stored date, blank fill on a new shipment).
3. **"Recovered" counted no-ops.** Fixed.
   - Cause: every replay that did not fail was counted as recovered, including the ones the pipeline refused as stale or found already held.
   - Fix: the replay response, the audit entry and the feed counters carry `result` (applied, unchanged, stale, failed); the drill's table splits them; the Data health page says "Replayed", "Replayed, nothing to change" or "Not applied: newer data held".
   - Tests: `tests/test_replay_db.py` (results for ERP and GPS replays).
4. **30% flaky never opens a breaker. This is by design.** Each poll retries once, so a 30% failure rate fails a poll only about 9% of the time, and the breaker needs 3 failed polls in a row. C2 failed 4 of 48 polls and never opened. The breaker exists for a carrier that is down, not for one that is noisy; noise shows in the failed-poll count and the accept rate.

### 12.5 Before the fixes
The first run, same seed, same script:

| Measure | Before | After |
|---|---|---|
| C3 on `/health/feeds` after the restart | degraded, open (to the end of the run) | closed from the first sample after the restart |
| Existing shipments whose promised date a replay moved | 8 (all earlier) | 0 |
| Row-level re-drop of the ERP files | 1 shipment changed (md5 d64b19fac4 to 664ec304fa) | unchanged (66da6ceb92) |
| Replays reported as recovered | 70 of 176 | 28 applied of 176 |
| ... of which changed nothing | 36 (30 stale, 6 duplicate), not shown | 42 (38 stale, 4 unchanged), shown |
| Replays that failed again | 1 | 1 |
| State md5 after recovery | d64b19fac4 | 66da6ceb92 |

The counts in sections 5 to 7 were the same in both runs, except the GPS duplicates suppressed (30 then, 33 now: the sender retried 3 requests). The breaker timings and poll counts vary by run. Fewer replays apply now (28 against 34 that changed state before) because the 8 that regressed promised dates, and the rows behind them, are now refused as stale or already held.

Smaller notes:
- `warning` means "accept rate under 95%". A normal morning here has dead letters in every export, so erp reads `warning` from the first minute and bol after its first bad PDF. A feed that is always `warning` carries no signal.
- GPS went `warning` after the burst (its dead letters and forged requests count as rejected) and back to `ok` after the replay.
- The kill landed with 2 files left in the inbox. The ERP worker commits a file atomically, so a file cut by the kill is read again from the top on restart. The GPS sender retried 3 requests and no ping was stored twice.
- The drill kills the process tree: the venv `python.exe` is a launcher with the interpreter as its child.

## 13. What the drill does not cover
No model calls (triage, chat, embeddings), no contract ingest, no UI, no outbox or chat webhook traffic, one GPS sender, one ERP source, a hard kill of the tower but not of Postgres, and a parked clock, so nothing here measures the time-dependent risk maths.
