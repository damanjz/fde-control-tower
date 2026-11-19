"use client";
// The single case: the strip of incidents on the indigo, the paper sheet with one incident in four stages, and the
// decision rail with the money, the deadline and Approve. Approve is real: POST /playbooks/{id}/run, then /approve
// for a move over the limit. The strip and the clock stay fresh by polling; the sheet keeps the evidence it opened with.
import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useClock } from "./Frame";
import Sheet, { type Triage } from "./Sheet";
import { best, deadlineRunning, defaultPick, nextOpen, ordered, savedBy, stripOrder, weight } from "@/lib/case";
import { api, type Reply } from "@/lib/client";
import { countdown, hhmm, rs, rsLimit, rsShort } from "@/lib/format";
import { roleOf, type RoleId } from "@/lib/roles";
import type { Brief, Done, Incident, Live, Me, Network, Playbook, RunReply } from "@/lib/types";
import { ROLE_PERSON, dcName, doneBy, fallbackWhy, fixDone, fixTitle, plain, shortTitle, who } from "@/lib/words";

export type CaseInitial = {
  role: RoleId; me: Me | null; network: Network | null; incidents: Incident[]; done: Record<number, Done>;
  id: number | null; brief: Brief | null; briefStatus: number; askedHidden: number | null; down: boolean;
};

const POLL = 15_000;

function useReducedMotion() {
  const [r, setR] = useState(false);
  useEffect(() => {
    const q = matchMedia("(prefers-reduced-motion: reduce)");
    const on = () => setR(q.matches);
    const t = setTimeout(on, 0);
    q.addEventListener("change", on);
    return () => { clearTimeout(t); q.removeEventListener("change", on); };
  }, []);
  return r;
}

/** The rail number: shown as is on load; on a change it counts once to the new value (700 ms, ease-out). */
function useCountTo(target: number, reduced: boolean) {
  const [shown, setShown] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    const start = from.current;
    if (start === target) return;
    from.current = target;
    let raf = 0;
    const t0 = performance.now();
    const step = (t: number) => {
      const k = reduced ? 1 : Math.min(1, (t - t0) / 700), e = 1 - Math.pow(1 - k, 3);
      setShown(start + (target - start) * e);
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target, reduced]);
  return shown;
}

/** A refusal from the tower, in plain words, in place. */
function refusal(r: Reply<unknown>, person: string): string {
  const d = r.detail ?? "";
  if (r.status === 403) {
    if (/cannot execute/.test(d)) return `${person}'s role cannot act on fixes. Nothing was done.`;
    if (/outside your scope/.test(d)) return `This fix touches a warehouse outside ${person}'s area. Nothing was done.`;
    if (/cannot approve/.test(d)) return "Only Asha can give the final approval. Nothing was done.";
    return `The tower refused this for ${person}. Nothing was done.`;
  }
  if (r.status === 404) return `This fix is no longer visible to ${person}. Reload the page to see the case as it is now.`;
  if (r.status === 409) return /not waiting for approval/.test(d)
    ? "This fix is no longer waiting for approval. Reload the page to see what happened."
    : "A shipment changed after this fix was priced, so nothing was done. Reload the page to see the new figures.";
  if (r.status === 0 || r.status === 502) return "The tower is not answering. Nothing was done.";
  return `The tower refused this (HTTP ${r.status}). Nothing was done.`;
}

