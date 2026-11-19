"use client";
// The single case: the strip of incidents on the indigo, the paper sheet with one incident in four stages, and the
// decision rail with the money, the deadline and Approve. Approve is real: POST /playbooks/{id}/run, then /approve
// for a move over the limit. The strip and the clock stay fresh by polling; the sheet keeps the evidence it opened with.
import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useClock } from "./Frame";
import { Badge, Chev } from "./Marks";
import Sheet, { type Hi, type Triage } from "./Sheet";
import { best, deadlineRunning, defaultPick, nextOpen, ordered, revealed, savedBy, stripOrder, weight } from "@/lib/case";
import { api, type Reply } from "@/lib/client";
import { countdown, hhmm, rs, rsLimit, rsShort } from "@/lib/format";
import { roleOf, type RoleId } from "@/lib/roles";
import type { Brief, Done, Incident, Live, Me, Network, Playbook, RunReply } from "@/lib/types";
import { ROLE_PERSON, STAGE_OF, cannotUndo, clauseDrives, dcName, doneBy, fallbackWhy, fixDone, fixTitle, plain, shortTitle, who, whyLine } from "@/lib/words";

export type CaseInitial = {
  role: RoleId; me: Me | null; network: Network | null; incidents: Incident[]; done: Record<number, Done>;
  id: number | null; briefs: Record<number, Brief>; briefStatus: number; askedHidden: number | null; down: boolean;
};

const POLL = 15_000;
const SEEN = "ct-replayed";

/** Has this case's recorded run already been replayed in this browser session? (Per-viewer convenience only.) */
function seen(id: number): boolean {
  try { return (JSON.parse(sessionStorage.getItem(SEEN) ?? "[]") as number[]).includes(id); } catch { return true; }
}
function markSeen(id: number) {
  try { const a = JSON.parse(sessionStorage.getItem(SEEN) ?? "[]") as number[]; sessionStorage.setItem(SEEN, JSON.stringify([...new Set([...a, id])])); } catch { /* storage off: replay stays manual */ }
}

const ICON = {
  deadline: <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" strokeWidth="1.8" /><path d="M8 4.6V8l2.3 1.6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg>,
  pending: <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 1.5h9M3.5 14.5h9" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /><path d="M4.5 1.5h7v2.2L8.6 8l2.9 4.3v2.2h-7v-2.2L7.4 8 4.5 3.7Z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" /><path d="M6 13.4 8 10.6l2 2.8Z" fill="currentColor" /></svg>,
  done: <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" /></svg>,
};
const STAT_WORD = { deadline: "deadline running", pending: "waiting for approval", done: "done" };
const HOLD_MS = 5000;
const holdUntil = () => Date.now() + HOLD_MS;

