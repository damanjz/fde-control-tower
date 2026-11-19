-- Milestone 2: contracts (PDF), bills of lading, impact and exposure, agent runs, playbooks, outbox.
-- Idempotent. Applied after db/schema.sql by `python -m tower.migrate` (also by tower.db.apply_schema).

-- ---------------------------------------------------------------- audit scope
-- Which DCs an action touched; NULL means network-wide. A dispatcher sees rows that touch his DC or that he wrote.
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS dc_ids text[];

-- ---------------------------------------------------------------- bill of lading data on the shipment (PII columns)
ALTER TABLE shipment ADD COLUMN IF NOT EXISTS bol_no          text;
ALTER TABLE shipment ADD COLUMN IF NOT EXISTS vehicle_no      text;
ALTER TABLE shipment ADD COLUMN IF NOT EXISTS driver_name     text;   -- PII
ALTER TABLE shipment ADD COLUMN IF NOT EXISTS driver_phone    text;   -- PII
ALTER TABLE shipment ADD COLUMN IF NOT EXISTS bol_received_at timestamptz;

-- ---------------------------------------------------------------- master services agreements (one PDF per chain)
CREATE TABLE IF NOT EXISTS msa_document (
    id           serial PRIMARY KEY,
    chain        text NOT NULL UNIQUE,
    file         text NOT NULL,
    sha256       text NOT NULL,
    template     text NOT NULL,
    pages        int  NOT NULL,
    ingested_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE contract ADD COLUMN IF NOT EXISTS msa_id int REFERENCES msa_document(id);

CREATE TABLE IF NOT EXISTS contract_chunk (
    id         bigserial PRIMARY KEY,
    msa_id     int NOT NULL REFERENCES msa_document(id) ON DELETE CASCADE,
    clause_id  text NOT NULL,                 -- MSA-7.2
    heading    text NOT NULL,
    page       int  NOT NULL,
    text       text NOT NULL,
    kind       text,                          -- late_delivery | fill_rate | force_majeure (money clauses only)
    params     jsonb NOT NULL DEFAULT '{}',   -- parsed from the PDF text
    verified   boolean,                       -- params equal the contract_clause seed? NULL for non-money clauses
    embedding  vector(768),
    tsv        tsvector GENERATED ALWAYS AS (to_tsvector('english', heading || ' ' || text)) STORED,
    UNIQUE (msa_id, clause_id)
);
CREATE INDEX IF NOT EXISTS contract_chunk_tsv_idx ON contract_chunk USING gin (tsv);
CREATE INDEX IF NOT EXISTS contract_chunk_vec_idx ON contract_chunk USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------- capacity and stock (options for the playbooks)
CREATE TABLE IF NOT EXISTS fleet_capacity (
    dc_id        text NOT NULL REFERENCES warehouse(id),
    carrier_id   text NOT NULL REFERENCES carrier(id),
    trucks_free  int  NOT NULL CHECK (trucks_free >= 0),
    PRIMARY KEY (dc_id, carrier_id)
);
CREATE TABLE IF NOT EXISTS dc_stock (
    dc_id  text NOT NULL REFERENCES warehouse(id),
    sku    text NOT NULL REFERENCES product(sku),
    cases  int  NOT NULL CHECK (cases >= 0),
    PRIMARY KEY (dc_id, sku)
);

-- ---------------------------------------------------------------- incident impact and money at risk
CREATE TABLE IF NOT EXISTS incident_impact (
    incident_id         bigint NOT NULL REFERENCES disruption_event(id) ON DELETE CASCADE,
    shipment_id         text   NOT NULL REFERENCES shipment(id) ON DELETE CASCADE,
    reason              text   NOT NULL,
    est_delay_days      numeric(5,2) NOT NULL,
    affected_value_inr  numeric(14,2) NOT NULL,   -- whole shipment, or the affected SKU lines (supplier incidents)
    est_fill_rate       numeric(4,3),             -- supplier incidents only
    arrival_estimate    timestamptz NOT NULL,
    days_late           int NOT NULL DEFAULT 0,
    late_penalty_inr    numeric(14,2) NOT NULL DEFAULT 0,
    fill_penalty_inr    numeric(14,2) NOT NULL DEFAULT 0,
    gross_penalty_inr   numeric(14,2) NOT NULL DEFAULT 0,
    waived              boolean NOT NULL DEFAULT false,
    net_penalty_inr     numeric(14,2) NOT NULL DEFAULT 0,
    clauses             text[] NOT NULL DEFAULT '{}',
    params_unverified   boolean NOT NULL DEFAULT false,   -- the PDF disagreed with the seeded clause params
    computed_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (incident_id, shipment_id)
);
CREATE INDEX IF NOT EXISTS incident_impact_ship_idx ON incident_impact (shipment_id);

CREATE TABLE IF NOT EXISTS incident_exposure (
    incident_id      bigint PRIMARY KEY REFERENCES disruption_event(id) ON DELETE CASCADE,
    shipments        int NOT NULL,
    value_inr        numeric(16,2) NOT NULL,
    gross_inr        numeric(16,2) NOT NULL,
    waivable_inr     numeric(16,2) NOT NULL,
    net_inr          numeric(16,2) NOT NULL,
    notify_deadline  timestamptz,
    notes            jsonb NOT NULL DEFAULT '{}',
    computed_at      timestamptz NOT NULL DEFAULT now()
);

-- Delay already written into shipment.eta by an update_eta / reroute / swap action, so it is not counted twice.
CREATE TABLE IF NOT EXISTS eta_adjustment (
    shipment_id  text   NOT NULL REFERENCES shipment(id) ON DELETE CASCADE,
    incident_id  bigint NOT NULL REFERENCES disruption_event(id) ON DELETE CASCADE,
    days         numeric(5,2) NOT NULL,
    playbook_id  bigint,
    PRIMARY KEY (shipment_id, incident_id)
);

CREATE TABLE IF NOT EXISTS customer_notification (
    id           bigserial PRIMARY KEY,
    incident_id  bigint NOT NULL REFERENCES disruption_event(id) ON DELETE CASCADE,
    customer_id  int    NOT NULL REFERENCES customer(id),
    shipment_ids text[] NOT NULL DEFAULT '{}',
    notified_at  timestamptz NOT NULL,
    playbook_id  bigint,
    channel      text NOT NULL DEFAULT 'chat_webhook',
    UNIQUE (incident_id, customer_id)
);

-- ---------------------------------------------------------------- agent runs
CREATE TABLE IF NOT EXISTS agent_run (
    id                bigserial PRIMARY KEY,
    incident_id       bigint NOT NULL REFERENCES disruption_event(id) ON DELETE CASCADE,
    user_id           text NOT NULL,
    role              text NOT NULL,
    model             text NOT NULL,
    started_at        timestamptz NOT NULL,
    finished_at       timestamptz,
    ms                int,
    prompt_tokens     int NOT NULL DEFAULT 0,
    completion_tokens int NOT NULL DEFAULT 0,
    llm_calls         int NOT NULL DEFAULT 0,
    outcome           text NOT NULL DEFAULT 'running' CHECK (outcome IN ('running','ok','fallback','error')),
    fallback_reason   text,
    honesty_failures  jsonb NOT NULL DEFAULT '[]',
    summary           text
);
CREATE INDEX IF NOT EXISTS agent_run_lookup_idx ON agent_run (incident_id, role, id DESC);

CREATE TABLE IF NOT EXISTS agent_step (
    id              bigserial PRIMARY KEY,
    run_id          bigint NOT NULL REFERENCES agent_run(id) ON DELETE CASCADE,
    seq             int NOT NULL,
    tool            text NOT NULL,
    args            jsonb NOT NULL DEFAULT '{}',
    result_summary  text NOT NULL DEFAULT '',
    ms              int NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS agent_step_run_idx ON agent_step (run_id, seq);

-- ---------------------------------------------------------------- playbooks and actions
CREATE TABLE IF NOT EXISTS playbook (
    id               bigserial PRIMARY KEY,
    incident_id      bigint NOT NULL REFERENCES disruption_event(id) ON DELETE CASCADE,
    run_id           bigint REFERENCES agent_run(id) ON DELETE SET NULL,
    role             text NOT NULL,
    action_type      text NOT NULL CHECK (action_type IN
                        ('notify_customers','reroute_via_dc','swap_carrier','stock_transfer','update_eta')),
    title            text NOT NULL,
    params           jsonb NOT NULL DEFAULT '{}',
    shipment_ids     text[] NOT NULL DEFAULT '{}',
    dc_scope         text[] NOT NULL DEFAULT '{}',   -- DCs the shipments leave from: who may act on it
    goods_value_inr  numeric(16,2) NOT NULL DEFAULT 0,
    est_cost_inr     numeric(16,2) NOT NULL DEFAULT 0,
    est_saving_inr   numeric(16,2) NOT NULL DEFAULT 0,
    clause_id        text,
    clause_chain     text,
    clause_quote     text,
    rationale        text NOT NULL DEFAULT '',
    source           text NOT NULL DEFAULT 'agent',  -- agent | fallback
    status           text NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed','pending_approval','executed','failed','rejected')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    requested_by     text,
    approved_by      text,
    executed_by      text,
    executed_at      timestamptz,
    idempotency_key  text UNIQUE,
    result           jsonb
);
CREATE INDEX IF NOT EXISTS playbook_incident_idx ON playbook (incident_id, id DESC);

CREATE TABLE IF NOT EXISTS stock_transfer (
    id           bigserial PRIMARY KEY,
    playbook_id  bigint NOT NULL REFERENCES playbook(id) ON DELETE CASCADE,
    from_dc      text NOT NULL REFERENCES warehouse(id),
    to_dc        text NOT NULL REFERENCES warehouse(id),
    sku          text NOT NULL REFERENCES product(sku),
    cases        int  NOT NULL CHECK (cases > 0),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outbox (
    id           bigserial PRIMARY KEY,
    topic        text NOT NULL,                  -- chat_webhook
    payload      jsonb NOT NULL,
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
    attempts     int NOT NULL DEFAULT 0,
    last_error   text,
    playbook_id  bigint,
    created_at   timestamptz NOT NULL DEFAULT now(),
    sent_at      timestamptz
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox (status, id);
