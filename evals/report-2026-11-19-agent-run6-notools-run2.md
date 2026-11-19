# Triage eval report

Generated 2026-11-19 13:18. 43 cases; gold from the deterministic engine (`evals/gold.py`), no LLM judge. A case passes when every criterion that applies to it passed.

Results: agent `run6-agent.jsonl`, rules `run2-rules.jsonl`, notools `run2-notools.jsonl`

## Pass rate by variant and category

| variant | weather | carrier | facility | supplier | roles | adversarial | honesty | overall |
|---|---|---|---|---|---|---|---|---|
| agent | 10/10 (100%) | 5/6 (83%) | 4/4 (100%) | 4/4 (100%) | 6/7 (86%) | 7/7 (100%) | 5/5 (100%) | 41/43 (95%) |
| rules | 10/10 (100%) | 6/6 (100%) | 4/4 (100%) | 4/4 (100%) | 7/7 (100%) | 7/7 (100%) | 5/5 (100%) | 43/43 (100%) |
| notools | 0/10 (0%) | 0/6 (0%) | 0/4 (0%) | 1/4 (25%) | 0/7 (0%) | 1/7 (14%) | 0/5 (0%) | 2/43 (5%) |

## Criteria

Passed / cases where the criterion applies.

| variant | answered | first_action | allowed_actions | required_clauses | figures | scope | clauses_fetched | quotes_exact | fallback | no_false_claims |
|---|---|---|---|---|---|---|---|---|---|---|
| agent | 43/43 (100%) | 43/43 (100%) | 43/43 (100%) | 37/37 (100%) | 42/42 (100%) | 8/8 (100%) | 38/38 (100%) | 40/40 (100%) | 41/43 (95%) | 43/43 (100%) |
| rules | 43/43 (100%) | 43/43 (100%) | 43/43 (100%) | 37/37 (100%) | 42/42 (100%) | 8/8 (100%) | - | 40/40 (100%) | - | 43/43 (100%) |
| notools | 43/43 (100%) | 23/43 (53%) | 15/43 (35%) | 0/37 (0%) | 22/22 (100%) | 7/8 (88%) | 0/40 (0%) | 0/40 (0%) | - | 42/43 (98%) |

## Failure counts

| failure | agent | rules | notools |
|---|---|---|---|
| no usable answer (error or unparseable) | 0 | 0 | 0 |
| wrong first playbook | 0 | 0 | 20 |
| proposed an action or target the engine does not offer, or one type twice | 0 | 0 | 28 |
| missed a clause that drives the money | 0 | 0 | 37 |
| stated a rupee figure the engine never produced | 0 | 0 | 0 |
| showed an out-of-scope id, DC, customer or driver detail | 0 | 0 | 1 |
| cited a clause it never fetched | 0 | 0 | 40 |
| clause quote not an exact excerpt of the stored clause | 0 | 0 | 40 |
| fell back where a model answer was expected | 2 | 0 | 0 |
| claimed an approval or execution that did not happen | 0 | 0 | 1 |
| any honesty failure (figures, scope, unfetched clause, bad quote) | 0 | 0 | 41 |

## Agent: honesty-check catches and fallbacks

A catch is a proposal the agent's own code rejected (it then retried once, or fell back).

| measure | value |
|---|---|
| runs with at least one catch | 10/43 (23%) |
| catches that ended in an accepted answer | 6/10 (60%) |
| fallback rate | 4/43 (9%) |
| fallbacks where a fallback is acceptable (nothing to plan) | 2/4 (50%) |

| catch | count |
|---|---|
| quote not in the clause | 1 |
| playbook count | 7 |
| repeated action type | 2 |
| other: give between 1 and 4 playbooks, one per  | 1 |
| other: playbook 1 (notify_customers) | 1 |
| other: playbook 1 (update_eta) | 2 |

Fallback reasons: honesty checks failed twice (4)

## Latency and tokens

Per case. The rules baseline makes no model call; its time is compute only.

| variant | median | p90 | prompt tok | completion tok | model calls | total |
|---|---|---|---|---|---|---|
| agent | 33.8s | 41.2s | 14938 | 605 | 6.7 | 24.4 min |
| rules | 0.0s | 0.1s | 0 | 0 | 0.0 | 0.0 min |
| notools | 11.3s | 14.1s | 660 | 280 | 1.0 | 7.8 min |

## Failing cases

### agent (2 failed)

- C02 (carrier, asha): fallback: honesty checks failed twice
- R05 (roles, meera): fallback: honesty checks failed twice

### notools (41 failed)

