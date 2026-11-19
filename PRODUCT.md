# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
- Primary (confirmed by Daman, 2026-11-17): a portfolio visitor. A recruiter or hiring manager for Forward Deployed Engineer and AI roles, with about 60 seconds and no logistics knowledge. They must get the story without help.
- Inside the story: three demo roles. Asha (VP Operations, all of India), Kiran (Dispatcher South, DC-HYD only, phones masked) and Meera (Finance, no driver data, cannot execute).

## Product Purpose
A logistics control tower for Tarangini Distribution, a fictional Indian FMCG distributor. On a live morning it ranks disruptions by money at risk. An AI agent triages each one, prices the exposure from the customers' real contract clauses, and proposes playbooks. A person approves anything that moves more than Rs 5 L of goods. Success: the visitor walks away believing **an AI agent catches a disruption, prices it in rupees from real contracts, and proposes a fix a person approves.**

## Positioning
- The agent never does arithmetic. Money comes from a deterministic engine, and clauses come from real contract PDFs.
- Code checks every rupee figure and every cited clause. If the agent cites something it did not fetch, the run falls back to a rule plan.
- Everything runs on the owner's own machine: a local model (fusion27b) at Rs 0 per run, with real latency (about 40-60 s) and real token counts shown.

## Operating Context
- The demo morning is 2026-11-13, scenario clock 10:40 IST. 4 DCs (HYD, MUM, DEL, KOL), 36 cities, 3 carriers, about 344 trucks, Rs 13.87 Cr moving.
- Six incidents:
  - Ganga Freight strike in UP, Rs 1,78,163;
  - NH44 closed near Adilabad, Rs 1,73,622, all waivable if customers are told by 11:10;
  - cyclone watch on the Odisha coast, Rs 1,27,837;
  - Kolkata DC conveyor down, Rs 55,181;
  - supplier 5 days late, Rs 27,865;
  - fog on the Yamuna Expressway, Rs 4,269.
- Playbooks: notify customers, re-route via another DC, swap carrier, transfer stock, update ETAs. A mover is offered only if it saves more than it costs.
- Data health: dead letters from broken ERP files, bad bill-of-lading PDFs and flaky carrier APIs, with replay and discard. An Outage toggle injects faults.
- Everything is written to an append-only audit log.

## Capabilities and Constraints
- Backend: FastAPI + Postgres (pgvector). UI: Next.js 16 + TypeScript in `web/`, behind a server-side proxy that keeps bearer tokens out of the browser.
- Endpoints:
  - incidents: `/incidents`, `/incidents/{id}/brief`, `/incidents/{id}/triage`;
  - playbooks: `/playbooks/{id}/run`, `/playbooks/{id}/approve`;
  - live state: `/network`, `/live`, `/outage`;
  - data health: `/health/feeds`, `/dead-letters`;
  - audit: `/audit`.
- Local only: no cloud, no external CDN or font requests at runtime.
- Triage takes 40-60 s on the local GPU and must be shown honestly (elapsed time, no fake streaming).
- The visual remake must keep every function: roles, masking, approvals, data health, audit, outage.

## Evidence on Hand
- 12 generated contract PDFs (240 clause chunks), 60 bill-of-lading PDFs, agent runs with real ms and token counts, and the audit trail. All data is synthetic, for a fictional company.
- Demo numbers above come from the M2 demo and the 2026-11-14 restage.
- Absent: real customers, testimonials and production deployment. Never claim them.

## Product Principles
1. The story first: a visitor understands what happened, what it costs and what the agent recommends before any detail.
2. Show the proof, not the claim: real clause text, real rupee figures and real timings, each traceable.
3. A person stays in charge: the approve moment is visible and deliberate.
4. Honest about the machine: latency, fallbacks and limits are shown, not hidden.

## Accessibility & Inclusion
- Keyboard reachable and visible focus.
- Reduced motion respected.
- Text contrast at WCAG AA or better.
- Works from 375 px phones to 1920 px desktops with the same behaviour.
