// Plain words for what the tower returns. Nothing internal reaches the surface: no ids, no action codes, no "playbook".
import { cityOf, hhmm, plural, rs, rsLimit, rsShort } from "./format";
import type { AgentRun, AuditRow, Carrier, DeadLetter, Feed, Network, Playbook } from "./types";

export const WHO: Record<string, string> = { asha: "Asha", kiran: "Kiran", meera: "Meera", system: "The tower", admin: "A script" };
export const who = (id: string | null | undefined) => (id ? WHO[id] ?? id : "");
/** The role names playbooks and agent runs are stored under. */
export const ROLE_PERSON: Record<string, string> = { vp_operations: "Asha", dispatcher_south: "Kiran", finance: "Meera" };

/** Strip labels: short enough for one line at 1440. Keyed by the incident's own title; anything else keeps its title. */
const SHORT: Record<string, string> = {
  "NH44 closed near Adilabad": "NH44 closed",
  "Kolkata DC dock 2 conveyor down": "Kolkata conveyor",
  "Ganga Freight drivers on strike in UP": "Ganga strike",
  "Cyclone watch on the Odisha coast": "Odisha cyclone",
  "Prakash Consumer Products inbound 5 days late": "Supplier late",
  "Dense fog on the Yamuna Expressway": "Yamuna fog",
};
export const shortTitle = (title: string) => SHORT[title] ?? title;

export const SOURCE: Record<string, string> = {
  imd: "An IMD weather alert", carrier_notice: "A carrier strike notice", dc_ops: "A warehouse floor report",
  supplier_edi: "The supplier's dispatch feed",
};

export const carrierName = (net: Network | null, id: string) => net?.carriers.find((c) => c.id === id)?.name ?? "another carrier";
export const dcName = (net: Network | null, id: string) => cityOf(net?.warehouses.find((w) => w.id === id)?.city ?? id.replace(/^DC-/, ""));

/** "section 14.3"; carrier codes as names. For the agent's own words. */
export function plain(s: string, net: Network | null): string {
  return s
    .replace(/^Agent fallback \([^)]*\)\.\s*/, "")
    .replace(/\bclause MSA-/g, "MSA-")
    .replace(/\bin your scope\b/g, "in view")
    .replace(/MSA-(\d+(\.\d+)?)/g, "section $1")
    .replace(/\bC([1-3])\b/g, (m) => carrierName(net, m))
    .replace(/\bDC-([A-Z]{3})\b/g, (_, c: string) => dcName(net, "DC-" + c))
    .replace(/\bDC\(s\)/g, "warehouses")
    .replace(/\bp(\d+)\b/g, "page $1")
    .replace(/\bplaybooks?\b/gi, (m) => (m.length > 8 ? "fixes" : "fix"))
    .replace(/\b(notify_customers|update_eta|swap_carrier|reroute_via_dc|stock_transfer)\b/g, (m) => ACTION[m] ?? m)
    .replace(/\bget_clause\b/g, "the contract search");
}

export const ACTION: Record<string, string> = {
  notify_customers: "telling customers", update_eta: "new delivery times", swap_carrier: "a carrier swap",
  reroute_via_dc: "a re-route", stock_transfer: "a stock transfer",
};

type Customers = { customers?: { customer: string }[] };
type Moves = { moves?: { to_carrier?: string }[]; to_carrier?: string; to_dc?: string; from_dc?: string };

/** The fix in a few words, as a person would say it. */
export function fixTitle(p: Playbook, net: Network | null): string {
  const pr = p.params as Customers & Moves;
  const n = p.shipment_ids?.length ?? 0;
  switch (p.action_type) {
    case "notify_customers": return `Tell the ${plural(pr.customers?.length ?? 0, "affected customer")} now`;
    case "update_eta": return `Send new delivery times for ${plural(n, "shipment")}`;
    case "swap_carrier": return `Move ${plural(n, "shipment")} to ${carrierName(net, pr.to_carrier ?? pr.moves?.[0]?.to_carrier ?? "")}`;
    case "reroute_via_dc": return `Send ${plural(n, "shipment")} from ${dcName(net, pr.to_dc ?? "")} instead`;
    case "stock_transfer": return `Move stock from ${dcName(net, pr.from_dc ?? "")} to cover the shortfall`;
    default: return "Proposed fix";
  }
}

