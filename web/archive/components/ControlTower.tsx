"use client";
// The whole screen (.design/command.html), on the real API through the /api proxy. Polls every 5 s; the brief, Data
// health and Audit poll while they are open. "View as" switches the role the proxy calls the tower as.
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import Brief, { type BriefState, type PbState, type Triage } from "./Brief";
import MapView from "./MapView";
import { AuditPage, HealthPage, IncidentsPage, type DlState } from "./Pages";
import { api } from "@/lib/client";
import { cap, count, dcCity, hhmm, hhmmss, incidentId, inr, lakhs } from "@/lib/format";
import type { MapEngine, MapData } from "@/lib/mapEngine";
import { ROLES, ROLE_COOKIE, roleOf, type RoleId } from "@/lib/roles";
import type { AuditRow, Brief as BriefT, DeadLetter, DeadLetters, Feed, Incident, Live, Me, Network, Outage } from "@/lib/types";
import { who } from "@/lib/words";

export type Initial = {
  role: RoleId; me: Me | null; network: Network | null; incidents: Incident[] | null; live: Live | null;
  outage: Outage | null; incident: number | null; down: string | null;
};
type View = "map" | "inc" | "health" | "audit";
const TABS: [View, string][] = [["map", "Live map"], ["inc", "Incidents"], ["health", "Data health"], ["audit", "Audit"]];
const KC: Record<string, string> = { weather: "var(--watch)", facility: "var(--risk)", carrier: "var(--risk)", supplier: "#7aa7ff" };
const POLL = 5000;

