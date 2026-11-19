// Shapes of the tower's JSON, as the UI reads them (see tower/routes.py and tower/scoped.py).
export type Me = {
  id: string; name: string; role: string; scope: "all India" | { dcs: string[] };
  can_execute: boolean; can_approve: boolean; approval_threshold_inr: number;
  field_access: { driver_contact: string; carrier_contact: string; carrier_bank: string; contracts: string };
};
export type Warehouse = { id: string; city: string; lat: number; lon: number };
export type Zone = { id: string; city: string; state: string; lat: number; lon: number; serving_dc: string; road_km: number; std_transit_days: number };
export type Carrier = { id: string; name: string; dispatch_phone?: string | null; bank_account?: string | null };
export type Supplier = { id: string; name: string; lat: number; lon: number; state: string };
export type Network = { warehouses: Warehouse[]; zones: Zone[]; carriers: Carrier[]; suppliers: Supplier[] };
export type Plan = { incident_id: number; id: number; title: string; action_type: string; executed_by: string; approved_by: string | null; executed_at: string };
export type Live = {
  now: string; exposure_inr: number; plans: Record<string, Plan>;
  circuits: Record<string, string>; dead_letters_waiting: number | null;
};
export type Incident = {
  id: number; source: string; kind: string; title: string; detail: string; lat: number | null; lon: number | null; radius_km: number | null;
  dc_id: string | null; carrier_id: string | null; supplier_id: string | null; states: string[] | null; severity: string;
  started_at: string; status: string; shipments: number; value_inr: number; gross_inr: number; net_inr: number;
  waivable_inr: number; waived_inr: number; notify_deadline: string | null; rank: number; unverified_rows: number;
};
export type ChainItem = { id: string; label: string; shipments: number; value_inr: number };
export type Step = { seq: number; tool: string; args: Record<string, unknown>; result_summary: string; ms: number };
export type AgentRun = {
  id: number; user_id: string; role: string; model: string; started_at: string; finished_at: string | null; ms: number;
  prompt_tokens: number; completion_tokens: number; llm_calls: number; outcome: string; fallback_reason: string | null;
  honesty_failures: string[]; summary: string | null; steps: Step[];
};
export type PlaybookResult = { exposure_before_inr?: number | null; exposure_after_inr?: number | null; changed?: number };
export type Playbook = {
  id: number; incident_id: number; run_id: number | null; role: string; action_type: string; title: string;
  params: Record<string, unknown>; shipment_ids: string[]; dc_scope: string[];
  goods_value_inr: number; est_cost_inr: number; est_saving_inr: number; clause_id: string | null; clause_chain: string | null;
  rationale: string; source: string; status: string; requested_by: string | null; approved_by: string | null;
  executed_by: string | null; executed_at: string | null; result: PlaybookResult | null;
};
/** What POST /playbooks/{id}/run and /approve return: the playbook's state, and the result once it ran. */
export type RunReply = {
  id: number; incident_id: number; action_type: string; status: string; goods_value_inr: number; requested_by: string | null;
  approved_by: string | null; executed_by: string | null; executed_at: string | null; result?: PlaybookResult | null; message?: string;
};
export type Clause = { chain: string; clause_id: string; heading: string; page: number; text: string; verified: boolean };
export type TopCustomer = { customer: string; chain: string; shipments: number; net_inr: number; gross_inr: number };
export type Money = {
  shipments: number; value_inr: number; late_inr: number; fill_inr: number; gross_inr: number; net_inr: number; waived_inr: number;
  waivable_inr: number; notify_deadline: string | null; max_days_late: number | null; unverified_rows: number;
  top_customers: TopCustomer[]; hours_left_to_notify: number | null;
  unverified_sources?: { chain: string; clause_id: string; dead_letter_id: number | null }[];
};
export type Brief = {
  incident: Incident;
  money: Money;
  chain: { source: { kind: string; title: string; severity: string }; warehouses: ChainItem[]; carriers: ChainItem[]; cities: ChainItem[]; customers: ChainItem[] };
  agent: AgentRun | null; clauses: Clause[]; playbooks: Playbook[];
  shipments: { total: number; items: BriefShipment[] };
};
/** An affected shipment in the brief: the impact row and the shipment, driver fields as the role may see them
 *  (masked for Kiran, removed entirely for Meera). */
export type BriefShipment = {
  id: string; customer: string; chain: string; dc_id: string; zone_id: string; zone_city: string; carrier_id: string;
  value_inr: number; days_late: number; net_penalty_inr: number; gross_penalty_inr: number; waived: boolean; clauses: string[];
  last_lat: number | null; last_lon: number | null; driver_name?: string | null; driver_phone?: string | null;
};
export type Outage = {
  active: boolean; simulator_up: boolean; error_rate: number; can_toggle: boolean; since: string | null; by: string | null;
  erp_file: string | null; erp_rows: number; carrier_polls: number; carrier_failed_polls: number; erp_rejected: number;
};
export type Feed = {
  feed: string; received: number; accepted: number; rejected: number; last_at: string | null; last_error: string | null;
  circuit_state: string; counters: Record<string, number>; accept_rate: number | null; status: string;
};
export type DeadLetter = {
  id: number; feed: string; received_at: string; payload: Record<string, unknown>; reason_code: string; reason: string;
  suggested_fix: string | null; suggested_patch: Record<string, unknown> | null; status: string; attempts: number;
};
export type DeadLetters = { total: number; by_reason: { reason_code: string; n: number }[]; items: DeadLetter[] };
export type AuditRow = { id: number; at: string; actor: string; action: string; target: string; detail: Record<string, unknown>; dc_ids: string[] | null };

/** A handled incident: the executed playbook, as the strip and the rail show it. */
export type Done = { pb: Playbook; saved: number };
