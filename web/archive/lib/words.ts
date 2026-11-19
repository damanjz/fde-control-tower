// Plain words for codes the tower returns: actors, actions, feeds, tools.
import { inr } from "./format";
import type { AuditRow, Carrier, DeadLetter } from "./types";

export const WHO: Record<string, string> = { asha: "Asha", kiran: "Kiran", meera: "Meera", system: "System", admin: "Admin" };
export const who = (id: string | null | undefined) => (id ? WHO[id] ?? id : "");

export const ACTION: Record<string, string> = {
  notify_customers: "notify customers", reroute_via_dc: "re-route via another DC", swap_carrier: "swap carrier",
  stock_transfer: "stock transfer", update_eta: "update ETAs",
};

/** What each agent tool does, and the concept's tag for it. */
export const TOOL: Record<string, [string, string]> = {
  get_incident: ["Read the incident", "graph"],
  find_affected: ["Found the affected shipments", "graph"],
  compute_exposure: ["Priced the exposure", "math"],
  get_clause: ["Searched the contracts", "contract"],
  list_options: ["Listed what can be done", "math"],
  propose_playbooks: ["Proposed playbooks", "plan"],
};

export function feedName(feed: string, carriers: Carrier[]): string {
  if (feed.startsWith("carrier:")) {
    const c = carriers.find((x) => x.id === feed.slice(8));
    return `Carrier status API · ${c ? c.name.split(" ")[0] : feed.slice(8)}`;
  }
  return ({ erp: "ERP order export (CSV, hourly)", gps: "GPS pings (webhook)", alerts: "Weather alerts (IMD feed)",
    bol: "Bills of lading (PDF inbox)", contracts: "Contracts (MSA PDFs)" } as Record<string, string>)[feed] ?? feed;
}

export const DL_FEED: Record<string, string> = {
  erp: "ERP order export", gps: "GPS pings", alerts: "Weather alerts", bol: "Bills of lading", contracts: "Contracts",
};

/** The bad row as a short line: the raw text, the file, or its first fields. */
export function payloadLine(d: DeadLetter): string {
  const p = d.payload;
  if (typeof p.raw_line === "string") return p.raw_line;
  if (Array.isArray(p.headers)) return `${p._file ?? "file"}: ${(p.headers as unknown[]).join(", ")}`;
  const vals = Object.entries(p).filter(([k]) => !k.startsWith("_")).map(([, v]) => (v === "" || v == null ? "(blank)" : typeof v === "object" ? JSON.stringify(v) : String(v)));
  return vals.slice(0, 6).join(", ");
}

/** Can the tower replay it at all? A cut-off line, a whole-file rejection and a contract mismatch cannot be. */
export function replayable(d: DeadLetter): boolean {
  if (d.feed === "contracts") return false;
  const keys = Object.keys(d.payload).filter((k) => !k.startsWith("_"));
  if (keys.length === 1 && keys[0] === "raw_line") return false;
  if ("headers" in d.payload) return false;
  if (d.feed === "bol" && d.reason_code === "unreadable_pdf" && !d.suggested_patch) return false;
  return true;
}

const num = (v: unknown) => (typeof v === "number" ? v : Number(v ?? 0));
const dlId = (target: string) => "DL-" + target.split(":")[1];
const pbNo = (target: string) => target.split(":")[1];

export function auditLine(a: AuditRow, carriers: Carrier[]): string {
  const d = a.detail as Record<string, unknown>;
  switch (a.action) {
    case "playbook.execute":
      return `Ran playbook ${pbNo(a.target)}: ${ACTION[String(d.action_type)] ?? d.action_type}${d.changed != null ? `, ${d.changed} changed` : ""}${d.approved_by ? `, approved by ${who(String(d.approved_by))}` : ""}`;
    case "playbook.request_approval":
      return `Asked for approval: playbook ${pbNo(a.target)}, ${ACTION[String(d.action_type)] ?? d.action_type}, moves ${inr(num(d.goods_value_inr))} of goods`;
    case "playbook.approve":
      return `Approved playbook ${pbNo(a.target)}: ${inr(num(d.goods_value_inr))} of goods`;
    case "carrier.circuit": {
      const c = carriers.find((x) => x.id === a.target.split(":")[1]);
      return `${c?.name ?? a.target} circuit ${String(d.from).replace("_", " ")} → ${String(d.to).replace("_", " ")}`;
    }
    case "erp.file_processed":
      return `Read ${a.target}: ${num(d.rows)} rows, ${num(d.new)} new, ${num(d.updated)} updated, ${num(d.dead_letter)} to dead letters`;
    case "dead_letter.replay":
      return `Replayed ${dlId(a.target)} (${DL_FEED[String(d.feed)] ?? d.feed})${d.used === "suggested_patch" ? " with the suggested fix" : ""}`;
    case "dead_letter.replay_failed":
      return `Replay of ${dlId(a.target)} failed: ${String(d.reason_code).replaceAll("_", " ")}`;
    case "dead_letter.discard":
      return `Discarded ${dlId(a.target)} (${DL_FEED[String(d.feed)] ?? d.feed})`;
    case "contracts.ingested":
      return `Read ${num(d.files)} contract PDFs; ${num(d.mismatches)} clause number mismatch sent to dead letters`;
    case "bol.files_processed":
      return `Read bills of lading: ${num(d.applied)} applied, ${num(d.dead_letter)} to dead letters`;
    case "sim.outage":
      return d.on ? `Started the outage simulation: ${Math.round(num(d.error_rate) * 100)}% of carrier API calls fail, ${d.erp_file} dropped` : "Stopped the outage simulation";
    default:
      return `${a.action} · ${a.target}`;
  }
}
