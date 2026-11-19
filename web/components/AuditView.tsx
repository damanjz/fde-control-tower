"use client";
// The audit trail as a readable timeline: who did what, when, and the money involved. GET /audit, scoped per role.
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useClock } from "./Frame";
import { api } from "@/lib/client";
import { group, hhmm, rs, stamp } from "@/lib/format";
import type { RoleId } from "@/lib/roles";
import type { AuditRow, Network } from "@/lib/types";
import { auditLine, isPersonAction, who } from "@/lib/words";

export type AuditInitial = { role: RoleId; network: Network | null; rows: AuditRow[]; total: number; ok: boolean };

const dayOf = (iso: string) => stamp(Date.parse(iso)).split(",")[0];

/** A small drawn mark per kind of event, so the eye can scan the log. */
type Kind = "paused" | "recovered" | "read" | "asked" | "approved" | "notified" | "executed" | "replayed" | "discarded" | "drill" | "start" | "other";
function kindOf(a: AuditRow): Kind {
  const d = a.detail as Record<string, unknown>;
  if (a.action === "carrier.circuit") return String(d.to) === "open" ? "paused" : "recovered";
  if (/file|files_processed|ingested/.test(a.action)) return "read";
  if (a.action === "playbook.request_approval") return "asked";
  if (a.action === "playbook.approve") return "approved";
  if (a.action === "playbook.execute") return d.action_type === "notify_customers" ? "notified" : "executed";
  if (a.action === "dead_letter.replay" || a.action === "dead_letter.replay_failed") return "replayed";
  if (a.action === "dead_letter.discard") return "discarded";
  if (a.action === "sim.outage") return "drill";
  if (a.action === "scenario.start") return "start";
  return "other";
}
const KIND_WORD: Record<Kind, string> = {
  paused: "paused", recovered: "recovered", read: "read", asked: "asked for approval", approved: "approved", notified: "customers told",
  executed: "fix ran", replayed: "replayed", discarded: "discarded", drill: "outage drill", start: "morning started", other: "event",
};
const ic = (d: string) => <svg viewBox="0 0 16 16" aria-hidden="true"><path d={d} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>;
const KIND: Record<Kind, ReactNode> = {
  paused: ic("M6 3.5v9M10 3.5v9"),
  recovered: ic("M3 8.5l3.2 3L13 4.5"),
  read: ic("M4 2.5h5.5L12 5v8.5H4ZM9.5 2.5V5H12M6 8h4M6 10.5h4"),
  asked: ic("M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 0 0 0-11M8 5v3.2l2 1.3"),
  approved: ic("M8 2l5 2v4c0 3-2.2 5-5 6c-2.8-1-5-3-5-6V4ZM5.8 8.2l1.6 1.6l3-3.2"),
  notified: ic("M2.5 4.5h11v7h-11ZM2.5 4.5L8 9l5.5-4.5"),
  executed: ic("M5 3l7 5l-7 5Z"),
  replayed: ic("M3 8a5 5 0 1 0 1.6-3.7M3 3v3h3"),
  discarded: ic("M4 4l8 8M12 4l-8 8"),
  drill: ic("M9 2L4 9h4l-1 5l5-7H8Z"),
  start: ic("M4 14V2.5M4 3h7.5l-1.5 2.5l1.5 2.5H4"),
  other: ic("M8 7.5v.01"),
};

export default function AuditView({ initial }: { initial: AuditInitial }) {
  const { role, network } = initial;
  const { resync } = useClock();
  const [rows, setRows] = useState(initial.rows);
  const [total, setTotal] = useState(initial.total);
  const [all, setAll] = useState(() => !initial.rows.some(isPersonAction));   // nothing by a person yet: show everything

  const refresh = useCallback(async () => {
    const [a, l] = await Promise.all([api<{ total: number; items: AuditRow[] }>(role, "audit?limit=300&morning=true"), api<{ now: string }>(role, "live")]);
    if (a.data) { setRows(a.data.items); setTotal(a.data.total); }
    if (l.data) resync(l.data.now);
  }, [role, resync]);
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) void refresh(); }, 15_000);
    return () => clearInterval(t);
  }, [refresh]);

  const lines = rows.map((r) => ({ r, ...auditLine(r, network) }));
  const shown = all ? lines : lines.filter((x) => isPersonAction(x.r));
  const saved = lines.reduce((s, x) => s + x.saved, 0);
  const runs = rows.filter((r) => r.action === "playbook.execute").length;
  const approvals = rows.filter((r) => r.action === "playbook.approve").length;
  const records = rows.filter((r) => r.action.startsWith("dead_letter.")).length;

  return (
    <main className="page">
      <div className="sheet paper">
        <div className="sheet-in">
          <section className="head">
            <h1>Audit trail</h1>
            <p className="intro">This morning&apos;s actions by people and by the tower itself, newest first, with the money involved. Written once, never edited.</p>
          </section>
          <div className="filter">
            <div className="seg" role="radiogroup" aria-label="Show">
              <button type="button" role="radio" aria-checked={!all} onClick={() => setAll(false)}>People and fixes</button>
              <button type="button" role="radio" aria-checked={all} onClick={() => setAll(true)}>Everything</button>
            </div>
          </div>
          {!initial.ok && rows.length === 0 ? (
            <p className="section">The tower did not answer for the audit trail. Reload to try again.</p>
          ) : (
            <ol className="log">
              {shown.map(({ r, text, money, saved: s }, i) => {
                const day = dayOf(r.at), head = i === 0 || day !== dayOf(shown[i - 1].r.at);
                return (
                  <li key={r.id}>
                    {head && <span className="day">{day}</span>}
                    <time dateTime={r.at}>{hhmm(r.at)}</time>
                    <span className="kind" title={KIND_WORD[kindOf(r)]}>{KIND[kindOf(r)]}<span className="sr">{KIND_WORD[kindOf(r)]}</span></span>
                    <span className="who">{i === 0 || head || shown[i - 1].r.actor !== r.actor ? who(r.actor) : ""}</span>
                    <span className="say2">{text}</span>
                    <span className={`money ${s > 0 ? "saved" : ""}`}>{money ?? ""}</span>
                  </li>
                );
              })}
              {shown.length === 0 && <li><span className="empty">Nothing by a person yet this morning.</span></li>}
            </ol>
          )}
        </div>
      </div>

      <aside className={`card ${saved > 0 ? "won" : ""}`} aria-label="Summary">
        <span className="lbl">Avoided by approved fixes</span>
        <span className={`fig ${saved > 0 ? "" : "plain"}`}>{rs(saved)}</span>
        <ul className="facts-list">
          <li><span>Fixes run</span><b>{group(runs)}</b></li>
          <li><span>Large moves approved by Asha</span><b>{group(approvals)}</b></li>
          <li><span>Held-back records replayed or discarded</span><b>{group(records)}</b></li>
          <li><span>Entries this morning</span><b>{group(rows.length)} of {group(total)}</b></li>
        </ul>
        <p className="why">The figures add up this morning&apos;s entries; each fix records the exposure before and after it ran.</p>
      </aside>
    </main>
  );
}