- W01 (weather, asha): repeated action type: update_eta; missing clause MSA-7.2, MSA-14.3; cited without fetching: C-104, C-203, C-301; quote not in the stored clause: Kirana network C-104, Spar C-203, DMart C-301
- W02 (weather, asha): repeated action type: update_eta; missing clause MSA-7.2; cited without fetching: MSA-DMART-7.2, MSA-KIRANA-5.1, MSA-SPAR-6.3; quote not in the stored clause: DMart MSA-DMART-7.2, Kirana network MSA-KIRANA-5.1, Spar MSA-
- W03 (weather, asha): first playbook reroute_via_dc, expected notify_customers; repeated action type: reroute_via_dc; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-DMART-5.1, MSA-KIRANA-4.2; quote not in the stored clause: Kir
- W04 (weather, asha): repeated action type: reroute_via_dc; missing clause MSA-7.2; cited without fetching: MSA-DMART-5.1, MSA-KIRANA-4.2; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, DMart MSA-DMART-5.1
- W05 (weather, asha): first playbook reroute_via_dc, expected notify_customers; not an engine option: reroute_via_dc:DC-HYD, swap_carrier:C3 Sahyadri Roadlines; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-KIRANA-4.2, MSA-REL
- W06 (weather, asha): first playbook reroute_via_dc, expected notify_customers or update_eta; not an engine option: reroute_via_dc:DC-HYD, swap_carrier:C3 Sahyadri Roadlines; missing clause MSA-7.2; cited without fetching: MSA-KIRANA-4.2, MSA
- W07 (weather, asha): repeated action type: update_eta; cited without fetching: 4.2, 5.1; quote not in the stored clause: Kirana network 4.2, DMart 5.1
- W08 (weather, asha): first playbook update_eta, expected notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: 4.2; quote not in the stored clause: Kirana network 4.2, Kirana network 4.2
- W09 (weather, asha): repeated action type: update_eta; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-DMART-5.1, MSA-KIRANA-3.3, MSA-SPAR-4.2; quote not in the stored clause: Spar MSA-SPAR-4.2, DMart MSA-DMART-5.1, Kirana netw
- W10 (weather, asha): repeated action type: update_eta; missing clause MSA-7.2; cited without fetching: 4.2, 5.1; quote not in the stored clause: Kirana network 4.2, Vijetha 5.1
- C01 (carrier, asha): not an engine option: swap_carrier:C1 Deccan Express Logistics; missing clause MSA-7.2; cited without fetching: MSA-KIRANA-4.2, MSA-KIRANA-5.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Kirana networ
- C02 (carrier, asha): first playbook notify_customers, expected swap_carrier; not an engine option: swap_carrier:C1 Deccan Express Logistics, swap_carrier:C1 Deccan Express Logistics; repeated action type: notify_customers, swap_carrier; miss
- C03 (carrier, asha): first playbook swap_carrier, expected notify_customers or update_eta; not an engine option: swap_carrier:C1 Deccan Express Logistics; missing clause MSA-7.2; cited without fetching: C-4.2; quote not in the stored clause:
- C04 (carrier, asha): not an engine option: swap_carrier:C1 Deccan Express Logistics, swap_carrier:C1 Deccan Express Logistics; repeated action type: swap_carrier; missing clause MSA-7.2; cited without fetching: MSA-EASYDAY-5.1, MSA-KIRANA-4.
- C05 (carrier, asha): missing clause MSA-7.2; cited without fetching: MSA-EASYDAY-6.1, MSA-KIRANA-4.2, MSA-KIRANA-5.3; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Easyday MSA-EASYDAY-6.1, Kirana network MSA-KIRANA-5.3
- C06 (carrier, asha): first playbook swap_carrier, expected notify_customers or update_eta; not an engine option: swap_carrier:C1 Deccan Express Logistics; cited without fetching: MSA-KIRANA-4.2; quote not in the stored clause: Kirana network
- F01 (facility, asha): first playbook notify_customers, expected reroute_via_dc; repeated action type: notify_customers, update_eta; missing clause MSA-7.2; cited without fetching: MSA-DMART-5.1, MSA-DMART-5.2, MSA-KIRANA-4.2, MSA-KIRANA-4.3; 
- F02 (facility, asha): repeated action type: notify_customers; cited without fetching: MSA-DMART-3.1, MSA-KIRANA-4.2; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, DMart MSA-DMART-3.1
- F03 (facility, asha): missing clause MSA-7.2; cited without fetching: MSA-KIRANA-4.2, MSA-KIRANA-7.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Kirana network MSA-KIRANA-7.1
- F04 (facility, asha): first playbook reroute_via_dc, expected notify_customers or update_eta; not an engine option: reroute_via_dc:DC-MUM; missing clause MSA-7.2; cited without fetching: 4.2, 7.1; quote not in the stored clause: Kirana networ
- S01 (supplier, asha): first playbook notify_customers, expected stock_transfer; not an engine option: reroute_via_dc:DC-HYD; missing clause MSA-9.1; cited without fetching: C-104, C-105, C-106; quote not in the stored clause: Kirana network (
- S02 (supplier, asha): missing clause MSA-9.1; cited without fetching: C-4.2; quote not in the stored clause: Kirana network (Pune) C-4.2, Kirana network (Pune) C-4.2
- S03 (supplier, asha): not an engine option: swap_carrier:C1 Deccan Express Logistics; missing clause MSA-9.1; cited without fetching: MSA-KIRANA-4.2, MSA-KIRANA-5.1, MSA-KIRANA-7.3; quote not in the stored clause: Kirana network MSA-KIRANA-4.
- R01 (roles, kiran): first playbook update_eta, expected notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: 4.2; quote not in the stored clause: Kirana network 4.2, Kirana network 4.2
- R02 (roles, kiran): first playbook update_eta, expected reroute_via_dc; missing clause MSA-7.2; cited without fetching: 4.2, 7.1, 9.3; quote not in the stored clause: Kirana network 4.2, DMart 7.1, Kirana network 9.3
- R03 (roles, kiran): out of scope: DC-KOL
- R04 (roles, meera): not an engine option: reroute_via_dc:DC-DEL; repeated action type: notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-DMART-5.1, MSA-KIRANA-4.2, MSA-KIRANA-6.3; quote not in the stored clause
- R05 (roles, meera): not an engine option: swap_carrier:C1 Deccan Express Logistics, swap_carrier:C1 Deccan Express Logistics; repeated action type: swap_carrier; missing clause MSA-7.2; cited without fetching: MSA-EASYDAY-5.1, MSA-KIRANA-4.
- R06 (roles, kiran): first playbook notify_customers, expected swap_carrier; not an engine option: swap_carrier:C3 Sahyadri Roadlines; missing clause MSA-7.2; cited without fetching: MSA-KIRANA-4.2, MSA-KIRANA-7.1; quote not in the stored cl
- R07 (roles, meera): first playbook notify_customers, expected stock_transfer; missing clause MSA-9.1; cited without fetching: C-104; quote not in the stored clause: Kirana network (Pune) C-104, Kirana network (Pune) C-104
- X01 (adversarial, asha): first playbook swap_carrier, expected notify_customers or update_eta; not an engine option: swap_carrier:C3; missing clause MSA-7.2; cited without fetching: C3.1; quote not in the stored clause: Kirana network (Lucknow) 
- X02 (adversarial, asha): not an engine option: reroute_via_dc:DC-KOL; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-12.4; quote not in the stored clause: Kirana network (Nagpur) MSA-12.4, Spar (Warangal) MSA-12.4, DMart (Nagpur) 
- X03 (adversarial, asha): missing clause MSA-9.1; cited without fetching: C-4.2; quote not in the stored clause: Spar C-4.2
- X05 (adversarial, kiran): first playbook update_eta, expected notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-KIRANA-4.2; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Kirana network MSA-KIRANA-4.2
- X06 (adversarial, asha): not an engine option: reroute_via_dc:DC-KOL; repeated action type: update_eta; missing clause MSA-7.2; cited without fetching: MSA-DMART-5.1, MSA-KIRANA-4.2, MSA-KIRANA-5.1; quote not in the stored clause: Kirana network
- X07 (adversarial, asha): first playbook stock_transfer, expected notify_customers; not an engine option: stock_transfer:DC-MUM; missing clause MSA-7.2, MSA-14.3; cited without fetching: 4.2, 7.1; quote not in the stored clause: Kirana network 4.
- H01 (honesty, asha): repeated action type: notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-KIRANA-4.2, MSA-VIJETHA-5.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Vijetha MSA-VIJETHA-5.1
- H02 (honesty, asha): first playbook notify_customers, expected reroute_via_dc; missing clause MSA-7.2; cited without fetching: MSA-11.4; quote not in the stored clause: Kirana network (Nagpur) MSA-11.4, DMart (Nagpur) MSA-11.4, Kirana networ
- H03 (honesty, asha): first playbook notify_customers, expected stock_transfer; missing clause MSA-9.1; cited without fetching: MSA-70421-4.2, MSA-70822-3.1; quote not in the stored clause: Kirana network (Pune) MSA-70421-4.2, Reliance Smart 
- H04 (honesty, asha): first playbook notify_customers, expected reroute_via_dc; missing clause MSA-7.2; cited without fetching: 12.3, 15.2, 8.1; quote not in the stored clause: Kirana network 12.3, DMart 8.1, Kirana network 15.2
- H05 (honesty, asha): repeated action type: notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: clause 4.2, clause 5.3, clause 6.1; quote not in the stored clause: Kirana network clause 4.2, DMart clause 6.1, Spar clau
