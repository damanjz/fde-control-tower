# Data model and dirty-data catalogue

Postgres 17 (pgvector extension, used for contract clause embeddings from milestone 2). Plain relational, foreign keys, one app user (`ct`).
`db/schema.sql` is idempotent. Every table is listed below.

## Network (reference data, loaded by `python -m tower.seed`)

- **warehouse**: the four distribution centres (`DC-HYD`, `DC-MUM`, `DC-DEL`, `DC-KOL`) with coordinates.
- **zone**: one row per delivery city (36), with state, coordinates, serving DC, road km and standard transit days. A
  shipment's lane is DC to zone.
- **carrier**: three trucking carriers (`C1` Deccan Express Logistics, `C2` Ganga Freight, `C3` Sahyadri Roadlines)
  with a dispatch phone and a bank account string. These two columns are PII-like: since milestone 2 `/network` masks or removes them by role.
- **supplier**: 40 suppliers with hub, state, coordinates and quoted lead days. Names are cleaned on load (the source
  has stray spaces and shouting capitals).
- **product**: 200 SKUs with supplier, unit cost, unit price and case quantity. A line's value is
  `cases * case_qty * unit_price`.
- **customer**: a retail chain at a city (`Ratnadeep (Hyderabad)`); 180 rows. Every city has the Kirana network plus four
  chains, picked deterministically.
