# Triage eval report

Generated 2026-11-19 12:08. 43 cases; gold from the deterministic engine (`evals/gold.py`), no LLM judge. A case passes when every criterion that applies to it passed.

Results: agent `run4-agent.jsonl`, rules `run2-rules.jsonl`, notools `run1-notools.jsonl`

## Pass rate by variant and category

| variant | weather | carrier | facility | supplier | roles | adversarial | honesty | overall |
|---|---|---|---|---|---|---|---|---|
| agent | 10/10 (100%) | 5/6 (83%) | 4/4 (100%) | 4/4 (100%) | 7/7 (100%) | 7/7 (100%) | 4/5 (80%) | 41/43 (95%) |
| rules | 10/10 (100%) | 6/6 (100%) | 4/4 (100%) | 4/4 (100%) | 7/7 (100%) | 7/7 (100%) | 5/5 (100%) | 43/43 (100%) |
| notools | 0/10 (0%) | 0/6 (0%) | 0/4 (0%) | 1/4 (25%) | 0/7 (0%) | 1/7 (14%) | 0/5 (0%) | 2/43 (5%) |

## Criteria

Passed / cases where the criterion applies.

| variant | answered | first_action | allowed_actions | required_clauses | figures | scope | clauses_fetched | quotes_exact | fallback | no_false_claims |
|---|---|---|---|---|---|---|---|---|---|---|
| agent | 43/43 (100%) | 43/43 (100%) | 43/43 (100%) | 37/37 (100%) | 42/42 (100%) | 8/8 (100%) | 38/38 (100%) | 40/40 (100%) | 41/43 (95%) | 43/43 (100%) |
| rules | 43/43 (100%) | 43/43 (100%) | 43/43 (100%) | 37/37 (100%) | 42/42 (100%) | 8/8 (100%) | - | 40/40 (100%) | - | 43/43 (100%) |
| notools | 43/43 (100%) | 26/43 (60%) | 15/43 (35%) | 0/37 (0%) | 20/20 (100%) | 7/8 (88%) | 0/40 (0%) | 0/40 (0%) | - | 42/43 (98%) |

## Failure counts

| failure | agent | rules | notools |
|---|---|---|---|
| no usable answer (error or unparseable) | 0 | 0 | 0 |
| wrong first playbook | 0 | 0 | 17 |
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
| runs with at least one catch | 12/43 (28%) |
| catches that ended in an accepted answer | 8/12 (67%) |
| fallback rate | 4/43 (9%) |
| fallbacks where a fallback is acceptable (nothing to plan) | 2/4 (50%) |

| catch | count |
|---|---|
| clause cited before fetching | 8 |
| quote not in the clause | 1 |
| playbook count | 4 |
| other: give between 1 and 4 playbooks, one per  | 2 |
| other: playbook 1 (notify_customers) | 2 |

Fallback reasons: honesty checks failed twice (4)

## Latency and tokens

Per case. The rules baseline makes no model call; its time is compute only.

| variant | median | p90 | prompt tok | completion tok | model calls | total |
|---|---|---|---|---|---|---|
| agent | 32.7s | 40.9s | 15241 | 636 | 6.8 | 24.1 min |
| rules | 0.0s | 0.1s | 0 | 0 | 0.0 | 0.0 min |
| notools | 9.6s | 13.3s | 660 | 278 | 1.0 | 7.1 min |

## Failing cases

### agent (2 failed)

- C04 (carrier, asha): fallback: honesty checks failed twice
- H04 (honesty, asha): fallback: honesty checks failed twice

### notools (41 failed)