/** What the fix did, after it ran: "4 customers told; section 14.3 waives the penalty." */
export function fixDone(p: Playbook, saved: number, net: Network | null): string {
  const pr = p.params as Customers & Moves;
  const n = p.shipment_ids?.length ?? 0;
  switch (p.action_type) {
    case "notify_customers":
      return `${plural(pr.customers?.length ?? 0, "customer")} told` + (saved > 0 && p.clause_id ? `; ${plain(p.clause_id, net)} waives the penalty.` : ".");
    case "swap_carrier":
      return `${plural(n, "shipment")} moved to ${carrierName(net, pr.to_carrier ?? pr.moves?.[0]?.to_carrier ?? "")} for ${rs(p.est_cost_inr)}.`;
    case "reroute_via_dc": return `${plural(n, "shipment")} now leave from ${dcName(net, pr.to_dc ?? "")}, for ${rs(p.est_cost_inr)}.`;
    case "stock_transfer": return `Stock moved from ${dcName(net, pr.from_dc ?? "")} for ${rs(p.est_cost_inr)}.`;
    default: return `New delivery times sent for ${plural(n, "shipment")}.`;
  }
}

/** One plain line of why, shown directly above Approve. Figures come from the priced fix, never from the model. */
export function whyLine(p: Playbook, netInr: number, deadline: string | null, net: Network | null): string {
  const pr = p.params as Customers & Moves;
  const n = p.shipment_ids?.length ?? 0;
  const all = p.est_saving_inr >= netInr - 1;
  switch (p.action_type) {
    case "notify_customers": {
      const c = plural(pr.customers?.length ?? 0, "customer");
      if (p.est_saving_inr <= 0) return `Telling the ${c} keeps them informed; it saves no penalty.`;
      return `Telling the ${c}${deadline ? ` before ${hhmm(deadline)}` : ""} waives ${all ? "the whole penalty" : rs(p.est_saving_inr) + " of the penalty"}${p.clause_id ? ` (${plain(p.clause_id, net)})` : ""}.`;
    }
    case "swap_carrier":
      return `Moving ${plural(n, "shipment")} to ${carrierName(net, pr.to_carrier ?? pr.moves?.[0]?.to_carrier ?? "")} avoids ${all ? "the whole penalty" : rs(p.est_saving_inr)} for ${rs(p.est_cost_inr)}.`;
    case "reroute_via_dc":
      return `Sending ${plural(n, "shipment")} from ${dcName(net, pr.to_dc ?? "")} avoids ${rs(p.est_saving_inr)} for ${rs(p.est_cost_inr)}.`;
    case "stock_transfer":
      return `Covering the shortfall from ${dcName(net, pr.from_dc ?? "")} avoids ${rs(p.est_saving_inr)} for ${rs(p.est_cost_inr)}.`;
    default:
      return "New delivery times keep customers informed; the penalty still applies.";
  }
}

/** What has happened in the world once a fix ran, so nobody expects an undo. */
export function cannotUndo(p: Playbook): string {
  switch (p.action_type) {
    case "notify_customers": return "Messages were sent; this cannot be undone.";
    case "update_eta": return "Customers have the new times; this cannot be undone.";
    case "swap_carrier": return "The new carrier is booked; this cannot be undone here.";
    default: return "The change is made; this cannot be undone here.";
  }
}

/** "Approved by Asha at 10:52." or "Asked by Kiran, approved by Asha at 10:58." */
export function doneBy(p: Playbook): string {
  const at = p.executed_at ? ` at ${hhmm(p.executed_at)}` : "";
  if (p.approved_by && p.requested_by && p.requested_by !== p.approved_by) return `Asked by ${who(p.requested_by)}, approved by ${who(p.approved_by)}${at}.`;
  return `Approved by ${who(p.approved_by ?? p.executed_by)}${at}.`;
}