- **contract** and **contract_clause**: one contract per customer, each referencing the three clauses with their
  parameters: `MSA-7.2` late delivery (1.5% of invoice per day, capped at 10%), `MSA-9.1` fill rate below 95% (2% of
  the week's SKU invoice), `MSA-14.3` force majeure (penalties waived if the customer is notified within 6 hours).

## Operational data

- **shipment**: one row per ERP shipment id (natural key). Holds order, DC, zone, carrier, customer, cases, value,
  status (`planned`, `in_transit`, `held`, `delivered`), departed, ETA and promised times, the last GPS position
  (`last_lat`, `last_lon`, `last_position_at`), what the carrier API last said (`carrier_status*`), the `source`,
  a `version` that increases on every real change, a `content_hash` of the canonical content, and
  `source_exported_at` (the newest export that carried the stored content, a duplicate from a newer export included; a row from an older export, replayed or not, is refused as stale).
- **shipment_line**: SKU, cases and value per shipment; replaced as a set when the shipment is updated.
- **gps_ping**: every accepted ping, kept as history. `shipment_id` is a soft reference because a truck can report
  before the hourly ERP export has its shipment.
- **disruption_event**: kind (`weather`, `facility`, `carrier`, `supplier`), title, detail, severity, start time, and a
  scope: a circle (lat, lon, radius) or a DC, carrier (optionally states) or supplier. Unique on (source, external id).

## Ingestion bookkeeping

- **feed_health**: one row per feed (`erp`, `gps`, `alerts`, `carrier:C1`..`C3`): received, accepted, rejected, last
  seen, last error, circuit state (`closed`, `open`, `half_open`) and a `counters` JSON with feed-specific counts
  (duplicates, reordered, new, updated, circuit openings and so on).
- **dead_letter**: a record the pipeline refused: feed, payload (JSON), reason code, plain reason, suggested fix, an
  optional machine-applicable `suggested_patch`, status (`waiting`, `replayed`, `discarded`) and attempts. Unique per
  (feed, payload hash), so the same bad row in a later export or a reprocessed file does not pile up and a replayed or
  discarded payload is not resurrected.
- **ingest_log**: idempotency keys seen per feed: ERP file hashes, GPS ping ids, alert ids.
- **audit_log**: append-only (id, time, actor, action, target, detail). Triggers reject UPDATE, DELETE and TRUNCATE.
  Written for dead-letter replay and discard, ERP files processed, and carrier circuit changes. A scenario reset keeps it.

## How the feeds are processed

- **ERP**: header mapped by normalised spelling; each row canonicalised (zone, DC, carrier, customer, status, lines,
  amount, dates) and validated with pydantic; upsert keyed on shipment id. Same content hash: duplicate, nothing
  changes. Different content from an equal or newer export: update, `version + 1`, lines replaced. Different content
  from an older export: stale, ignored. A file is atomic and moves to `data/processed/` (or `rejected/` when no header
  can be mapped). The file's SHA-256 is remembered, so the same file again is skipped; with that memory wiped the
  row-level rules give the same result.
- **GPS**: HMAC over `timestamp.body` within a 5 minute window; ping id dedupe; event time at most 5 minutes ahead of
  now; position only moves forward in event time.
- **Carrier poller**: one thread per carrier, 1.5 s timeout, one retry with jittered exponential backoff, circuit opens
  after 3 failed polls, one probe after a cooldown (4 s, doubling to 16 s on a failed probe). While open it makes no
  calls and the shipments keep their last known carrier status; `feed_health` says so.
- **Alerts poller**: validates and upserts; severity vocabularies are normalised to low, medium, high, critical.

## Dirty-data catalogue

| Mess the simulator injects | Where | How the pipeline handles it |
|---|---|---|
| Three header dialects (`Shipment_No` / `shipment id` / `SHIP_REF` and so on), three delimiters (comma, semicolon, tab) | ERP | Headers matched by lower-cased alphanumerics against an alias table; delimiter chosen by how many header cells it maps. A file with no mappable shipment column is rejected whole. |
| BOM, CRLF and LF mixed, blank lines, padded fields | ERP | Stripped while reading; the row parses normally. |
| Dates as `DD/MM/YYYY HH:MM`, ISO with offset, ISO without, `13-Nov-2026`, Excel serial numbers | ERP | One parser, day-first, naive means IST, serials rounded to the second. All give the same instant, so a shipment re-exported in a different style is not a change. |
| Amounts as `420000.00`, `4,20,000`, `Rs. 4.2 lakh`, `4.2 L`, `1.2 Cr` | ERP | One amount parser to rupees. |
| Unreadable amounts (`TBD`, `N/A`, `see invoice`) | ERP | Dead letter `bad_amount`; the fix is the value computed from the lines, applied by replay. |
| Blank promised date | ERP | Dead letter `missing_promised_at`; the fix is departure plus the lane's standard transit. |
| Unknown city code (`HYD-2`) | ERP | Dead letter `unknown_zone`; when the base code resolves (`HYD`) the fix maps it to the zone. |
| City, carrier, DC and customer written as id, name, code or upper case | ERP | Alias tables resolve them; unresolvable values go to dead letters (`unknown_carrier`, `unknown_dc`, `unknown_customer`). |
| Lower-case shipment ids | ERP | Upper-cased before keying. |
| Same shipment twice in one file with different values | ERP | The first row wins; the second goes to dead letter `duplicate_conflict`, replayable as an update. |
| Exact duplicate rows, and unchanged re-exports in later files | ERP | Same content hash: counted as suppressed duplicates, no write. |
| A shipment re-exported with a new ETA or amended value | ERP | Update with `version + 1`. |
| An older export arriving after a newer one | ERP | Refused as stale. |
| Truncated rows, junk text, broken quoting | ERP | Dead letter `malformed_row` with the raw line. |
| Out-of-order pings | GPS | Stored, counted as reordered, never move the last position back. |
| Duplicate pings | GPS | Dropped by ping id. |
| Pings dated 20 to 45 minutes ahead (device clock skew) | GPS | Dead letter `clock_skew_future`; the fix is to use the receipt time. |
| Forged or stale-signed requests | GPS | 401, counted in `feed_health`, never stored. |
| A ping before its shipment exists | GPS | Stored; the position is attached when the ERP row arrives. |
| Error rate, latency, timeouts, outage | Carrier API | Retries with jittered backoff, then the circuit breaker; last known status kept while open. |
| Severity as colour, word or number; alerts republished every poll | Alerts | Normalised; duplicates suppressed by alert id. |
| Alert with no scope, unknown severity or kind, unknown DC or carrier | Alerts | Dead letter with a reason; replayable. |


## Milestone 2 tables (`db/migrations/002_milestone2.sql`)

- **shipment** gains `bol_no`, `vehicle_no`, `driver_name`, `driver_phone` (PII), `bol_received_at`, set from the bill of lading PDF.
- **audit_log** gains `dc_ids` (the DCs an action touched; NULL = network-wide), used to scope what a dispatcher sees.
- **msa_document**: one row per chain's contract PDF (file, hash, layout, pages). **contract** gains `msa_id`.
- **contract_chunk**: one row per clause: clause id, heading, page, text, `kind` and `params` for the money clauses (parsed from the
  PDF), `verified` (equal to the seeded clause params), `embedding vector(768)` and a generated `tsvector`.
- **fleet_capacity** (spare trucks per DC and carrier) and **dc_stock** (cases per DC and SKU): the options the playbooks work with,
  seeded deterministically by `tower.seed`.
- **incident_impact**: one row per (incident, affected shipment): reason, estimated delay, affected value, estimated fill rate,
  arrival estimate, days late, late and fill penalties, gross, waived flag, net, clauses used, `params_unverified`.
  **incident_exposure**: the per-incident roll-up and the force-majeure notify deadline.
- **customer_notification**: who was told about which incident and when (the MSA-14.3 evidence). **eta_adjustment**: delay already
  written into a shipment's ETA by an action, so it is not counted twice.
- **agent_run** and **agent_step**: every triage run with model, timing, tokens, outcome (`ok` or `fallback`), honesty failures,
  and each tool step. **playbook**: the proposed actions (type, params, goods value, cost, saving, clause, status, who requested,
  approved and executed, idempotency key, result). **stock_transfer**: stock moved by an executed playbook.
- **outbox**: messages waiting for the chat webhook (`pending`, `sent`, `failed`, attempts).
- The scenario reset (`reset_operational`) also clears these operational tables (through the shipment and incident cascades and `outbox`);
  contracts, chunks, capacity and the audit log stay.
