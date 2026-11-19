"use client";
// Data health, in the same world: the paper sheet holds every feed and the records that failed a check (what was wrong,
// the suggested fix, Replay and Discard); the rail on the indigo holds the summary and the Outage drill switch.
import { useCallback, useEffect, useState } from "react";
import { useClock } from "./Frame";
import { api } from "@/lib/client";
import { group, hhmm, plural } from "@/lib/format";
import { roleOf, type RoleId } from "@/lib/roles";
import type { DeadLetter, DeadLetters, Feed, Me, Network, Outage } from "@/lib/types";
import { feedHow, feedName, feedNote, feedStatus, reasonGroup, reasonText, recordKind, recordLine, replayable, who } from "@/lib/words";

export type HealthInitial = {
  role: RoleId; me: Me | null; network: Network | null; feeds: Feed[]; dead: { status: number; data: DeadLetters | null }; outage: Outage | null;
  record?: number | null;
};

const ORDER = ["erp", "gps", "alerts", "carrier:C1", "carrier:C2", "carrier:C3", "bol", "contracts"];
const rank = (f: string) => (ORDER.indexOf(f) + 1) || 99;
const SHOW = 3;

type RowState = { busy?: boolean; error?: string; done?: string };
// what a replay did (the API's `result`): only "applied" changed the data
const REPLAYED: Record<string, string> = { applied: "Replayed", unchanged: "Replayed, nothing to change", stale: "Not applied: newer data held" };

