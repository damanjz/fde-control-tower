-- Tarangini Distribution control tower: ontology + ingestion bookkeeping.
-- Idempotent: safe to run on first start (compose init) and again from `python -m tower.seed`.

CREATE EXTENSION IF NOT EXISTS vector;  -- enabled for later milestones, unused so far

-- ---------------------------------------------------------------- network (reference data)
CREATE TABLE IF NOT EXISTS warehouse (
    id   text PRIMARY KEY,                 -- DC-HYD
    city text NOT NULL,
    lat  double precision NOT NULL,
    lon  double precision NOT NULL
);

CREATE TABLE IF NOT EXISTS zone (
    id                text PRIMARY KEY,    -- Z01
    city              text NOT NULL UNIQUE,
    state             text NOT NULL,
    lat               double precision NOT NULL,
    lon               double precision NOT NULL,
    serving_dc        text NOT NULL REFERENCES warehouse(id),
    road_km           numeric(8,1) NOT NULL,
    std_transit_days  int NOT NULL
);
CREATE INDEX IF NOT EXISTS zone_dc_idx ON zone (serving_dc);

CREATE TABLE IF NOT EXISTS carrier (
    id             text PRIMARY KEY,       -- C1
    name           text NOT NULL UNIQUE,
    dispatch_phone text NOT NULL,          -- PII-like: masked by role in a later milestone
    bank_account   text NOT NULL           -- PII-like: masked by role in a later milestone
);

CREATE TABLE IF NOT EXISTS supplier (
    id                text PRIMARY KEY,    -- S001
    name              text NOT NULL,
    category          text NOT NULL,
    hub               text NOT NULL,
    state             text NOT NULL,
    lat               double precision NOT NULL,
    lon               double precision NOT NULL,
    quoted_lead_days  int NOT NULL
);

CREATE TABLE IF NOT EXISTS product (
    sku          text PRIMARY KEY,         -- PE-001
    category     text NOT NULL,
    supplier_id  text NOT NULL REFERENCES supplier(id),
    unit_cost    numeric(10,2) NOT NULL,
    unit_price   numeric(10,2) NOT NULL,
    case_qty     int NOT NULL
);
CREATE INDEX IF NOT EXISTS product_supplier_idx ON product (supplier_id);

CREATE TABLE IF NOT EXISTS customer (
    id       serial PRIMARY KEY,
    chain    text NOT NULL,
    zone_id  text NOT NULL REFERENCES zone(id),
    name     text NOT NULL UNIQUE,         -- "Ratnadeep (Hyderabad)"
    UNIQUE (chain, zone_id)
);

CREATE TABLE IF NOT EXISTS contract (
    id             serial PRIMARY KEY,
    customer_id    int NOT NULL UNIQUE REFERENCES customer(id),
    contract_no    text NOT NULL UNIQUE,
    effective_from date NOT NULL,
    effective_to   date NOT NULL
);

CREATE TABLE IF NOT EXISTS contract_clause (
    id           serial PRIMARY KEY,
    contract_id  int NOT NULL REFERENCES contract(id) ON DELETE CASCADE,
    clause_id    text NOT NULL,            -- MSA-7.2
    kind         text NOT NULL,            -- late_delivery | fill_rate | force_majeure
    text         text NOT NULL,
    params       jsonb NOT NULL DEFAULT '{}',
    UNIQUE (contract_id, clause_id)
);