/** Why the plan was chosen by rule, in a sentence. */
export function fallbackWhy(run: AgentRun): string {
  const f = run.honesty_failures ?? [];
  const r = run.fallback_reason ?? "";
  if (f.some((x) => /was not returned by get_clause/.test(x))) {
    return "The model's answer failed a check (it cited a clause it never fetched), so this plan was chosen by rule.";
  }
  if (/unavailable/.test(r)) return "The local model was not answering, so this plan was chosen by rule.";
  if (/honesty/.test(r) || f.length) return "The model's answer failed the checks twice, so this plan was chosen by rule.";
  if (/error/.test(r)) return "The run stopped on an error, so this plan was chosen by rule.";
  return "The model never proposed a valid plan, so this plan was chosen by rule.";
}

/** The agent's tools, as steps a person would describe. */
export const TOOL: Record<string, string> = {
  get_incident: "Read the incident", find_affected: "Found the shipments it touches", compute_exposure: "Asked code for the price",
  get_clause: "Searched the contracts", list_options: "Asked code which fixes it can price", propose_playbooks: "Proposed the plan",
};
export function stepLine(tool: string, summary: string, net: Network | null): [string, string] {
  if (tool === "propose_playbooks" && /^rejected/.test(summary)) return ["Proposed a plan; code sent it back", plain(summary.replace(/^rejected:\s*/, ""), net)];
  return [TOOL[tool] ?? "Used a tool", plain(summary, net)];
}

