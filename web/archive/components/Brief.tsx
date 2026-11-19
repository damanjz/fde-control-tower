"use client";
// The click-to-open brief (.design/command.html: brief()), on GET /incidents/{id}/brief.
import { Fragment, useEffect, useState, type RefObject } from "react";
import { cap, count, dcCity, hhmm, incidentId, inr, lakhs, pad2, short } from "@/lib/format";
import { roleOf, type RoleId } from "@/lib/roles";
import type { Brief as BriefT, BriefShipment, Carrier, Me, Network, Playbook } from "@/lib/types";
import { TOOL, who } from "@/lib/words";

export type BriefState = { id: number; status: number; data: BriefT | null; detail: string | null } | null;
export type Triage = { incident: number; role: RoleId; started: number } | null;
export type PbState = Record<number, { busy?: boolean; error?: string }>;

type Props = {
  state: BriefState; title: string | null; role: RoleId; me: Me | null; network: Network | null;
  triage: Triage; triageError: { incident: number; detail: string } | null; pb: PbState;
  closeRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void; onTriage: (id: number) => void; onPlaybook: (p: Playbook, action: "run" | "approve") => void;
};

function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  return <>{Math.max(0, Math.round((now - since) / 1000))} s</>;
}

const Mask = ({ v }: { v: string }) => <span className="mask">{v}</span>;

function driverCell(s: BriefShipment) {
  if (!("driver_phone" in s) && !("driver_name" in s)) return <Mask v="removed" />;
  if (!s.driver_phone) return <span className="more">no bill of lading</span>;
  return s.driver_phone.includes("*") ? <Mask v={s.driver_phone} /> : s.driver_phone;
}

function bankCell(c: Carrier | undefined) {
  if (!c || !("bank_account" in c) || c.bank_account == null) return <Mask v="removed" />;
  return c.bank_account.includes("*") ? <Mask v={c.bank_account.replace(/^(\S+) A\/c /, "$1 ")} /> : c.bank_account;
}