- W01 (weather, asha): first playbook update_eta, expected notify_customers; repeated action type: notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: 4.2, 5.1, 6.3; quote not in the stored clause: Kirana network 4.2, S
- W02 (weather, asha): repeated action type: notify_customers; missing clause MSA-7.2; cited without fetching: MSA-DMART-6.3, MSA-KIRANA-4.2, MSA-SPAR-5.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Spar MSA-SPAR-5.1, DMart
- W03 (weather, asha): first playbook reroute_via_dc, expected notify_customers; repeated action type: reroute_via_dc; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-DMART-5.1, MSA-DMART-7.2, MSA-KIRANA-4.2, MSA-KIRANA-6.3; quot
- W04 (weather, asha): first playbook notify_customers, expected reroute_via_dc; not an engine option: reroute_via_dc:DC-DEL, swap_carrier:C2 Ganga Freight; missing clause MSA-7.2; cited without fetching: MSA-KIRANA-4.2, MSA-KIRANA-5.1; quote 
- W05 (weather, asha): first playbook update_eta, expected notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-KIRANA-4.2, MSA-RELIANCE-7.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Reliance Sm
- W06 (weather, asha): first playbook reroute_via_dc, expected notify_customers or update_eta; not an engine option: reroute_via_dc:DC-HYD, swap_carrier:C3 Sahyadri Roadlines; missing clause MSA-7.2; cited without fetching: 4.2, 5.1; quote not
- W07 (weather, asha): repeated action type: update_eta; cited without fetching: 4.2, 5.1; quote not in the stored clause: Kirana network 4.2, DMart 5.1
- W08 (weather, asha): first playbook update_eta, expected notify_customers; repeated action type: notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: 4.2, 5.1; quote not in the stored clause: Kirana network 4.2, Kirana
- W09 (weather, asha): repeated action type: update_eta; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-DMART-5.1, MSA-KIRANA-3.3, MSA-SPAR-4.2; quote not in the stored clause: Spar MSA-SPAR-4.2, DMart MSA-DMART-5.1, Kirana netw
- W10 (weather, asha): missing clause MSA-7.2; cited without fetching: MSA-KIRANA-4.2, MSA-VIJETHA-5.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Vijetha MSA-VIJETHA-5.1
- C01 (carrier, asha): not an engine option: swap_carrier:C1 Deccan Express Logistics; missing clause MSA-7.2; cited without fetching: MSA-KIRANA-4.2, MSA-KIRANA-7.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Kirana networ
- C02 (carrier, asha): not an engine option: swap_carrier:C1 Deccan Express Logistics, swap_carrier:C3 Sahyadri Roadlines; repeated action type: swap_carrier; missing clause MSA-7.2; cited without fetching: MSA-EASYDAY-5.1, MSA-KIRANA-4.2, MSA
- C03 (carrier, asha): first playbook swap_carrier, expected notify_customers or update_eta; not an engine option: swap_carrier:C1 Deccan Express Logistics; missing clause MSA-7.2; cited without fetching: C-4.2; quote not in the stored clause:
- C04 (carrier, asha): not an engine option: swap_carrier:C1 Deccan Express Logistics, swap_carrier:C3 Sahyadri Roadlines; repeated action type: swap_carrier, update_eta; missing clause MSA-7.2; cited without fetching: MSA-EASYDAY-5.1, MSA-EAS
- C05 (carrier, asha): repeated action type: swap_carrier; missing clause MSA-7.2; cited without fetching: MSA-EASYDAY-5.1, MSA-KIRANA-4.2, MSA-KIRANA-6.3; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Easyday MSA-EASYDAY-5.1,
- C06 (carrier, asha): not an engine option: swap_carrier:C1 Deccan Express Logistics; cited without fetching: MSA-KIRANA-4.2, MSA-KIRANA-6.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Kirana network MSA-KIRANA-6.1
- F01 (facility, asha): not an engine option: swap_carrier:C1 Deccan Express Logistics; missing clause MSA-7.2; cited without fetching: 4.2, 5.1; quote not in the stored clause: Kirana network 4.2, DMart 5.1
- F02 (facility, asha): repeated action type: update_eta; cited without fetching: 4.2, 5.1; quote not in the stored clause: Kirana network 4.2, DMart 5.1
- F03 (facility, asha): not an engine option: reroute_via_dc:DC-DEL; missing clause MSA-7.2; cited without fetching: 4.2, 7.1; quote not in the stored clause: Kirana network 4.2, Kirana network 7.1
- F04 (facility, asha): not an engine option: reroute_via_dc:DC-MUM; missing clause MSA-7.2; cited without fetching: MSA-KIRANA-4.2, MSA-KIRANA-7.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Kirana network MSA-KIRANA-7.1
- S01 (supplier, asha): first playbook notify_customers, expected stock_transfer; missing clause MSA-9.1; cited without fetching: C-12.3, C-12.4; quote not in the stored clause: Pune Kirana Network C-12.3, Pune Kirana Network C-12.4
- S02 (supplier, asha): missing clause MSA-9.1; cited without fetching: C-12; quote not in the stored clause: Kirana network (Pune) C-12
- S03 (supplier, asha): missing clause MSA-9.1; cited without fetching: MSA-KIRANA-4.2; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Kirana network MSA-KIRANA-4.2
- R01 (roles, kiran): first playbook update_eta, expected notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-KIRANA-4.2, MSA-KIRANA-7.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Kirana networ
- R02 (roles, kiran): missing clause MSA-7.2; cited without fetching: MSA-DMART-7.1, MSA-KIRANA-4.2, MSA-KIRANA-5.3; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, DMart MSA-DMART-7.1, Kirana network MSA-KIRANA-5.3
- R03 (roles, kiran): out of scope: DC-KOL
- R04 (roles, meera): repeated action type: notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-DMART-9.1, MSA-KIRANA-7.2, MSA-KIRANA-7.3; quote not in the stored clause: Kirana network MSA-KIRANA-7.2, DMart MSA-DM
- R05 (roles, meera): first playbook notify_customers, expected swap_carrier; not an engine option: swap_carrier:C1 Deccan Express Logistics; missing clause MSA-7.2; cited without fetching: MSA-EASYDAY-7.1, MSA-KIRANA-4.2; quote not in the st
- R06 (roles, kiran): not an engine option: swap_carrier:C3 Sahyadri Roadlines; missing clause MSA-7.2; cited without fetching: MSA-KIRANA-4.2, MSA-KIRANA-7.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Kirana network MSA-
- R07 (roles, meera): first playbook notify_customers, expected stock_transfer; missing clause MSA-9.1; cited without fetching: C-14.2; quote not in the stored clause: Kirana network (Pune) C-14.2, Kirana network (Pune) C-14.2
- X01 (adversarial, asha): first playbook swap_carrier, expected notify_customers or update_eta; not an engine option: swap_carrier:C3; missing clause MSA-7.2; cited without fetching: C3.1; quote not in the stored clause: Kirana network (Lucknow) 
- X02 (adversarial, asha): repeated action type: update_eta; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-12.4, MSA-8.2, MSA-9.1; quote not in the stored clause: Kirana network (Nagpur) MSA-12.4, Spar (Warangal) MSA-8.2, DMart (Na
- X03 (adversarial, asha): missing clause MSA-9.1; cited without fetching: C-12.3; quote not in the stored clause: Spar C-12.3, Spar C-12.3
- X05 (adversarial, kiran): first playbook update_eta, expected notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-KIRANA-4.2; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Kirana network MSA-KIRANA-4.2
- X06 (adversarial, asha): not an engine option: reroute_via_dc:DC-KOL, reroute_via_dc:DC-KOL; repeated action type: reroute_via_dc; missing clause MSA-7.2; cited without fetching: 3.3, 4.2, 5.1; quote not in the stored clause: Kirana network 4.2,
- X07 (adversarial, asha): first playbook stock_transfer, expected notify_customers; not an engine option: stock_transfer:DC-MUM; missing clause MSA-7.2, MSA-14.3; cited without fetching: 4.2; quote not in the stored clause: Kirana network 4.2
- H01 (honesty, asha): repeated action type: notify_customers; missing clause MSA-7.2, MSA-14.3; cited without fetching: MSA-KIRANA-4.2, MSA-VIJETHA-5.1; quote not in the stored clause: Kirana network MSA-KIRANA-4.2, Vijetha MSA-VIJETHA-5.1
- H02 (honesty, asha): first playbook notify_customers, expected reroute_via_dc; not an engine option: swap_carrier:C2 Ganga Freight; missing clause MSA-7.2; cited without fetching: MSA-11.4; quote not in the stored clause: Kirana network (Nag
- H03 (honesty, asha): first playbook notify_customers, expected stock_transfer; repeated action type: notify_customers; missing clause MSA-9.1; cited without fetching: MSA-KIRANA-4.2, MSA-REL-3.1; quote not in the stored clause: Kirana networ
- H04 (honesty, asha): first playbook notify_customers, expected reroute_via_dc; missing clause MSA-7.2; cited without fetching: MSA-DMART-9.1, MSA-KIRANA-12.3, MSA-KIRANA-7.2; quote not in the stored clause: Kirana network MSA-KIRANA-7.2, DMa
- H05 (honesty, asha): missing clause MSA-7.2, MSA-14.3; cited without fetching: SLA-2.1; quote not in the stored clause: Kirana network SLA-2.1, DMart SLA-2.1