export default function CaseView({ initial }: { initial: CaseInitial }) {
  const { role, me, network } = initial;
  const person = roleOf(role).name;
  const thr = me?.approval_threshold_inr ?? 500_000;
  const { now, resync } = useClock();
  const reduced = useReducedMotion();

  const [raw, setIncidents] = useState(initial.incidents);
  const [done, setDone] = useState(initial.done);
  const [sel, setSel] = useState(initial.id);
  const [brief, setBrief] = useState(initial.brief);
  const [briefStatus, setBriefStatus] = useState(initial.briefStatus);
  const [picks, setPicks] = useState<Record<number, number>>({});
  const [act, setAct] = useState<{ id: number; busy?: boolean; error?: string } | null>(null);
  const [traceOpen, setTraceOpen] = useState(false);
  const [triage, setTriage] = useState<Triage>(null);
  const [triageError, setTriageError] = useState<string | null>(null);
  const [barAway, setBarAway] = useState(false);
  const sheetRef = useRef<HTMLDivElement | null>(null);
  const innerRef = useRef<HTMLDivElement | null>(null);
  const sentinel = useRef<HTMLSpanElement | null>(null);
  const selRef = useRef(sel);
  const doneRef = useRef(done);
  useEffect(() => { selRef.current = sel; doneRef.current = done; }, [sel, done]);

  // the URL names the case on screen
  useEffect(() => {
    if (sel == null) return;
    const u = new URL(location.href);
    if (u.searchParams.get("incident") !== String(sel)) { u.searchParams.set("incident", String(sel)); history.replaceState(history.state, "", u); }
  }, [sel]);

  // the strip and the clock: incidents and executed plans, every 15 s while the tab is visible
  const refresh = useCallback(async () => {
    const [i, l] = await Promise.all([api<{ items: Incident[] }>(role, "incidents"), api<Live>(role, "live")]);
    if (i.data) setIncidents(i.data.items);
    if (!l.data) return;
    resync(l.data.now);
    const fresh = Object.values(l.data.plans).filter((p) => doneRef.current[p.incident_id]?.pb.id !== p.id);
    if (!fresh.length) return;
    const rows = await Promise.all(fresh.map((p) => api<Playbook>(role, `playbooks/${p.id}`)));
    setDone((d) => {
      const out = { ...d };
      for (const r of rows) if (r.data) out[r.data.incident_id] = { pb: r.data, saved: savedBy(r.data) };
      return out;
    });
  }, [role, resync]);
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) void refresh(); }, POLL);
    return () => clearInterval(t);
  }, [refresh]);

  // phone: the bottom bar steps aside while the rail's own Approve is on screen
  useEffect(() => {
    const el = sentinel.current;
    if (!el || !("IntersectionObserver" in window)) return;
    const io = new IntersectionObserver((es) => es.forEach((e) => setBarAway(e.isIntersecting)));
    io.observe(el);
    return () => io.disconnect();
  }, []);

  /** Incident change: the sheet's content cross-fades, 200 ms. */
  const crossfade = (fn: () => void) => {
    const inner = innerRef.current, sheet = sheetRef.current;
    if (reduced || !inner || !sheet || !inner.animate) { fn(); return; }
    const ghost = inner.cloneNode(true) as HTMLElement;
    ghost.querySelectorAll("[id]").forEach((e) => e.removeAttribute("id"));
    ghost.classList.add("ghost");
    ghost.setAttribute("aria-hidden", "true");
    ghost.inert = true;
    sheet.appendChild(ghost);
    flushSync(fn);
    inner.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 200, easing: "ease-out" });
    ghost.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 200, easing: "ease-out" }).onfinish = () => ghost.remove();
  };

  const go = async (id: number) => {
    if (id === selRef.current) return;
    const r = await api<Brief>(role, `incidents/${id}/brief?limit=1`);
    crossfade(() => {
      setSel(id); setBrief(r.data); setBriefStatus(r.status); setAct(null); setTraceOpen(false); setTriageError(null);
    });
  };

  const patchPlaybook = (pb: Playbook) => setBrief((b) => (b && b.incident.id === pb.incident_id ? { ...b, playbooks: b.playbooks.map((x) => (x.id === pb.id ? pb : x)) } : b));

  const approve = async (p: Playbook, kind: "run" | "approve") => {
    setAct({ id: p.id, busy: true });
    const r = await api<RunReply>(role, `playbooks/${p.id}/${kind}`, { method: "POST" });
    if (r.status !== 200 || !r.data) { setAct({ id: p.id, error: refusal(r, person) }); return; }
    const d = r.data;
    const pb: Playbook = { ...p, status: d.status, requested_by: d.requested_by, approved_by: d.approved_by, executed_by: d.executed_by, executed_at: d.executed_at, result: d.result ?? p.result };
    patchPlaybook(pb);
    setAct(null);
    if (pb.status === "executed") {
      setDone((x) => ({ ...x, [pb.incident_id]: { pb, saved: savedBy(pb) } }));
      void refresh();
    }
  };

  const runTriage = async () => {
    if (!brief || triage) return;
    const id = brief.incident.id;
    setTriage({ incident: id, started: Date.now() }); setTriageError(null);
    const r = await api<unknown>(role, `incidents/${id}/triage`, { method: "POST" });
    setTriage(null);
    if (r.status !== 200) { setTriageError(r.status === 404 ? "This incident is not visible to you any more." : "The run did not finish: the local model or the tower stopped answering. Nothing changed."); return; }
    if (selRef.current !== id) return;
    const b = await api<Brief>(role, `incidents/${id}/brief?limit=1`);
    if (b.data) { setBrief(b.data); setTraceOpen(true); }
  };

  // ------------------------------------------------------------------ derived
  const incidents = stripOrder(raw, done);
  const maxGross = Math.max(1, ...incidents.map((i) => weight(i, done)));
  const at = incidents.findIndex((i) => i.id === sel);
  const scopeDcs = me && typeof me.scope === "object" ? me.scope.dcs : null;
  const scopeNote = scopeDcs ? `${person} sees ${scopeDcs.map((d) => dcName(network, d)).join(" and ")} incidents only.` : null;
  const stripNote = initial.askedHidden != null && initial.askedHidden !== sel
    ? `${scopeNote ?? ""} The case in the link is outside ${person}'s view, so the most urgent one ${person} can see is open.`.trim()
    : scopeNote;

  const b = brief, d = b ? done[b.incident.id] : undefined;
  const ps = b ? ordered(b.playbooks) : [];
  const pickId = b ? picks[b.incident.id] ?? defaultPick(b) : null;
  const p = ps.find((x) => x.id === pickId) ?? ps[0];
  const top = b ? best(b.playbooks) : null;
  const m = b?.money;
  const won = !!d && d.saved > 0;
  const target = !b ? 0 : won ? d!.saved : m!.net_inr;
  const fig = useCountTo(target, reduced);
  const dlRunning = !!m && !d && deadlineRunning(m, now);
  const dlClosed = !!m && !d && !!m.notify_deadline && !dlRunning && b?.incident.kind === "weather" && Date.parse(m.notify_deadline) <= now;
  const left = m?.notify_deadline ? Date.parse(m.notify_deadline) - now : 0;
  const next = b && d ? nextOpen(incidents, b.incident.id, done, now) : null;

  // the approval gate, from /me and the fix itself; the tower has the last word (its refusal shows in place)
  let gate = "", off = false, label = "Approve", kind: "run" | "approve" = "run", stepLabel = "Recommended";
  if (p) {
    const big = p.goods_value_inr > thr;
    const pendingNow = p.status === "pending_approval";
    stepLabel = pendingNow ? "Needs Asha's approval" : p.id === top?.id ? "Recommended" : "Your pick";
    if (pendingNow) {
      gate = `Asked by ${who(p.requested_by)}. It moves ${rsShort(p.goods_value_inr)} of goods, over the ${rsLimit(thr)} limit.`;
      if (me?.can_approve) { gate += " Approving runs it now."; label = "Approve as Asha"; kind = "approve"; }
      else { gate += ` Only Asha can approve it; ${person} can see it waiting.`; label = "Waiting for Asha"; off = true; }
    } else if (me && !me.can_execute) {
      gate = `Finance cannot act on fixes: ${person} sees the money, not the controls.`; off = true;
    } else if (scopeDcs && !p.dc_scope.every((x) => scopeDcs.includes(x))) {
      gate = `This fix leaves from ${p.dc_scope.map((x) => dcName(network, x)).join(" and ")}, outside ${person}'s area.`; off = true;
    } else if (big) {
      gate = `Moves ${rsShort(p.goods_value_inr)} of goods, over the ${rsLimit(thr)} limit, so it needs Asha's approval.`;
      label = me?.can_approve ? "Approve" : "Ask Asha to approve";
    } else if (p.goods_value_inr > 0) {
      gate = `Moves ${rsShort(p.goods_value_inr)} of goods, under the ${rsLimit(thr)} limit, so a dispatcher can approve it.`;
    } else {
      gate = "Moves no goods, so a dispatcher can approve it.";
    }
    if (p.status === "failed") { gate = "This fix failed when it ran; the audit trail has the details."; off = true; }
  }
  const busy = !!act?.busy && act.id === p?.id;
  const err = act?.error && act.id === p?.id ? act.error : null;
  const why = !p ? "" : b?.agent && b.agent.outcome !== "ok" ? `Chosen by rule. ${fallbackWhy(b.agent)}`
    : `Agent's reason${b?.agent ? "" : `, from ${ROLE_PERSON[p.role] ?? "another"}'s run`}: ${plain(p.rationale, network)}`;
  const doneLine = d ? `${doneBy(d.pb)} ${fixDone(d.pb, d.saved, network)}` : "";
  const sub = d ? (won ? "Avoided, approved" : "Handled") : dlRunning ? null : "At risk";

  const onApprove = () => { if (p && !off && !busy) void approve(p, kind); };

  return (
    <>
      <main className="case">
        <nav className="strip" aria-label="This morning's incidents, largest first">
          {incidents.map((i, k) => {
            const dn = done[i.id], isSel = i.id === sel;
            const amt = dn ? (dn.saved > 0 ? `${rsShort(dn.saved)} avoided` : "Handled") : i.net_inr > 0 ? rsShort(i.net_inr) : "Nothing at risk";
            const cls = ["inc", dn ? "done" : "", isSel ? "paper" : "", k === 0 ? "first" : "", k === 5 ? "last" : ""].filter(Boolean).join(" ");
            return (
              <button key={i.id} type="button" className={cls} aria-current={isSel ? "true" : undefined} onClick={() => void go(i.id)}>
                <span className="name">{shortTitle(i.title)}</span>
                <span className="row"><span className="bar"><i style={{ width: `${((weight(i, done) / maxGross) * 100).toFixed(1)}%` }} /></span><span className="amt">{amt}</span></span>
              </button>
            );
          })}
          {stripNote && <p className="strip-note">{stripNote}</p>}
        </nav>

        <div className={`sheet paper ${at === 0 ? "join-l" : ""} ${at === 5 ? "join-r" : ""}`} ref={sheetRef}>
          <div className="sheet-in" ref={innerRef}>
            {b ? (
              <Sheet brief={b} me={me} network={network} done={d} now={now} pick={p?.id ?? null}
                onPick={(id) => { setPicks((x) => ({ ...x, [b.incident.id]: id })); setAct(null); }}
                locked={!!d || busy || p?.status === "pending_approval"}
                traceOpen={traceOpen} onTrace={() => setTraceOpen((o) => !o)}
                triage={triage} triageError={triageError} onTriage={() => void runTriage()} />
            ) : (
              <section className="head">
                <h1>{initial.down ? "The tower is not answering" : incidents.length ? "This case could not be opened" : "Nothing needs attention"}</h1>
                <p>{initial.down ? "Start it with python scripts/dev.py, then reload." : incidents.length ? (briefStatus === 404 ? `It is not in ${person}'s view.` : "The tower did not answer for this case. Reload to try again.") : `No incident touches ${person}'s view this morning.`}</p>
              </section>
            )}
          </div>
        </div>

        <aside className={`card ${won ? "won" : ""}`} aria-label="Decision">
          {b && m && (
            <>
              <span className="lbl">{d ? (won ? "Avoided" : "Still at risk after the fix") : "At risk"}</span>
              <span className="fig">{rs(fig)}</span>
              {dlRunning && (
                <div className="clock"><b>{countdown(left)}</b><span>left to tell customers, until {hhmm(m.notify_deadline!)}</span></div>
              )}
              {dlClosed && <div className="clock closed"><span>The window to tell customers closed at {hhmm(m.notify_deadline!)}.</span></div>}
              <div aria-live="polite">
                {d ? (
                  <>
                    <p className="done-line">{doneLine}</p>
                    {next && (
                      <button type="button" className="btn secondary next" onClick={() => void go(next.id)}>
                        Next case: {shortTitle(next.title)}, {rsShort(next.net_inr)} at risk
                      </button>
                    )}
                  </>
                ) : p ? (
                  <>
                    <div className="act">
                      <span className="lbl">{stepLabel}</span>
                      <p className="t">{fixTitle(p, network)}</p>
                      <p className="why">{why}</p>
                      <p className={`gate ${off ? "block" : ""}`}>{gate}</p>
                    </div>
                    <button type="button" className="btn primary" disabled={off || busy} aria-busy={busy} onClick={onApprove}>{busy ? "Working" : label}</button>
                    {err && <p className="err" role="alert">{err}</p>}
                  </>
                ) : (
                  <p className="gate">No fix has been proposed for {person} yet.</p>
                )}
              </div>
              <span className="sentinel" ref={sentinel} aria-hidden="true" />
            </>
          )}
        </aside>
      </main>

      {b && m && (
        <div className={`bbar ${won ? "won" : ""} ${d ? "closed" : ""} ${barAway ? "away" : ""}`} aria-hidden="true">
          <span className="fig">{rs(fig)}</span>
          <span className={`sub ${!d && !dlRunning ? "calm" : ""}`}>{sub ?? <><b>{countdown(left)}</b> left to tell customers, until {hhmm(m.notify_deadline!)}</>}</span>
          {!d && p && <button type="button" className="btn primary" tabIndex={-1} disabled={off || busy} onClick={onApprove}>{busy ? "Working" : label}</button>}
          {!d && p && <p className={`gate ${off ? "block" : ""}`}>{err ?? gate}</p>}
        </div>
      )}
    </>
  );
}