export default function Brief({ state, title, role, me, network, triage, triageError, pb: pbs, closeRef, onClose, onTriage, onPlaybook }: Props) {
  const close = <button className="close" id="close" ref={closeRef} aria-label="Close the brief" onClick={onClose}>&times;</button>;
  if (!state) return null;
  const scoped = !!me && typeof me.scope === "object";
  const dcs = me && typeof me.scope === "object" ? me.scope.dcs.join(", ") : "";
  if (state.status === 404) {
    return (
      <div className="bin">
        <div className="bx"><div className="k"><span className="kind">Hidden by your role</span><span>{incidentId(state.id)}</span></div>{close}</div>
        <h2>Not in your view</h2>
        <p className="lead">
          The tower answered 404 for {roleOf(role).name}: none of this incident&apos;s shipments leave {dcs || "your scope"}, so in the
          {" "}{roleOf(role).title} view it does not exist. Its money, plans and shipments stay with the roles that cover them.
        </p>
      </div>
    );
  }
  if (!state.data) {
    return (
      <div className="bin">
        <div className="bx"><div className="k"><span>{incidentId(state.id)}</span></div>{close}</div>
        {title && <h2>{title}</h2>}
        <p className="lead">{state.status === 0 ? "Loading the brief." : `The tower did not answer: ${state.detail}.`}</p>
      </div>
    );
  }
  const b = state.data, inc = b.incident, thr = me?.approval_threshold_inr ?? 500_000;
  const carriers = network?.carriers ?? [];
  const carrierName = (id: string) => carriers.find((c) => c.id === id)?.name ?? id;
  const dcName = (id: string) => dcCity(network?.warehouses.find((w) => w.id === id)?.city ?? id);
  const ch = b.chain;
  const tot = (xs: { shipments: number }[]) => xs.reduce((a, x) => a + x.shipments, 0);
  const first: [string, string, string] =
    inc.kind === "supplier" ? ["Supplier", network?.suppliers.find((s) => s.id === inc.supplier_id)?.name ?? short(inc.title), `${inc.severity} severity`]
      : inc.kind === "carrier" ? ["Carrier", carrierName(inc.carrier_id ?? ""), inc.states?.join(", ") || `${inc.severity} severity`]
        : inc.kind === "facility" ? ["Facility", `${dcName(inc.dc_id ?? "")} DC`, `${inc.severity} severity`]
          : ["Event", short(inc.title), `since ${hhmm(inc.started_at)}`];
  const nodes: [string, string, string][] = [
    first,
    ["Warehouse", ch.warehouses.map((w) => dcName(w.id)).join(", ") || "none", count(ch.warehouses.length, "DC", "DCs")],
    ["Carriers", ch.carriers.map((c) => carrierName(c.id).split(" ")[0]).join(", ") || "none", count(tot(ch.carriers), "truck")],
    ["Cities", ch.cities.slice(0, 2).map((c) => c.label).join(", ") + (ch.cities.length > 2 ? ` +${ch.cities.length - 2}` : "") || "none", count(ch.cities.length, "city", "cities")],
    ["Customers", count(ch.customers.length, "retailer"), inr(ch.customers.reduce((a, c) => a + c.value_inr, 0))],
  ];
  const run = b.agent;
  const running = triage?.incident === inc.id && triage.role === role;
  const busyElsewhere = !!triage && !running;
  const tErr = triageError?.incident === inc.id ? triageError.detail : null;
  const items = b.shipments.items, shown = items.slice(0, 10);
  return (
    <div className="bin">
      <div className="bx">
        <div className="k"><span className="kind">{cap(inc.kind)}</span><span>{incidentId(inc.id)}</span><span>since {hhmm(inc.started_at)}</span></div>
        {close}
      </div>
      <h2>{inc.title}</h2>
      <p className="lead">{inc.detail}</p>
      <div className="money">
        <div className="h"><b className="num">{inr(b.money.net_inr)}</b><span>at risk{scoped ? " in your zone" : ""}</span></div>
        <div><b className="num">{inr(b.money.value_inr)}</b><span>goods on affected trucks</span></div>
        <div><b className="num">{b.money.shipments}</b><span>shipments{scoped ? ` from ${dcs}` : ""}</span></div>
      </div>

      <section className="sec">
        <h3><span>What it touches</span></h3>
        <div className="chain">
          {nodes.map(([k, v, s], i) => (
            <Fragment key={k}>
              {i > 0 && <span className="link" />}
              <div className={`node ${i ? "" : "src"}`}><small>{k}</small><b>{v}</b><span>{s}</span></div>
            </Fragment>
          ))}
        </div>
      </section>

      <section className="sec" aria-busy={running}>
        <h3>
          <span>How the agent got there</span>
          {run ? <span>{(run.ms / 1000).toFixed(1)} s · {(run.prompt_tokens + run.completion_tokens).toLocaleString("en-IN")} tokens · ₹0</span>
            : <span>{running ? "running" : "not run for your role"}</span>}
        </h3>
        {run && !running && (
          <>
            <div className="agent">
              {run.steps.map((s) => (
                <div key={s.seq}><i>{pad2(s.seq)}</i><span>{(TOOL[s.tool]?.[0] ?? s.tool) + ": " + s.result_summary}</span><em>{TOOL[s.tool]?.[1] ?? "tool"} · {s.ms >= 1000 ? `${(s.ms / 1000).toFixed(1)} s` : `${s.ms} ms`}</em></div>
              ))}
            </div>
            <p className="trace">
              {run.model} · {run.llm_calls} model calls · {run.prompt_tokens.toLocaleString("en-IN")} prompt + {run.completion_tokens.toLocaleString("en-IN")} completion tokens · run by {who(run.user_id)} at {hhmm(run.started_at)}
              {run.honesty_failures.length > 0 && <> · <span className="bad">{count(run.honesty_failures.length, "answer")} rejected by the honesty checks and retried</span></>}
              {run.outcome !== "ok" && <> · <span className="bad">agent fallback: {run.fallback_reason ?? run.outcome}; the playbooks were chosen by rule</span></>}
            </p>
            {run.summary && <p className="sum">{run.summary}</p>}
          </>
        )}
        {(!run || running) && (
          <div className="pb">
            <b>{running ? "The local model is working on it" : `No triage yet as ${roleOf(role).name}`}</b>
            <span>
              {running ? <>fusion27b:iq3 on this machine, one call at a time · <Elapsed since={triage!.started} /> so far · usually 40 to 60 s</>
                : "The agent reads the incident, the affected shipments, the contracts and the options, then proposes 2 to 4 playbooks. About 40 to 60 s on the local model."}
            </span>
            <button onClick={() => onTriage(inc.id)} disabled={running || busyElsewhere}>{running ? "Running" : "Run triage"}</button>
            {busyElsewhere && <span className="wait">another triage is running; one model call at a time</span>}
            {tErr && <span className="err">{tErr}</span>}
          </div>
        )}
        {run && !running && (
          <>
            <button className="again" onClick={() => onTriage(inc.id)} disabled={busyElsewhere}>Run triage again</button>
            {tErr && <p className="trace"><span className="bad">{tErr}</span></p>}
          </>
        )}
        {b.clauses.map((c) => (
          <div className="quote" key={c.chain + c.clause_id}>
            <b>{c.clause_id} · from the {c.chain} contract, page {c.page}{c.verified ? "" : " · numbers differ from the contract master"}</b>{c.text}
          </div>
        ))}
      </section>

      <section className="sec">
        <h3><span>What to do</span><span>over {lakhs(thr)} needs approval</span></h3>
        {b.playbooks.length === 0 && <div className="pb"><span>No playbooks yet. Run triage to get them.</span></div>}
        {b.playbooks.map((pb) => {
          const big = pb.goods_value_inr > thr, st = pbs[pb.id] ?? {};
          const done = pb.status === "executed", pending = pb.status === "pending_approval";
          const label = done ? "Done" : pending ? (me?.can_approve ? "Approve" : "Waiting") : pb.status === "failed" ? "Failed" : big ? "Request approval" : "Run";
          const action: "run" | "approve" = pending ? "approve" : "run";
          const res = pb.result;
          return (
            <div className="pb" key={pb.id}>
              <b>{pb.title}</b>
              <span>{pb.rationale}</span>
              <div className="n">
                {pb.est_cost_inr > 0 && <span>costs {inr(pb.est_cost_inr)}</span>}
                {pb.est_saving_inr > 0 && <em>saves {inr(pb.est_saving_inr)}</em>}
                {pb.goods_value_inr > 0 && <span>moves {inr(pb.goods_value_inr)}</span>}
              </div>
              {big && !done && <span className="lk">moves more than {lakhs(thr)} of goods{pending ? ` · waiting for approval, asked by ${who(pb.requested_by)}` : ""}</span>}
              <button className={done ? "done" : big || pending ? "need" : ""} disabled={done || st.busy || (pending && !me?.can_approve) || pb.status === "failed"}
                onClick={() => onPlaybook(pb, action)}>{st.busy ? "Working" : label}</button>
              {done && (
                <span className="au">
                  {who(pb.executed_by)}{pb.approved_by ? ` · approved by ${who(pb.approved_by)}` : ""} · {pb.executed_at ? hhmm(pb.executed_at) : ""}
                  {res && res.exposure_before_inr != null ? ` · exposure ${inr(res.exposure_before_inr)} → ${inr(res.exposure_after_inr ?? 0)}` : ""} · audit trail updated
                </span>
              )}
              {st.error && <span className="err">{st.error}</span>}
            </div>
          );
        })}
      </section>

      <section className="sec">
        <h3><span>Shipments</span><span>{shown.length} of {b.shipments.total}{scoped ? " in your zone" : ""}</span></h3>
        <div className="tw">
          <table>
            <thead><tr><th>Shipment</th><th>Customer</th><th>Carrier</th><th>Driver</th><th>Bank</th><th className="r">Goods</th></tr></thead>
            <tbody>
              {shown.map((s) => (
                <tr key={s.id}>
                  <td><span className="id">{s.id}</span></td><td>{s.customer}</td><td>{carrierName(s.carrier_id).split(" ")[0]}</td>
                  <td>{driverCell(s)}</td><td>{bankCell(carriers.find((c) => c.id === s.carrier_id))}</td><td className="r num">{inr(s.value_inr)}</td>
                </tr>
              ))}
              {b.shipments.total > shown.length && <tr><td colSpan={6} className="more">and {b.shipments.total - shown.length} more</td></tr>}
              {scoped && <tr className="hid"><td colSpan={6}>Shipments outside {dcs} are hidden by your role</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