function useVisibleInterval(fn: () => void, ms: number, deps: unknown[]) {
  useEffect(() => {
    fn();
    const t = setInterval(() => { if (!document.hidden) fn(); }, ms);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

function Clock({ offset }: { offset: number | null }) {
  const [text, setText] = useState("");
  useEffect(() => {
    if (offset == null) return;
    const tick = () => setText(hhmmss(Date.now() + offset));
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, [offset]);
  return <span className="clock num" title="Scenario clock (IST)">{text}</span>;
}

export default function ControlTower({ initial }: { initial: Initial }) {
  const [role, setRole] = useState<RoleId>(initial.role);
  const roleRef = useRef(role);
  const [me, setMe] = useState(initial.me);
  const [network, setNetwork] = useState(initial.network);
  const [incidents, setIncidents] = useState(initial.incidents);
  const [live, setLive] = useState(initial.live);
  const [outage, setOutage] = useState(initial.outage);
  const [outageBusy, setOutageBusy] = useState(false);
  const [down, setDown] = useState<string | null>(initial.down);
  const [offset, setOffset] = useState<number | null>(null);
  const [view, setView] = useState<View>("map");
  const [sel, setSel] = useState<number | null>(initial.incident);
  const selRef = useRef(sel);
  const [brief, setBrief] = useState<BriefState>(initial.incident != null ? { id: initial.incident, status: 0, data: null, detail: null } : null);
  const [triage, setTriage] = useState<Triage>(null);
  const triageRef = useRef<Triage>(null);
  const [triageError, setTriageError] = useState<{ incident: number; detail: string } | null>(null);
  const [pb, setPb] = useState<PbState>({});
  const [feeds, setFeeds] = useState<Feed[] | null>(null);
  const [dead, setDead] = useState<{ status: number; data: DeadLetters | null } | null>(null);
  const [dl, setDl] = useState<DlState>({});
  const [audit, setAudit] = useState<{ rows: AuditRow[]; total: number } | null>(null);
  const [fresh, setFresh] = useState<Set<number>>(new Set());
  const auditMax = useRef(0);
  const engine = useRef<MapEngine | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const opener = useRef<number | null>(null);
  const tabRefs = useRef<Record<View, HTMLButtonElement | null>>({ map: null, inc: null, health: null, audit: null });

  // ------------------------------------------------------------------ data
  const refresh = useCallback(async () => {
    const r = roleRef.current;
    const [l, i, o] = await Promise.all([api<Live>(r, "live"), api<{ items: Incident[] }>(r, "incidents"), api<Outage>(r, "outage")]);
    if (r !== roleRef.current) return;
    setDown(l.status === 0 || l.status === 502 ? l.detail : null);
    if (l.data) { setLive(l.data); setOffset(Date.parse(l.data.now) - Date.now()); }
    if (i.data) setIncidents(i.data.items);
    if (o.data) setOutage(o.data);
  }, []);

  useVisibleInterval(refresh, POLL, [role]);

  useEffect(() => {   // who am I and the network (carrier contacts are masked per role)
    const r = role;
    void Promise.all([api<Me>(r, "me"), api<Network>(r, "network")]).then(([m, n]) => {
      if (r !== roleRef.current) return;
      if (m.data) setMe(m.data);
      if (n.data) setNetwork(n.data);
    });
  }, [role]);

  const loadBrief = useCallback(async (id: number) => {
    const r = roleRef.current;
    const res = await api<BriefT>(r, `incidents/${id}/brief?limit=25`);
    if (r !== roleRef.current || selRef.current !== id) return;
    setBrief({ id, status: res.status, data: res.data, detail: res.detail });
  }, []);

  useEffect(() => {
    selRef.current = sel;
    const url = new URL(location.href);
    if (sel == null) url.searchParams.delete("incident"); else url.searchParams.set("incident", String(sel));
    history.replaceState(null, "", url);
    if (sel == null) return;
    void loadBrief(sel);
    const t = setInterval(() => { if (!document.hidden) void loadBrief(sel); }, 10_000);
    return () => clearInterval(t);
  }, [sel, role, loadBrief]);

  const loadHealth = useCallback(async () => {
    const r = roleRef.current;
    const [f, d] = await Promise.all([api<{ feeds: Feed[] }>(r, "health/feeds"), api<DeadLetters>(r, "dead-letters?limit=60")]);
    if (r !== roleRef.current) return;
    if (f.data) setFeeds(f.data.feeds);
    setDead({ status: d.status, data: d.data });
  }, []);
  useVisibleInterval(() => { if (view === "health") void loadHealth(); }, POLL, [view, role]);

  const loadAudit = useCallback(async () => {
    const r = roleRef.current;
    const a = await api<{ total: number; items: AuditRow[] }>(r, "audit?limit=100");
    if (r !== roleRef.current || !a.data) return;
    const max = Math.max(0, ...a.data.items.map((x) => x.id));
    setFresh(new Set(auditMax.current ? a.data.items.filter((x) => x.id > auditMax.current).map((x) => x.id) : []));
    auditMax.current = Math.max(auditMax.current, max);
    setAudit({ rows: a.data.items, total: a.data.total });
  }, []);
  useVisibleInterval(() => { if (view === "audit") void loadAudit(); }, POLL, [view, role]);

  // ------------------------------------------------------------------ actions
  const changeRole = (r: RoleId) => {
    document.cookie = `${ROLE_COOKIE}=${r}; path=/; max-age=31536000; samesite=lax`;
    roleRef.current = r;
    setRole(r);
    setLive(null); setIncidents(null); setMe(null); setPb({}); setDead(null); setDl({}); setAudit(null); setFeeds(null);
    auditMax.current = 0;
    setBrief((b) => (b ? { id: b.id, status: 0, data: null, detail: null } : null));
  };

  const open = useCallback((id: number | null) => {
    const was = selRef.current;
    setView("map");
    setSel(id);
    setBrief((b) => (id == null ? null : b?.id === id ? b : { id, status: 0, data: null, detail: null }));
    // keyboard and screen-reader users land in the brief, and return to the incident they came from
    if (id != null) { opener.current = id; setTimeout(() => closeRef.current?.focus({ preventScroll: true }), 380); }
    else if (was != null) {
      setTimeout(() => {
        const card = document.querySelector<HTMLElement>(`.card[data-d="${opener.current ?? was}"]`);
        if (card) card.focus({ preventScroll: true });
        else if (!engine.current?.focusMark(opener.current ?? was)) document.getElementById("rail")?.focus({ preventScroll: true });
      }, 380);
    }
  }, []);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape" && selRef.current != null) open(null); };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [open]);

  const runTriage = async (id: number) => {
    if (triageRef.current) return;
    const r = roleRef.current, t = { incident: id, role: r, started: Date.now() };
    triageRef.current = t; setTriage(t); setTriageError(null);
    const res = await api(r, `incidents/${id}/triage`, { method: "POST" });
    triageRef.current = null; setTriage(null);
    if (res.status !== 200) setTriageError({ incident: id, detail: `The triage did not finish: ${res.detail} (HTTP ${res.status})` });
    if (roleRef.current === r && selRef.current === id) await loadBrief(id);
    void refresh();
  };

  const runPlaybook = async (p: { id: number; incident_id: number }, action: "run" | "approve") => {
    const r = roleRef.current;
    setPb((s) => ({ ...s, [p.id]: { busy: true } }));
    const res = await api(r, `playbooks/${p.id}/${action}`, { method: "POST" });
    if (r !== roleRef.current) return;
    setPb((s) => ({ ...s, [p.id]: res.status === 200 ? {} : { error: `The tower refused: ${res.detail} (HTTP ${res.status})` } }));
    await Promise.all([loadBrief(p.incident_id), refresh()]);
  };

  const onDead = async (d: DeadLetter, action: "replay" | "discard") => {
    const r = roleRef.current;
    setDl((s) => ({ ...s, [d.id]: { busy: true } }));
    const res = await api<{ ok: boolean }>(r, `dead-letters/${d.id}/${action}`, { method: "POST" });
    const b = (res.body ?? {}) as { reason?: string; suggested_fix?: string };
    setDl((s) => ({ ...s, [d.id]: res.status === 200 ? {} : res.status === 422
      ? { error: `Replay failed: ${b.reason ?? "rejected"} → ${b.suggested_fix ?? "discard it"}` }
      : { error: `The tower refused: ${res.detail} (HTTP ${res.status})` } }));
    await Promise.all([loadHealth(), refresh()]);
  };

  const toggleOutage = async () => {
    if (!outage) return;
    setOutageBusy(true);
    const res = await api<Outage>(roleRef.current, "outage", { method: "POST", body: { on: !outage.active } });
    setOutageBusy(false);
    if (res.data) setOutage(res.data);
    else setDown(`the outage switch failed: ${res.detail}`);
    void refresh();
  };

  const onTabKey = (e: KeyboardEvent<HTMLElement>) => {
    const i = TABS.findIndex(([v]) => v === view);
    const j = e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i + TABS.length - 1) % TABS.length
      : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : -1;
    if (j < 0) return;
    e.preventDefault();
    setView(TABS[j][0]);
    tabRefs.current[TABS[j][0]]?.focus();
  };

  // ------------------------------------------------------------------ derived
  const ships = useMemo(() => live?.shipments ?? [], [live]);
  const scoped = !!me && typeof me.scope === "object";
  const scopeDcs = me && typeof me.scope === "object" ? me.scope.dcs : [];
  const plans = live?.plans ?? {};
  const visible = incidents ?? [];
  const thr = me?.approval_threshold_inr ?? 500_000;
  const stale = useMemo(() => new Set(Object.entries(live?.circuits ?? {}).filter(([, s]) => s !== "closed").map(([c]) => c)), [live]);
  const mapData = useMemo<MapData | null>(() => (network && live && incidents ? {
    network, shipments: live.shipments, incidents, nowMs: Date.parse(live.now), stale, scoped: !!me && typeof me.scope === "object",
  } : null), [network, live, incidents, stale, me]);
  const selInc = sel != null ? visible.find((d) => d.id === sel) ?? null : null;
  const lit = sel != null ? ships.filter((s) => s.incidents.includes(sel)).length : 0;
  const dcNames = scopeDcs.map((id) => dcCity(network?.warehouses.find((w) => w.id === id)?.city ?? id)).join(", ");
  const staleTrucks = ships.filter((s) => stale.has(s.carrier_id)).length;

  const caption = selInc ? (
    <><small>{incidentId(selInc.id)} · {selInc.kind} · since {hhmm(selInc.started_at)}</small><b>{selInc.title}</b><span>{count(lit, "truck")} lit{scoped ? " in your zone" : ""}</span></>
  ) : sel != null ? (
    <><small>{incidentId(sel)}</small><b>Hidden by your role</b><span>None of its shipments leave {scopeDcs.join(", ") || "your scope"}</span></>
  ) : scoped ? (
    <><small>{roleOf(role).title}</small><b>{dcNames} DC lanes</b><span>Other zones are hidden by your role</span></>
  ) : (
    <><small>Tarangini Distribution · live</small><b>All India</b><span>Hover a truck or a warehouse; click an incident</span></>
  );

  const banner = down ? (
    <><b>Tower not answering</b><span>{down}; showing the last data it sent</span></>
  ) : outage?.active ? (
    <>
      <b>Outage simulation</b>
      <span>
        {Math.round(outage.error_rate * 100)}% of carrier API calls failing · ERP file malformed{outage.erp_rejected ? `, ${count(outage.erp_rejected, "row")} to dead letters` : ""}
        {staleTrucks ? ` · ${count(staleTrucks, "truck")} on cached ETAs` : ""}
        {live?.dead_letters_waiting != null ? ` · ${count(live.dead_letters_waiting, "row")} waiting in dead letters` : ""}
      </span>
      <button onClick={() => setView("health")}>See data health</button>
    </>
  ) : null;

  const kpis: [string, string, string?][] = live ? [
    [inr(ships.reduce((a, s) => a + s.value_inr, 0)), `moving · ${ships.length} trucks`],
    [String(ships.filter((s) => s.risk === "at risk").length), "trucks at risk"],
    [inr(live.exposure_inr), "at risk today", "r"],
  ] : [];
  const openCount = visible.filter((d) => d.status === "active" && !plans[d.id]).length;
  const r = roleOf(role);

  return (
    <div className={`app ${outage?.active ? "outage" : ""}`} id="app">
      <header className="top">
        <div className="brand"><i /><b>Tarangini Control Tower</b></div>
        <nav className="tabs" role="tablist" id="tabs" aria-label="Views" onKeyDown={onTabKey}>
          {TABS.map(([v, label]) => (
            <button key={v} role="tab" id={`tab-${v}`} aria-controls={`v-${v}`} data-v={v} aria-selected={view === v} tabIndex={view === v ? 0 : -1}
              ref={(n) => { tabRefs.current[v] = n; }} onClick={() => setView(v)}>
              {label}
              {v === "inc" && <span className="ct" id="ct">{incidents ? openCount : ""}</span>}
              {v === "health" && <span className="ct dlq" id="dlct">{live?.dead_letters_waiting ?? ""}</span>}
            </button>
          ))}
        </nav>
        <div className="kpis" id="kpis">
          {kpis.map(([v, l, c]) => <div className={`kpi ${c ?? ""}`} key={l}><b className="num">{v}</b><span>{l}</span></div>)}
        </div>
        <Clock offset={offset} />
        <label className="who">
          <span className="av" id="av" style={{ background: r.av }} aria-hidden="true">{r.name[0]}</span>
          <select id="role" aria-label="View as" value={role} onChange={(e) => changeRole(e.target.value as RoleId)}>
            {ROLES.map((x) => <option key={x.id} value={x.id}>{x.name} · {x.title}</option>)}
          </select>
        </label>
        <button className="out" id="out" aria-pressed={!!outage?.active} aria-busy={outageBusy} disabled={!outage?.can_toggle || outageBusy}
          title={outage?.can_toggle ? "Fail 30% of carrier API calls and send a malformed ERP file" : "Only VP Operations can start the outage simulation"}
          onClick={toggleOutage}><i />Outage</button>
      </header>

      <section className={`view ${view === "map" ? "on" : ""} ${sel != null ? "briefing" : ""}`} id="v-map" role="tabpanel" aria-labelledby="tab-map">
        <MapView data={mapData} sel={sel} caption={caption} banner={banner} bannerClass={down ? "err" : ""} onOpen={open} engineRef={engine} />
        <aside className="side">
          <div className="rail" id="rail" tabIndex={-1} inert={sel != null}>
            <div className="rh"><b>Needs attention</b><span>ranked by money at risk</span></div>
            {visible.map((d) => {
              const plan = plans[d.id];
              return (
                <button className="card" key={d.id} data-d={d.id} style={{ "--c": KC[d.kind] ?? "var(--mut)" } as CSSProperties} onClick={() => open(d.id)}>
                  <span className="k">{cap(d.kind)} · {hhmm(d.started_at)}</span>
                  <span className={`x num ${d.net_inr > 0 ? "" : "z"}`}>{d.net_inr > 0 ? inr(d.net_inr) : "₹0"}</span>
                  <span className="t">{d.title}</span>
                  <span className="m">{count(d.shipments, "shipment")} · {inr(d.value_inr)} moving</span>
                  {plan && <span className="st">{plan.approved_by ? "Plan approved" : "Plan run"} · {who(plan.executed_by)} · {hhmm(plan.executed_at)}</span>}
                </button>
              );
            })}
            {incidents && visible.length === 0 && <div className="railfoot">Nothing needs attention in your view.</div>}
            <div className="railfoot">Exposure comes from the late-delivery and fill-rate clauses in each customer&apos;s contract. Anything that moves more than {lakhs(thr)} of goods waits for a person.</div>
          </div>
          <div className="brief" id="brief" tabIndex={-1} inert={sel == null}>
            <Brief state={brief} title={selInc?.title ?? null} role={role} me={me} network={network} triage={triage} triageError={triageError}
              pb={pb} closeRef={closeRef} onClose={() => open(null)} onTriage={runTriage} onPlaybook={runPlaybook} />
          </div>
        </aside>
      </section>
      <section className={`view page ${view === "inc" ? "on" : ""}`} id="v-inc" role="tabpanel" aria-labelledby="tab-inc">
        {view === "inc" && <IncidentsPage incidents={incidents} plans={plans} onOpen={open} />}
      </section>
      <section className={`view page ${view === "health" ? "on" : ""}`} id="v-health" role="tabpanel" aria-labelledby="tab-health">
        {view === "health" && <HealthPage feeds={feeds} dead={dead} waiting={live?.dead_letters_waiting ?? null} outage={outage}
          carriers={network?.carriers ?? []} role={role} dl={dl} onDead={onDead} />}
      </section>
      <section className={`view page ${view === "audit" ? "on" : ""}`} id="v-audit" role="tabpanel" aria-labelledby="tab-audit">
        {view === "audit" && <AuditPage rows={audit?.rows ?? null} fresh={fresh} carriers={network?.carriers ?? []} total={audit?.total ?? null} />}
      </section>
    </div>
  );
}