/** Which of the four stages each agent tool fills, and how a stage names it. */
export const STAGE_OF: Record<string, number> = { get_incident: 1, find_affected: 1, compute_exposure: 2, get_clause: 3, list_options: 4, propose_playbooks: 4 };
const STAGE_WORD: Record<string, string> = {
  get_incident: "read the incident", find_affected: "found the shipments", compute_exposure: "asked code for the price",
  get_clause: "searched the contracts", list_options: "listed the priced fixes", propose_playbooks: "proposed the plan",
};
const times = (n: number) => (n === 1 ? "" : n === 2 ? " twice" : ` ${n} times`);
/** "Searched the contracts twice" for the steps of one stage. */
export function stageWords(tools: string[]): string {
  const counts = new Map<string, number>();
  for (const t of tools) counts.set(t, (counts.get(t) ?? 0) + 1);
  const s = [...counts].map(([t, n]) => (STAGE_WORD[t] ?? "used a tool") + times(n)).join(", ");
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** What a quoted clause drives in the money, for the linked highlight. */
export function clauseDrives(clauseId: string): { what: string; key: "late" | "fill" | "waiver" | null } {
  if (clauseId === "MSA-7.2") return { what: "Drives the late penalties", key: "late" };
  if (clauseId === "MSA-9.1") return { what: "Drives the short-fill penalties", key: "fill" };
  if (clauseId === "MSA-14.3") return { what: "Drives the waiver", key: "waiver" };
  return { what: "", key: null };
}

/** The driver as this role may see them: masked for a dispatcher, removed for finance. */
export function driverLine(s: { driver_name?: string | null; driver_phone?: string | null }): { name: string; phone: string; hidden: boolean; line: string } {
  if (!("driver_name" in s) && !("driver_phone" in s)) return { name: "Removed for this role", phone: "Removed", hidden: true, line: "Driver details removed for this role" };
  if (!s.driver_name && !s.driver_phone) return { name: "Not on file yet", phone: "", hidden: true, line: "Driver not on file yet" };
  const name = s.driver_name ?? "Not on file yet", phone = s.driver_phone ?? "";
  return { name, phone, hidden: false, line: `Driver: ${name}${phone ? `, ${phone}` : ""}` };
}

// ------------------------------------------------------------------ data health
const FEED: Record<string, string> = {
  erp: "Orders from the ERP", gps: "Truck GPS pings", alerts: "Weather alerts from IMD", bol: "Bills of lading",
  contracts: "Customer contracts",
};
const FEED_HOW: Record<string, string> = {
  erp: "An hourly CSV export", gps: "A signed webhook from the trucks", alerts: "Polled every few seconds",
  bol: "PDFs dropped in an inbox", contracts: "One PDF per retail chain",
};
export function feedName(feed: string, carriers: Carrier[]): string {
  if (feed.startsWith("carrier:")) return `${carriers.find((c) => c.id === feed.slice(8))?.name ?? "Carrier"} status`;
  return FEED[feed] ?? feed;
}
export const feedHow = (feed: string) => (feed.startsWith("carrier:") ? "Polled from the carrier's API" : FEED_HOW[feed] ?? "");

/** Plain status of a feed, and whether it needs a look. */
export function feedStatus(f: Feed): { word: string; warn: boolean } {
  if (f.circuit_state !== "closed") return { word: "Not answering; using the last known times", warn: true };
  if (f.status === "warning") return { word: "Some records held back for a person", warn: true };
  return { word: "Healthy", warn: false };
}

export function feedNote(f: Feed): string {
  const c = f.counters ?? {};
  if (f.feed.startsWith("carrier:")) return `${plural(c.failed_polls ?? f.rejected, "failed call")}; paused itself ${plural(c.to_open ?? 0, "time")} and recovered`;
  if (f.feed === "erp") return `${plural(c.duplicates ?? 0, "repeated row")} ignored; ${plural(f.rejected, "row")} held back`;
  if (f.feed === "gps") return `${plural(c.reordered ?? 0, "late ping")} put back in order; ${plural(c.bad_signature ?? 0, "forged request")} refused`;
  if (f.feed === "bol") return `${plural(f.rejected, "PDF")} held back for a person`;
  if (f.feed === "alerts") return `${plural(c.duplicates ?? 0, "repeat")} ignored`;
  return "";
}

const DL_FEED: Record<string, string> = {
  erp: "An order row", gps: "A GPS ping", alerts: "A weather alert", bol: "A bill of lading", contracts: "A contract clause",
};
export const recordKind = (feed: string) => DL_FEED[feed] ?? "A record";

const REASON: Record<string, string> = {
  clock_skew_future: "GPS pings stamped in the future", unknown_zone: "Unknown city codes", missing_promised_at: "No promised date",
  malformed_row: "Broken rows", duplicate_conflict: "The same order twice, different values", bad_amount: "Unreadable amounts",
  unreadable_pdf: "Unreadable PDFs", value_mismatch: "Declared value does not match", param_mismatch: "Contract figure differs",
  unknown_shipment: "Shipment not in the ERP", cases_mismatch: "Case count does not match",
};
export const reasonGroup = (code: string) => REASON[code] ?? code.replaceAll("_", " ");

/** Contract and plain-word tidy-ups for the tower's reason text. */
export const reasonText = (s: string) => s.replace(/MSA-(\d+(\.\d+)?)/g, "section $1").replace(/\bthe zone table\b/g, "the city list").replace(/\bto Z\d+ /g, "to ");

/** The record as one short line: the raw text, or its first fields. */
export function recordLine(d: DeadLetter): string {
  const p = d.payload;
  if (typeof p.raw_line === "string") return p.raw_line;
  if (Array.isArray(p.headers)) return (p.headers as unknown[]).join(", ");
  if (d.feed === "contracts") return `${p.chain} ${reasonText(String(p.clause_id ?? ""))}: the PDF says ${JSON.stringify(p.pdf_params)}`;
  const vals = Object.entries(p).filter(([k]) => !k.startsWith("_")).map(([k, v]) => `${k} ${v === "" || v == null ? "(blank)" : typeof v === "object" ? JSON.stringify(v) : String(v)}`);
  return vals.slice(0, 7).join(", ");
}

/** Can the tower replay it at all? A cut-off line, a whole-file rejection and a contract mismatch cannot be. */
export function replayable(d: DeadLetter): string | null {
  if (d.feed === "contracts") return "A contract figure is fixed in the PDF or the contract master, then read again.";
  const keys = Object.keys(d.payload).filter((k) => !k.startsWith("_"));
  if (keys.length === 1 && keys[0] === "raw_line") return "The row is cut off, so there is nothing to replay.";
  if ("headers" in d.payload) return "The whole file was refused, so there is no single row to replay.";
  if (d.feed === "bol" && d.reason_code === "unreadable_pdf" && !d.suggested_patch) return "Nobody has typed it in yet, so there is nothing to replay.";
  return null;
}

// ------------------------------------------------------------------ audit
const num = (v: unknown) => (typeof v === "number" ? v : Number(v ?? 0));
const what = (d: Record<string, unknown>) => ACTION[String(d.action_type)] ?? "a fix";
const DID: Record<string, string> = {
  notify_customers: "Told the customers", update_eta: "Sent new delivery times", swap_carrier: "Moved shipments to another carrier",
  reroute_via_dc: "Re-routed shipments through another warehouse", stock_transfer: "Moved stock between warehouses",
};

/** One audit row as a sentence, and the money it moved or saved (if any). */
export function auditLine(a: AuditRow, net: Network | null): { text: string; money: string | null; saved: number } {
  const d = a.detail as Record<string, unknown>;
  switch (a.action) {
    case "playbook.execute": {
      const saved = d.exposure_before_inr != null ? num(d.exposure_before_inr) - num(d.exposure_after_inr) : 0;
      const by = d.approved_by ? `, approved by ${who(String(d.approved_by))}` : "";
      return { text: `${DID[String(d.action_type)] ?? "Ran a fix"}${d.changed != null ? ` (${plural(num(d.changed), "change")})` : ""}${by}`, money: saved > 0 ? `${rsShort(saved)} avoided` : null, saved: Math.max(0, saved) };
    }
    case "playbook.request_approval":
      return { text: `Asked for approval: ${what(d)} over the ${rsLimit(num(d.threshold_inr) || 500000)} limit`, money: `${rsShort(num(d.goods_value_inr))} of goods`, saved: 0 };
    case "playbook.approve":
      return { text: d.requested_by && d.requested_by !== a.actor ? `Approved ${who(String(d.requested_by))}'s request to move goods` : "Gave the second, final approval to move goods", money: `${rsShort(num(d.goods_value_inr))} of goods`, saved: 0 };
    case "carrier.circuit": {
      const c = carrierName(net, a.target.split(":")[1] ?? "");
      const to = String(d.to);
      const text = to === "open" ? `Paused calls to ${c} after repeated failures; using the last known times`
        : to === "half_open" ? `Tried ${c} again` : `${c} is answering again`;
      return { text, money: null, saved: 0 };
    }
    case "erp.file_processed":
      return { text: `Read an ERP export: ${plural(num(d.rows), "row")}, ${num(d.new)} new, ${num(d.updated)} updated, ${num(d.dead_letter)} held back`, money: null, saved: 0 };
    case "dead_letter.replay":
      return { text: `Replayed a held-back record (${(DL_FEED[String(d.feed)] ?? "a record").toLowerCase()})${d.used === "suggested_patch" ? " with the suggested fix" : ""}`, money: null, saved: 0 };
    case "dead_letter.replay_failed":
      return { text: `A replay failed the checks again: ${String(reasonGroup(String(d.reason_code))).toLowerCase()}`, money: null, saved: 0 };
    case "dead_letter.discard":
      return { text: `Discarded a held-back record (${(DL_FEED[String(d.feed)] ?? "a record").toLowerCase()})`, money: null, saved: 0 };
    case "contracts.ingested":
      return { text: `Read ${plural(num(d.files), "contract PDF")}; ${plural(num(d.mismatches), "figure")} ${num(d.mismatches) === 1 ? "differs" : "differ"} from the contract master`, money: null, saved: 0 };
    case "bol.files_processed":
      return { text: `Read bills of lading: ${num(d.applied)} applied, ${num(d.dead_letter)} held back`, money: null, saved: 0 };
    case "sim.outage":
      return { text: d.on ? `Started the outage drill: ${Math.round(num(d.error_rate) * 100)}% of carrier calls fail and a broken ERP file lands` : "Stopped the outage drill", money: null, saved: 0 };
    default:
      return { text: a.action.replaceAll(".", " ").replaceAll("_", " "), money: null, saved: 0 };
  }
}

export const isPersonAction = (a: AuditRow) => ["asha", "kiran", "meera"].includes(a.actor) || a.action.startsWith("playbook.");