export default function HealthView({ initial }: { initial: HealthInitial }) {
  const { role, network } = initial;
  const person = roleOf(role).name;
  const { resync } = useClock();
  const [feeds, setFeeds] = useState(initial.feeds);
  const [dead, setDead] = useState(initial.dead);
  const [outage, setOutage] = useState(initial.outage);
  const [rows, setRows] = useState<Record<number, RowState>>({});
  const target = initial.record ?? null;
  const targetCode = target != null ? initial.dead.data?.items.find((x) => x.id === target)?.reason_code ?? null : null;
  const [openAll, setOpenAll] = useState<Record<string, boolean>>(targetCode ? { [targetCode]: true } : {});
  // a link from a case (?record=<id>) lands on that record: its group open, scrolled to, highlighted
  useEffect(() => {
    if (target == null) return;
    const t = setTimeout(() => document.getElementById(`rec-${target}`)?.scrollIntoView({ block: "center", behavior: "auto" }), 50);
    return () => clearTimeout(t);
  }, [target]);
  const [drill, setDrill] = useState<{ busy?: boolean; error?: string }>({});

  const refresh = useCallback(async () => {
    const [f, d, o] = await Promise.all([
      api<{ now: string; feeds: Feed[] }>(role, "health/feeds"), api<DeadLetters>(role, "dead-letters?status=waiting&limit=500"), api<Outage>(role, "outage"),
    ]);
    if (f.data) { setFeeds(f.data.feeds); resync(f.data.now); }
    setDead({ status: d.status, data: d.data });
    if (o.data) setOutage(o.data);
    setRows((r) => Object.fromEntries(Object.entries(r).filter(([, v]) => v.busy || v.error)));
  }, [role, resync]);
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) void refresh(); }, 10_000);
    return () => clearInterval(t);
  }, [refresh]);

  const onRecord = async (d: DeadLetter, action: "replay" | "discard") => {
    setRows((r) => ({ ...r, [d.id]: { busy: true } }));
    const res = await api<{ ok: boolean }>(role, `dead-letters/${d.id}/${action}`, { method: "POST" });
    const b = (res.body ?? {}) as { reason?: string; suggested_fix?: string; result?: string };
    const error = res.status === 200 ? undefined
      : res.status === 422 ? `The replay failed the checks again: ${reasonText(b.reason ?? "rejected")}. ${b.suggested_fix ? `Suggested: ${reasonText(b.suggested_fix)}` : "Discard it instead."}`
        : res.status === 403 ? `Only Asha can act on these records. Nothing was done.`
          : res.status === 404 ? "This record is gone. Reload the page."
            : res.status === 409 ? "Someone already handled this record. Reload the page to see the list as it is now."
              : "The tower is not answering. Nothing was done.";
    setRows((r) => ({ ...r, [d.id]: error ? { error } : { done: action === "replay" ? REPLAYED[b.result ?? "applied"] ?? "Replayed" : "Discarded" } }));
  };

  const toggleDrill = async () => {
    if (!outage) return;
    setDrill({ busy: true });
    const r = await api<Outage>(role, "outage", { method: "POST", body: { on: !outage.active } });
    if (r.data) { setOutage(r.data); setDrill({}); void refresh(); return; }
    setDrill({ error: r.status === 403 ? "Only Asha can start or stop the drill." : r.status === 503 ? "The simulator is not answering, so the drill cannot change." : "The tower is not answering. Nothing changed." });
  };

  const sorted = feeds.slice().sort((a, b) => rank(a.feed) - rank(b.feed));
  const healthy = sorted.filter((f) => !feedStatus(f).warn).length;
  const items = dead.data?.items ?? [];
  const groups = Object.entries(items.reduce<Record<string, DeadLetter[]>>((g, d) => { (g[d.reason_code] ??= []).push(d); return g; }, {}))
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
  const canAct = dead.status === 200;
  const carriers = network?.carriers ?? [];

  return (
    <main className="page">
      <div className="sheet paper">
        <div className="sheet-in">
          <section className="head">
            <h1>Data health</h1>
            <p className="intro">Every feed into the tower, how much of it got through, and the records that failed a check. Nothing is dropped quietly: a person replays or discards each one.</p>
          </section>

          <section className="section">
            <h2>Feeds</h2>
            <ul className="feedrows">
              <li className="fh" aria-hidden="true"><span>Feed</span><span>Status</span><span className="r">Accepted</span><span>Records</span></li>
              {sorted.map((f) => {
                const st = feedStatus(f);
                return (
                  <li key={f.feed}>
                    <span className="fn"><b>{feedName(f.feed, carriers)}</b><span>{feedHow(f.feed)}</span></span>
                    <span className={`st ${f.circuit_state !== "closed" ? "fail" : st.warn ? "warn" : ""}`}><i aria-hidden="true" />{st.word}</span>
                    <span className="pct num r">{f.accept_rate == null ? "Nothing yet" : `${(f.accept_rate * 100).toFixed(1)}%`}</span>
                    <span className="cnt"><span className="num">{group(f.accepted)} of {group(f.received)}{f.last_at ? `, last at ${hhmm(f.last_at)}` : ""}</span>{feedNote(f) && <span>{feedNote(f)}</span>}</span>
                  </li>
                );
              })}
            </ul>
          </section>

          <section className="section">
            <h2>Waiting for a person</h2>
            {canAct ? (
              <>
                <p>{items.length ? `${plural(items.length, "record")} failed a check and ${items.length === 1 ? "waits" : "wait"} here, grouped by what was wrong. Each has the tower's suggested fix.` : "Nothing is waiting: every record that failed a check has been handled."}</p>
                <div className="groups">
                  {groups.map(([code, ds], gi) => {
                    const all = openAll[code], list = all ? ds : ds.slice(0, SHOW);
                    return (
                      <details className="group" key={code} open={targetCode ? code === targetCode : gi === 0}>
                        <summary><span><svg className="chev" viewBox="0 0 10 10" aria-hidden="true"><path d="M3 1.5L7 5 3 8.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>{reasonGroup(code)}</span><span className="n">{plural(ds.length, "record")}</span></summary>
                        <ul className="recs">
                          {list.map((d) => {
                            const st = rows[d.id] ?? {}, why = replayable(d);
                            return (
                              <li className={`rec ${st.done ? "handled" : ""} ${d.id === target ? "target" : ""}`} key={d.id} id={`rec-${d.id}`}>
                                <div>
                                  <p className="what">{reasonText(d.reason)}</p>
                                  <p className="fix"><b>Suggested fix:</b> {d.suggested_fix ? reasonText(d.suggested_fix) : "none; a person decides."}</p>
                                  <p className="raw">{recordKind(d.feed)}, received {hhmm(d.received_at)}: {recordLine(d)}</p>
                                </div>
                                <div className="acts">
                                  {st.done ? <span className="state">{st.done}</span> : (
                                    <>
                                      {!why && <button type="button" className="btn secondary" disabled={st.busy} onClick={() => void onRecord(d, "replay")}>{d.suggested_patch ? "Replay with the fix" : "Replay"}</button>}
                                      <button type="button" className="btn secondary" disabled={st.busy} onClick={() => void onRecord(d, "discard")}>Discard</button>
                                    </>
                                  )}
                                </div>
                                {why && !st.done && <p className="note">{why}</p>}
                                {st.error && <p className="err" role="alert">{st.error}</p>}
                              </li>
                            );
                          })}
                        </ul>
                        {ds.length > SHOW && !all && (
                          <p className="more"><button type="button" className="quiet tog" aria-expanded="false" onClick={() => setOpenAll((o) => ({ ...o, [code]: true }))}><svg className="chev" viewBox="0 0 10 10" aria-hidden="true"><path d="M3 1.5L7 5 3 8.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>Show {plural(ds.length - SHOW, "more record")}</button></p>
                        )}
                      </details>
                    );
                  })}
                </div>
              </>
            ) : (
              <p>{dead.status === 403
                ? `These records hold raw shipment data from every warehouse, so only Asha can open them. The tower refused them for ${person}.`
                : "The tower did not answer for these records. Reload to try again."}</p>
            )}
          </section>
        </div>
      </div>

      <aside className="card" aria-label="Summary">
        {canAct ? (
          <>
            <span className="lbl">Waiting for a person</span>
            <span className="fig plain num">{group(items.length)}</span>
          </>
        ) : (
          <>
            <span className="lbl">Feeds healthy</span>
            <span className="fig plain num">{healthy} of {sorted.length}</span>
          </>
        )}
        <ul className="facts-list">
          {canAct && <li><span>Feeds healthy</span><b>{healthy} of {sorted.length}</b></li>}
          {canAct && groups.slice(0, 5).map(([code, ds]) => <li key={code}><span>{reasonGroup(code)}</span><b>{group(ds.length)}</b></li>)}
          {!canAct && <li><span>Records waiting</span><b>Asha only</b></li>}
        </ul>

        <div className="drill">
          <div className="drill-head">
            <span className="t" id="drill-label">Outage drill</span>
            <button type="button" role="switch" className="switch" aria-labelledby="drill-label" aria-checked={!!outage?.active}
              disabled={!outage?.can_toggle || !!drill.busy || !outage?.simulator_up} onClick={() => void toggleDrill()} />
          </div>
          <p className="why">Fails 30% of the carrier calls and drops a broken ERP file, to show the tower holding up: last known times, broken rows held back, nothing lost.</p>
          <p className={`gate ${outage?.can_toggle ? "" : "block"}`} aria-live="polite">
            {!outage ? "The tower did not report the drill's state."
              : outage.active
                ? `Running since ${outage.since ? hhmm(outage.since) : "now"}, started by ${who(outage.by) || "Asha"}: ${group(outage.carrier_failed_polls)} of ${group(outage.carrier_polls)} carrier calls failed; ${group(outage.erp_rejected)} of ${group(outage.erp_rows)} rows in the broken file held back.`
                : !outage.simulator_up ? "The simulator is not answering, so the drill cannot start."
                  : outage.can_toggle ? "Off. Switching it on changes the live feeds for everyone watching." : `Only Asha can start the drill; ${person} can watch what it does.`}
          </p>
          {drill.error && <p className="err" role="alert">{drill.error}</p>}
        </div>
      </aside>
    </main>
  );
}