/** Time left on a tab: "26 min", "2h 49m". */
function leftWords(ms: number): string {
  const m = Math.max(0, Math.floor(ms / 60_000));
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

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
  const [briefs, setBriefs] = useState(initial.briefs);
  const [briefStatus, setBriefStatus] = useState(initial.briefStatus);
  const [replay, setReplay] = useState<{ id: number; shown: number } | null>(null);
  const [hi, setHiState] = useState<Hi>({ cust: null, clause: null });
  const setHi = useCallback((h: Partial<Hi>) => setHiState((x) => ({ ...x, ...h })), []);
  const brief = sel != null ? briefs[sel] ?? null : null;
  const [picks, setPicks] = useState<Record<number, number>>({});
  const [act, setAct] = useState<{ id: number; busy?: boolean; error?: string } | null>(null);
  const [traceOpen, setTraceOpen] = useState(false);
  const [triage, setTriage] = useState<Triage>(null);
  const [triageError, setTriageError] = useState<string | null>(null);
  const [barAway, setBarAway] = useState(false);
  const [whyOpen, setWhyOpen] = useState(false);
  // the undo safety net: Approve waits 5 s before anything is sent
  const [hold, setHold] = useState<{ id: number; incident: number; kind: "run" | "approve"; until: number } | null>(null);
  const [holdLeft, setHoldLeft] = useState(0);
  const [keys, setKeys] = useState(false);
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

  // "watch the agent work": the first time a case opens in this session, its recorded run replays once (not under
  // reduced motion). One second here per real step; the real duration of each step is shown beside it.
  useEffect(() => {
    if (sel == null || !briefs[sel]?.agent) return;
    const id = sel;
    // decided inside the timer: React's development double-run of effects cancels the first timer before it fires
    const t = setTimeout(() => {
      if (seen(id)) return;
      markSeen(id);
      if (!matchMedia("(prefers-reduced-motion: reduce)").matches) setReplay({ id, shown: 1 });
    }, 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel]);
  useEffect(() => {
    if (!replay) return;
    const n = briefs[replay.id]?.agent?.steps.length ?? 0;
    const t = setTimeout(() => setReplay((r) => (!r ? r : r.shown >= n ? null : { ...r, shown: r.shown + 1 })), 1000);
    return () => clearTimeout(t);
  }, [replay, briefs]);

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
    const cached = briefs[id];
    const r = cached ? null : await api<Brief>(role, `incidents/${id}/brief?limit=200`);
    crossfade(() => {
      setSel(id); setReplay(null); setHiState({ cust: null, clause: null }); setHold(null);
      if (r) { const data = r.data; if (data) setBriefs((x) => ({ ...x, [id]: data })); setBriefStatus(r.status); } else setBriefStatus(200);
      setAct(null); setTraceOpen(false); setTriageError(null); setWhyOpen(false);
    });
  };

  const patchPlaybook = (pb: Playbook) => setBriefs((all) => {
    const b = all[pb.incident_id];
    return b ? { ...all, [pb.incident_id]: { ...b, playbooks: b.playbooks.map((x) => (x.id === pb.id ? pb : x)) } } : all;
  });

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
    const b = await api<Brief>(role, `incidents/${id}/brief?limit=200`);
    const data = b.data;
    if (data) { setBriefs((x) => ({ ...x, [id]: data })); setTraceOpen(true); }
  };

  // the hold: ticks down, then sends; Undo, Esc or another incident cancels it before anything leaves the browser
  const approveRef = useRef(approve);
  useEffect(() => { approveRef.current = approve; });
  useEffect(() => {
    if (!hold) return;
    const t = setInterval(() => {
      const leftMs = hold.until - Date.now();
      setHoldLeft(Math.max(0, leftMs));
      if (leftMs <= 0) {
        clearInterval(t);
        const pb = briefs[hold.incident]?.playbooks.find((x) => x.id === hold.id);
        setHold(null);
        if (pb) void approveRef.current(pb, hold.kind);
      }
    }, 100);
    return () => clearInterval(t);
  }, [hold, briefs]);

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
  const rp = replay && b && replay.id === b.incident.id ? replay.shown : null;
  const open = revealed(b?.agent?.steps ?? [], rp, STAGE_OF);
  const moneyOpen = open(2), actOpen = open(4);
  const target = !b || !moneyOpen ? 0 : won ? d!.saved : m!.net_inr;
  const drives = hi.clause ? clauseDrives(hi.clause).key : null;
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
  const doneLine = d ? `Done. ${doneBy(d.pb)} ${fixDone(d.pb, d.saved, network)} ${cannotUndo(d.pb)}` : "";
  const sub = d ? (won ? "Avoided, approved" : "Handled") : dlRunning ? null : "At risk";
  const after = p && m ? Math.max(0, m.net_inr - p.est_saving_inr) : 0;
  const status = (i: Incident): keyof typeof ICON | null =>
    done[i.id] ? "done" : briefs[i.id]?.playbooks.some((x) => x.status === "pending_approval") ? "pending" : deadlineRunning(i, now) ? "deadline" : null;

  const onApprove = () => {
    if (!p || !b || off || busy || hold || !actOpen) return;
    setHoldLeft(HOLD_MS);
    setHold({ id: p.id, incident: b.incident.id, kind, until: holdUntil() });
  };
  const holding = !!hold && !!b && hold.incident === b.incident.id;

  // keyboard: arrows or [ ] for the previous or next incident, 1-4 to pick a fix, A to approve (through the hold),
  // ? for the sheet, Esc to cancel a hold or close the sheet. Not while typing, nor inside the View as group.
  const keyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  useEffect(() => {
    keyRef.current = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.closest("input, textarea, select, [contenteditable=true]") || t.closest(".seg"))) return;
      if (e.key === "Escape") { if (hold) setHold(null); else setKeys(false); return; }
      if (e.key === "?") { e.preventDefault(); setKeys((k) => !k); return; }
      if (!b) return;
      const k = incidents.findIndex((i) => i.id === b.incident.id);
      if (e.key === "ArrowRight" || e.key === "]" || e.key === "ArrowLeft" || e.key === "[") {
        const step = e.key === "ArrowRight" || e.key === "]" ? 1 : -1;
        const nxt = incidents[(k + step + incidents.length) % incidents.length];
        if (nxt && nxt.id !== b.incident.id) { e.preventDefault(); void go(nxt.id); }
        return;
      }
      if (/^[1-4]$/.test(e.key)) {
        const x = ps[Number(e.key) - 1];
        if (x && !d && !busy && !holding && p?.status !== "pending_approval") { e.preventDefault(); setPicks((q) => ({ ...q, [b.incident.id]: x.id })); setAct(null); }
        return;
      }
      if (e.key === "a" || e.key === "A") { e.preventDefault(); onApprove(); }
    };
  });
  useEffect(() => {
    const on = (e: KeyboardEvent) => keyRef.current(e);
    addEventListener("keydown", on);
    return () => removeEventListener("keydown", on);
  }, []);

  return (
    <>
      <main className="case">
        <nav className="strip" aria-label="This morning's incidents, largest first">
          {incidents.map((i, k) => {
            const dn = done[i.id], isSel = i.id === sel;
            const amt = dn ? (dn.saved > 0 ? `${rsShort(dn.saved)} avoided` : "Handled") : i.net_inr > 0 ? rsShort(i.net_inr) : "Nothing at risk";
            const urgent = !dn && deadlineRunning(i, now);
            const cls = ["inc", dn ? "done" : "", urgent ? "urgent" : "", isSel ? "paper" : "", k === 0 ? "first" : "", k === 5 ? "last" : ""].filter(Boolean).join(" ");
            const st = status(i);
            return (
              <button key={i.id} type="button" className={cls} aria-current={isSel ? "true" : undefined} title={i.title} onClick={() => void go(i.id)}>
                {/* the name owns the top line (never cut by a badge); time left sits with the money below */}
                <span className="top-line">
                  <span className="name">{shortTitle(i.title)}</span>
                  {st && st !== "deadline" && <span className={`stat ${st}`} title={STAT_WORD[st]}>{ICON[st]}<span className="sr">, {STAT_WORD[st]}</span></span>}
                </span>
                <span className="row">
                  <span className="bar"><i style={{ width: `${((weight(i, done) / maxGross) * 100).toFixed(1)}%` }} /></span>
                  {st === "deadline" && i.notify_deadline && (
                    <span className={`tleft num ${Date.parse(i.notify_deadline) - now < 30 * 60_000 ? "soon" : ""}`} title="Time left to tell customers">
                      {leftWords(Date.parse(i.notify_deadline) - now)}<span className="sr"> left to tell customers</span>
                    </span>
                  )}
                  <span className="amt">{amt}</span>
                </span>
              </button>
            );
          })}
          {stripNote && <p className="strip-note">{stripNote}</p>}
        </nav>

        <div className={`sheet paper ${at === 0 ? "join-l" : ""} ${at === 5 ? "join-r" : ""}`} ref={sheetRef}>
          <div className="sheet-in" ref={innerRef}>
            {b ? (
              <Sheet key={b.incident.id} brief={b} me={me} network={network} done={d} now={now} pick={p?.id ?? null}
                replay={rp} onWatch={() => setReplay({ id: b.incident.id, shown: 1 })} onSkip={() => setReplay(null)} hi={hi} setHi={setHi}
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
              <div className={`money ${moneyOpen ? "" : "wait"}`}>
                <span className="lbl">{d ? (won ? "Avoided" : "Still at risk after the fix") : "At risk"}</span>
                <span className={`fig litable ${drives ? "lit" : ""}`}>{rs(fig)}</span>
                {!d && <Badge>Priced by code from the contracts</Badge>}
                {dlRunning && (
                  <div className={`clock litable ${drives === "waiver" ? "lit" : ""}`}><b>{countdown(left)}</b><span>left to tell customers, until {hhmm(m.notify_deadline!)}</span></div>
                )}
                {dlClosed && <div className="clock closed"><span>The window to tell customers closed at {hhmm(m.notify_deadline!)}.</span></div>}
              </div>
              <div aria-live="polite">
                {!actOpen ? (
                  <p className="pending-work">The recommendation appears when the replay reaches the decision.</p>
                ) : d ? (
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
                      <p className="preview" key={p.id} style={{ animation: reduced ? undefined : "rise .3s var(--ease) both" }}>
                        <b>{rs(m.net_inr)}</b> at risk <svg className="arrow" viewBox="0 0 16 10" aria-label="to"><path d="M1 5h12.5M10 1.5 13.8 5 10 8.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg> <span className={after < m.net_inr ? "to" : "same"}>{rs(after)}</span> after this fix
                        {p.est_cost_inr > 0 ? `, costs ${rs(p.est_cost_inr)}` : ", no cost"}
                        {p.goods_value_inr > thr ? `; needs Asha's approval: moves ${rsShort(p.goods_value_inr)}` : ""}
                      </p>
                      <div className="whyrow">
                        <button type="button" className="quiet tog" aria-expanded={whyOpen} aria-controls="why" onClick={() => setWhyOpen((o) => !o)}><Chev />{whyOpen ? "Hide the agent's full reason" : "The agent's full reason"}</button>
                        <p className="why" id="why" hidden={!whyOpen}>{why}</p>
                      </div>
                      <p className={`gate ${off ? "block" : ""}`}>{gate}</p>
                      <p className="whyline">{whyLine(p, m.net_inr, m.notify_deadline && deadlineRunning(m, now) ? m.notify_deadline : null, network)}</p>
                    </div>
                    {holding ? (
                      <div className="hold" role="status">
                        <span className="hold-t"><b className="num">{kind === "approve" ? "Approving" : label === "Ask Asha to approve" ? "Asking Asha" : "Approving"} in {Math.ceil(holdLeft / 1000)} s</b><span>Nothing is sent until then.</span></span>
                        <button type="button" className="btn secondary undo" onClick={() => setHold(null)}>Undo</button>
                        {!reduced && <i className="hold-bar" style={{ animationDuration: `${HOLD_MS}ms` }} aria-hidden="true" />}
                      </div>
                    ) : (
                      <button type="button" className="btn primary" disabled={off || busy || !actOpen} aria-busy={busy} onClick={onApprove}>{busy ? "Working" : label}</button>
                    )}
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

      <button type="button" className="keys-btn quiet" aria-expanded={keys} onClick={() => setKeys((k) => !k)}>Keyboard shortcuts <kbd>?</kbd></button>
      {keys && (
        <div className="keys" role="dialog" aria-label="Keyboard shortcuts">
          <p className="keys-h">Keyboard shortcuts</p>
          <dl>
            <dt><kbd>&larr;</kbd> <kbd>&rarr;</kbd> or <kbd>[</kbd> <kbd>]</kbd></dt><dd>Previous or next incident</dd>
            <dt><kbd>1</kbd> to <kbd>4</kbd></dt><dd>Pick a fix</dd>
            <dt><kbd>A</kbd></dt><dd>Approve, with 5 s to undo</dd>
            <dt><kbd>Esc</kbd></dt><dd>Undo a pending approval; close this</dd>
            <dt><kbd>?</kbd></dt><dd>Show or hide this</dd>
          </dl>
          <p className="keys-n">Not while typing or inside View as. {me && !me.can_execute ? `${person} cannot approve, so A does nothing.` : ""}</p>
        </div>
      )}

      {b && m && (
        <div className={`bbar ${won ? "won" : ""} ${d ? "closed" : ""} ${barAway ? "away" : ""}`} aria-hidden="true">
          <span className="fig">{rs(fig)}</span>
          <span className={`sub ${!d && !dlRunning ? "calm" : ""}`}>{sub ?? <><b>{countdown(left)}</b> left, until <span className="nw">{hhmm(m.notify_deadline!)}</span></>}</span>
          {!d && p && (holding
            ? <button type="button" className="btn primary" tabIndex={-1} onClick={() => setHold(null)}>Undo, {Math.ceil(holdLeft / 1000)} s</button>
            : <button type="button" className="btn primary" tabIndex={-1} disabled={off || busy || !actOpen} onClick={onApprove}>{busy ? "Working" : label}</button>)}
          {!d && p && <p className={`gate ${off ? "block" : ""}`}>{err ?? gate}</p>}
        </div>
      )}
    </>
  );
}
