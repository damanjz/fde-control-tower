"use client";
// The three page views (.design/command.html: incidents(), health(), auditView()), on real endpoints.
import { cap, count, hhmm, inr, pad2 } from "@/lib/format";
import { roleOf, type RoleId } from "@/lib/roles";
import type { AuditRow, Carrier, DeadLetter, Feed, Incident, Outage, Plan } from "@/lib/types";
import { DL_FEED, auditLine, feedName, payloadLine, replayable, who } from "@/lib/words";

export function IncidentsPage({ incidents, plans, onOpen }: { incidents: Incident[] | null; plans: Record<string, Plan>; onOpen: (id: number) => void }) {
  const ds = incidents ?? [];
  const max = Math.max(...ds.map((d) => d.net_inr), 1);
  const open = ds.filter((d) => d.status === "active").length;
  return (
    <>
      <h1>Incidents</h1>
      <p className="sub">{incidents ? `${open} open today, ranked by money at risk. Click one to see it on the map with its brief.` : "Loading."}</p>
      <div style={{ marginTop: 18 }}>
        {ds.map((d, i) => (
          <button className="it" key={d.id} data-open={d.id} onClick={() => onOpen(d.id)}>
            <i>{pad2(i + 1)}</i>
            <span><b>{d.title}</b><small>{cap(d.kind)} · {count(d.shipments, "shipment")} · {d.severity}{plans[d.id] ? " · plan run" : ""}</small></span>
            <span className="bar"><span style={{ width: `${(d.net_inr / max) * 100}%` }} /></span>
            <span className="when"><small>since {hhmm(d.started_at)}</small></span>
            <em className="num">{d.net_inr > 0 ? inr(d.net_inr) : "₹0"}</em>
          </button>
        ))}
      </div>
    </>
  );
}

function feedNote(f: Feed): string {
  const c = f.counters ?? {};
  if (f.feed.startsWith("carrier:")) {
    if (f.circuit_state !== "closed") return `circuit ${f.circuit_state.replace("_", " ")}, serving cached ETAs`;
    return `${count(c.failed_polls ?? f.rejected, "failed poll")}, circuit opened ${count(c.to_open ?? 0, "time")}`;
  }
  if (f.feed === "erp") return `${count(c.dead_letters ?? f.rejected, "row")} to dead letters, ${count(c.duplicates ?? 0, "duplicate")} suppressed`;
  if (f.feed === "gps") return `${count(c.reordered ?? 0, "out-of-order ping")} re-sorted, ${count(c.bad_signature ?? 0, "forged request")} refused`;
  if (f.feed === "bol") return `${count(f.rejected, "PDF")} to review`;
  if (f.feed === "alerts") return `${count(c.duplicates ?? 0, "repeat")} ignored`;
  return f.last_error ?? "";
}

export type DlState = Record<number, { busy?: boolean; error?: string }>;

export function HealthPage(p: {
  feeds: Feed[] | null; dead: { status: number; data: { items: DeadLetter[] } | null } | null; waiting: number | null;
  outage: Outage | null; carriers: Carrier[]; role: RoleId; dl: DlState; onDead: (d: DeadLetter, action: "replay" | "discard") => void;
}) {
  // the concept's order: the order data arrives in a morning, ERP first
  const rank = (f: string) => ["erp", "gps", "alerts", "carrier:C1", "carrier:C2", "carrier:C3", "bol", "contracts"].indexOf(f) + 1 || 99;
  const feeds = [...(p.feeds ?? [])].sort((a, b) => rank(a.feed) - rank(b.feed));
  return (
    <>
      <h1>Data health</h1>
      <p className="sub">Every feed into the tower, how much of it got through, and the rows that did not. Fix a row and replay it; nothing is dropped silently.</p>
      <div className="grid">
        {feeds.map((f) => {
          const ok = f.accept_rate ?? 1;
          return (
            <div className="feed" key={f.feed}>
              <b>{feedName(f.feed, p.carriers)}</b>
              <div className={`pct num ${ok < 0.8 ? "b" : ok < 0.97 ? "w" : ""}`}>{f.accept_rate == null ? "no data" : `${(ok * 100).toFixed(1)}%`}</div>
              <span>{f.last_at ? `last ${hhmm(f.last_at)}` : "nothing yet"}{feedNote(f) ? ` · ${feedNote(f)}` : ""}</span>
            </div>
          );
        })}
      </div>
      <div className="dl">
        {p.dead?.status === 403 ? (
          <>
            <h2>Dead letters</h2>
            <p className="note">Dead letters hold raw rows from every DC, so only VP Operations can open them. The tower answered 403 for <b>{roleOf(p.role).name}</b>.</p>
          </>
        ) : (
          <>
            <h2>Dead letters · {p.waiting ?? "…"} waiting{p.outage?.active && p.outage.erp_rejected ? ` · ${p.outage.erp_rejected} from the malformed ERP file during the outage simulation` : ""}</h2>
            {(p.dead?.data?.items ?? []).map((d) => {
              const st = p.dl[d.id] ?? {}, done = d.status !== "waiting";
              const canReplay = replayable(d) && !st.error;
              const label = done ? (d.status === "replayed" ? "Replayed" : "Discarded") : st.busy ? "Working" : canReplay ? (d.suggested_patch ? "Fix and replay" : "Replay") : "Discard";
              return (
                <div className={`dlr ${done ? "ok" : ""}`} key={d.id}>
                  <span className="id">DL-{d.id} · {hhmm(d.received_at)}</span>
                  <b>{DL_FEED[d.feed] ?? d.feed}</b>
                  <code title={payloadLine(d)}>{payloadLine(d)}</code>
                  <span className={`fx ${st.error ? "err" : ""}`}>{st.error ?? `${d.reason} → ${d.suggested_fix ?? "no automatic fix"}`}</span>
                  <button disabled={done || st.busy} onClick={() => p.onDead(d, canReplay ? "replay" : "discard")}>{label}</button>
                </div>
              );
            })}
          </>
        )}
      </div>
    </>
  );
}

export function AuditPage({ rows, fresh, carriers, total }: { rows: AuditRow[] | null; fresh: Set<number>; carriers: Carrier[]; total: number | null }) {
  return (
    <>
      <h1>Audit trail</h1>
      <p className="sub">Every action by a person, the agent or the system, newest first. Written once, never edited.{total != null && rows ? ` Showing ${rows.length} of ${total.toLocaleString("en-IN")}.` : ""}</p>
      <div className="au2" style={{ marginTop: 16 }}>
        {(rows ?? []).map((a) => (
          <div className={`ar ${fresh.has(a.id) ? "new" : ""}`} key={a.id}>
            <time dateTime={a.at}>{hhmm(a.at)}</time><span>{who(a.actor)}</span><b>{auditLine(a, carriers)}</b>
          </div>
        ))}
      </div>
    </>
  );
}