-- ---------------------------------------------------------------- operational data
CREATE TABLE IF NOT EXISTS shipment (
    id                  text PRIMARY KEY,  -- natural key from the ERP: SH-4812
    order_no            text NOT NULL,
    dc_id               text NOT NULL REFERENCES warehouse(id),
    zone_id             text NOT NULL REFERENCES zone(id),
    carrier_id          text NOT NULL REFERENCES carrier(id),
    customer_id         int  NOT NULL REFERENCES customer(id),
    cases               int  NOT NULL CHECK (cases > 0),
    value_inr           numeric(14,2) NOT NULL CHECK (value_inr >= 0),
    status              text NOT NULL CHECK (status IN ('planned','in_transit','held','delivered')),
    departed_at         timestamptz NOT NULL,
    eta                 timestamptz NOT NULL,
    promised_at         timestamptz NOT NULL,
    last_lat            double precision,
    last_lon            double precision,
    last_position_at    timestamptz,
    carrier_status      text,              -- what the carrier API last said
    carrier_status_at   timestamptz,
    carrier_eta         timestamptz,
    source              text NOT NULL,     -- erp | replay
    version             int NOT NULL DEFAULT 1,
    content_hash        text NOT NULL,
    source_exported_at  timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS shipment_dc_idx       ON shipment (dc_id);
CREATE INDEX IF NOT EXISTS shipment_zone_idx     ON shipment (zone_id);
CREATE INDEX IF NOT EXISTS shipment_carrier_idx  ON shipment (carrier_id);
CREATE INDEX IF NOT EXISTS shipment_customer_idx ON shipment (customer_id);
CREATE INDEX IF NOT EXISTS shipment_status_idx   ON shipment (status);
CREATE INDEX IF NOT EXISTS shipment_eta_idx      ON shipment (eta);

CREATE TABLE IF NOT EXISTS shipment_line (
    id           bigserial PRIMARY KEY,
    shipment_id  text NOT NULL REFERENCES shipment(id) ON DELETE CASCADE,
    sku          text NOT NULL REFERENCES product(sku),
    cases        int NOT NULL CHECK (cases > 0),
    value_inr    numeric(14,2) NOT NULL,
    UNIQUE (shipment_id, sku)
);
CREATE INDEX IF NOT EXISTS shipment_line_sku_idx ON shipment_line (sku);

-- Pings can arrive before the ERP row for their shipment, so shipment_id is a soft reference.
CREATE TABLE IF NOT EXISTS gps_ping (
    ping_id      text PRIMARY KEY,
    shipment_id  text NOT NULL,
    vehicle      text,
    lat          double precision NOT NULL,
    lon          double precision NOT NULL,
    speed_kmph   real,
    event_ts     timestamptz NOT NULL,
    received_at  timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS gps_ping_ship_idx ON gps_ping (shipment_id, event_ts DESC);

CREATE TABLE IF NOT EXISTS disruption_event (
    id           bigserial PRIMARY KEY,
    source       text NOT NULL,            -- imd | carrier_notice | ops | ...
    external_id  text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('weather','facility','carrier','supplier')),
    title        text NOT NULL,
    detail       text NOT NULL,
    lat          double precision,
    lon          double precision,
    radius_km    double precision,
    dc_id        text REFERENCES warehouse(id),
    carrier_id   text REFERENCES carrier(id),
    supplier_id  text REFERENCES supplier(id),
    states       text[],
    severity     text NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    started_at   timestamptz NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active','resolved')),
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS disruption_started_idx ON disruption_event (started_at DESC);

-- ---------------------------------------------------------------- ingestion bookkeeping
CREATE TABLE IF NOT EXISTS feed_health (
    feed           text PRIMARY KEY,       -- erp | gps | alerts | carrier:C1 ...
    received       bigint NOT NULL DEFAULT 0,
    accepted       bigint NOT NULL DEFAULT 0,
    rejected       bigint NOT NULL DEFAULT 0,
    last_at        timestamptz,
    last_error     text,
    circuit_state  text NOT NULL DEFAULT 'closed' CHECK (circuit_state IN ('closed','open','half_open')),
    counters       jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dead_letter (
    id               bigserial PRIMARY KEY,
    feed             text NOT NULL,
    received_at      timestamptz NOT NULL DEFAULT now(),
    payload          jsonb NOT NULL,
    payload_hash     text NOT NULL,
    reason_code      text NOT NULL,
    reason           text NOT NULL,
    suggested_fix    text,
    suggested_patch  jsonb,                -- fields to merge into payload when the fix is applied
    status           text NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting','replayed','discarded')),
    replayed_at      timestamptz,
    attempts         int NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS dead_letter_status_idx ON dead_letter (status, received_at DESC);
-- One entry per distinct bad payload, whatever its status: the same bad row in a later export (or a reprocessed
-- file) never piles up, and a payload already replayed or discarded is not resurrected.
DROP INDEX IF EXISTS dead_letter_waiting_uq;
CREATE UNIQUE INDEX IF NOT EXISTS dead_letter_payload_uq ON dead_letter (feed, payload_hash);

CREATE TABLE IF NOT EXISTS ingest_log (
    feed        text NOT NULL,
    idem_key    text NOT NULL,             -- file sha256 | ping id | alert id
    first_seen  timestamptz NOT NULL DEFAULT now(),
    meta        jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (feed, idem_key)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      bigserial PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    actor   text NOT NULL,
    action  text NOT NULL,
    target  text NOT NULL,
    detail  jsonb NOT NULL DEFAULT '{}'
);

CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (% rejected)', TG_OP USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();
