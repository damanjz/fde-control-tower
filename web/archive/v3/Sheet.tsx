"use client";
// The paper sheet: one incident in four stages (1 Caught, 2 Priced, 3 Contract, 4 Decision) and the trace.
// Everything here is evidence from GET /incidents/{id}/brief: real clause text, code-priced rupees, the agent's run.
import Link from "next/link";
import { useEffect, useState } from "react";
import LocatorMap from "./LocatorMap";
import { best, net as netOf, ordered } from "@/lib/case";
import { dur, group, hhmm, listWords, plural, rs, rsShort } from "@/lib/format";
import type { Brief, Done, Me, Network, Playbook } from "@/lib/types";
import { ROLE_PERSON, SOURCE, dcName, fallbackWhy, fixTitle, plain, stepLine, who } from "@/lib/words";

export type Triage = { incident: number; started: number } | null;

const tick = (
  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
);

/** Honest elapsed time for a triage that is running: wall seconds since the click, nothing simulated. */
function Elapsed({ since }: { since: number }) {
  const [s, setS] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setS(Math.max(0, Math.round((Date.now() - since) / 1000))), 1000);
    return () => clearInterval(t);
  }, [since]);
  return <span className="num">{s} s</span>;
}

export function lede(b: Brief, done: Done | undefined, now: number): string {
  const m = b.money, top = best(b.playbooks);
  if (done) return done.saved > 0 ? `Handled: ${rs(done.saved)} avoided.` : "Handled: customers have the new times; the penalty still applies.";
  if (m.waivable_inr > 0 && m.notify_deadline && Date.parse(m.notify_deadline) > now)
    return (m.waivable_inr >= m.net_inr - 1 ? "All of it is" : `${rsShort(m.waivable_inr)} of it is`) + ` waived if customers are told before ${hhmm(m.notify_deadline)}.`;
  if (top && netOf(top) > 0 && top.goods_value_inr > 0) {
    const how = top.action_type === "swap_carrier" ? "Moving the shipments to another carrier" : top.action_type === "reroute_via_dc" ? "Sending them from another warehouse" : "Moving stock from another warehouse";
    return `${how} saves ${top.est_saving_inr >= m.net_inr - 1 ? "all of it" : rsShort(top.est_saving_inr)}, but it moves ${rsShort(top.goods_value_inr)} of goods.`;
  }
  if (top && netOf(top) > 0) return `Acting now saves ${rsShort(top.est_saving_inr)} of it.`;
  return "No fix saves more than it costs, so customers get warned and the penalty is absorbed.";
}

type Props = {
  brief: Brief; me: Me | null; network: Network | null; done: Done | undefined; now: number;
  pick: number | null; onPick: (id: number) => void; locked: boolean;
  traceOpen: boolean; onTrace: () => void; triage: Triage; triageError: string | null; onTriage: () => void;
};

export default function Sheet({ brief: b, me, network, done, now, pick, onPick, locked, traceOpen, onTrace, triage, triageError, onTriage }: Props) {
  const inc = b.incident, m = b.money, ag = b.agent;
  const person = who(me?.id) || "you";
  const ps = ordered(b.playbooks);
  const p: Playbook | undefined = ps.find((x) => x.id === pick) ?? ps[0];
  const running = triage?.incident === inc.id;

  // 1 caught
  const whs = b.chain.warehouses.map((w) => dcName(network, w.id));
  const cities = b.chain.cities.map((c) => c.label);
  const lane = whs.length && cities.length
    ? `${listWords(whs)} ${whs.length > 1 ? "warehouses" : "warehouse"} to ${cities.length <= 2 ? listWords(cities) : `${cities.slice(0, 2).join(", ")} and ${plural(cities.length - 2, "more city", "more cities")}`}`
    : null;
  const topCarrier = b.chain.carriers.slice().sort((a, c) => c.shipments - a.shipments)[0];
  const car = topCarrier ? network?.carriers.find((c) => c.id === topCarrier.id) : undefined;
  const desk = car ? ("dispatch_phone" in car && car.dispatch_phone ? `, desk ${car.dispatch_phone}` : ", desk number hidden from finance") : "";

  // 2 priced: before a fix, what is at risk now; after one, what code priced before it
  const amt = (c: { net_inr: number; gross_inr: number }) => (done ? c.gross_inr : c.net_inr);
  const custs = m.top_customers.filter((c) => amt(c) > 0);
  const shown = custs.slice(0, 4), cmax = Math.max(1, ...shown.map(amt));
  const total = done ? m.gross_inr : m.net_inr;
  const restAmt = total - shown.reduce((s, c) => s + amt(c), 0), restN = b.chain.customers.length - shown.length;
  const late = m.max_days_late ?? 0;

  // 3 contract: the clause the chosen fix relies on comes first
  const cl = b.clauses.slice().sort((a, c) => Number(c.clause_id === p?.clause_id && c.chain === p?.clause_chain) - Number(a.clause_id === p?.clause_id && a.chain === p?.clause_chain));
  const chains = [...new Set(cl.map((c) => c.chain))];
  const sec = (id: string) => id.replace("MSA-", "");

  // 4 decision
  const fb = !!ag && ag.outcome !== "ok";
  const fromRun = b.playbooks[0] ? ROLE_PERSON[b.playbooks[0].role] ?? "another" : null;
  const toks = ag ? ag.prompt_tokens + ag.completion_tokens : 0;

  return (
    <>
      <section className="head">
        <h1>{inc.title}</h1>
        <p>{lede(b, done, now)}</p>
      </section>
      <ol className="stages">
        <li className="stage">
          <div className="rail"><span className="n">1</span><h2>Caught</h2></div>
          <div>
            <p className="say">{SOURCE[inc.source] ?? "A feed"} at {hhmm(inc.started_at)}. {ag ? "The agent found every shipment it touches." : "Code matched every shipment it touches."}</p>
            <p className="detail">{inc.detail}</p>
            <div className="art caught">
              <div className="facts">
                {m.shipments > 0 ? (
                  <>
                    <span className="big num">{plural(m.shipments, "shipment")}</span>
                    <span className="num">{rs(m.value_inr)} of goods</span>
                    {lane && <span>{lane}</span>}
                    {car && <span>{b.chain.carriers.length > 1 ? "Most with " : "All with "}{car.name}<span className="num">{desk}</span></span>}
                  </>
                ) : (
                  <>
                    <span className="big">No shipments affected now</span>
                    <span>The fix moved them out of the incident&apos;s way.</span>
                  </>
                )}
              </div>
              <div className="map"><LocatorMap brief={b} network={network} /></div>
            </div>
          </div>
        </li>

        <li className="stage">
          <div className="rail"><span className="n">2</span><h2>Priced</h2></div>
          <div>
            <p className="say">
              {custs.length
                ? <>{done ? "Before the fix, code" : "Code"}, not the model, priced the penalty from each customer&apos;s contract{late > 0 ? `: up to ${plural(late, "day")} late.` : m.fill_inr > 0 ? ": all of it is for short-filled orders." : "."}</>
                : "No penalty is left to price for these shipments."}
            </p>
            {custs.length > 0 && (
              <div className="art">
                <div className="cust">
                  {shown.map((c) => (
                    <div className="c" key={c.customer}>
                      <div className="row"><span>{c.customer}</span><span className="num">{rs(amt(c))}</span></div>
                      <div className="bar"><i style={{ width: `${((amt(c) / cmax) * 100).toFixed(1)}%` }} /></div>
                    </div>
                  ))}
                  {restN > 0 && restAmt >= 1 && (
                    <div className="c"><div className="row"><span>{plural(restN, "more customer")}</span><span className="num">{rs(restAmt)}</span></div></div>
                  )}
                </div>
                {m.unverified_rows > 0 && (
                  <p className="health-line">{plural(m.unverified_rows, "shipment")} priced from a contract figure that failed a source check; see <Link className="quiet" href="/data-health">data health</Link>.</p>
                )}
              </div>
            )}
          </div>
        </li>

        <li className="stage">
          <div className="rail"><span className="n">3</span><h2>Contract</h2></div>
          <div>
            <p className="say">
              {cl.length
                ? `${ag ? "The agent quoted" : "The proposed fixes quote"} ${cl.length > 1 ? "the clauses" : "the clause"} from ${listWords(chains.map((c) => `${c}'s`))} ${chains.length > 1 ? "contracts" : "contract"}; code checked ${cl.length > 1 ? "each quote" : "the quote"} against the PDF.`
                : "No contract clause is quoted for this case yet."}
            </p>
            {cl.length > 0 && (
              <div className="art clauses">
                {cl.map((c, i) => {
                  const where = `${c.chain} service agreement, section ${sec(c.clause_id)}, page ${c.page}`;
                  const check = c.verified
                    ? <span className="ok">{tick}Matches the PDF</span>
                    : <span className="warnword">Differs from the contract master; the PDF figure is used</span>;
                  const quote = <blockquote>&ldquo;{c.text}&rdquo;</blockquote>;
                  return i === 0 ? (
                    <div className="clause lead" key={c.chain + c.clause_id}>{quote}<cite><span>{c.heading}, {where}</span>{check}</cite></div>
                  ) : (
                    <div className="clause" key={c.chain + c.clause_id}>
                      <details>
                        <summary>Also quoted: {c.heading.toLowerCase()}, section {sec(c.clause_id)}, page {c.page}{c.verified ? ", matches the PDF" : ", differs from the contract master"}</summary>
                        {quote}
                      </details>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </li>

        <li className="stage">
          <div className="rail"><span className="n">4</span><h2>Decision</h2></div>
          <div>
            <p className="say">
              <span>
                {!ag
                  ? (ps.length ? `The agent has not run as ${person} yet. These fixes come from ${fromRun}'s run; code priced each one.` : `The agent has not run as ${person} yet, so there is no fix to approve.`)
                  : fb ? fallbackWhy(ag) : `The agent proposed ${plural(ps.length, "fix", "fixes")}; code priced each one.`}
              </span>
              {ag && !running && (
                <> <button type="button" className="quiet trace-btn" aria-expanded={traceOpen} aria-controls="trace" onClick={onTrace}>{traceOpen ? "Hide trace" : "Show trace"}</button></>
              )}
            </p>
            <div className="art">
              {(!ag || running) && (
                <div className="triage" aria-live="polite">
                  <button type="button" className="btn secondary" onClick={onTriage} disabled={running || !!triage}>{running ? "Running" : "Run the agent"}</button>
                  <p>
                    {running
                      ? <>The local model is working: <Elapsed since={triage!.started} /> so far, usually 40 to 60 s.</>
                      : triage ? "Another run is in progress; the local model takes one at a time."
                        : "It reads the incident, the shipments and the contracts, then proposes fixes for code to price. Usually 40 to 60 s on the local model, at Rs 0."}
                  </p>
                  {triageError && <p className="err" role="alert">{triageError}</p>}
                </div>
              )}
              {ps.length > 0 && (
                <div className="opts" role="radiogroup" aria-label="Proposed fixes">
                  {ps.map((x) => {
                    const bits = [x.est_saving_inr > 0 ? `Saves ${rs(x.est_saving_inr)}` : "Saves no penalty", x.est_cost_inr > 0 ? `costs ${rs(x.est_cost_inr)}` : "no cost"];
                    if (x.goods_value_inr > 0) bits.push(`moves ${rsShort(x.goods_value_inr)} of goods`);
                    return (
                      <label className="opt" key={x.id}>
                        <input type="radio" name={`fix-${inc.id}`} value={x.id} checked={x.id === p?.id} disabled={locked} onChange={() => onPick(x.id)} />
                        <span className="t">{fixTitle(x, network)}</span>
                        <span className="m num">{bits.join(", ")}</span>
                      </label>
                    );
                  })}
                </div>
              )}
              {ag && (
                <div className="trace" id="trace" hidden={!traceOpen}>
                  <p>
                    {dur(ag.ms)}, {group(toks)} tokens, {plural(ag.llm_calls, "model call")}, Rs 0 per run on a local model ({ag.model}).{" "}
                    {fb ? `Outcome: ${fallbackWhy(ag)}` : "Outcome: every figure and quote passed the checks."}
                  </p>
                  <ol className="steps">
                    {ag.steps.map((s) => {
                      const [what, said] = stepLine(s.tool, s.result_summary, network);
                      return (
                        <li key={s.seq}>
                          <span className="n num">{s.seq}</span>
                          <span><b>{what}.</b> {said}</span>
                          <span className="ms num">{dur(s.ms)}</span>
                        </li>
                      );
                    })}
                  </ol>
                  {ag.summary && <p className="sum">{fb ? "The rule's summary" : "The agent's summary"}: {plain(ag.summary, network)}</p>}
                  <p>Run by {who(ag.user_id)} at {hhmm(ag.started_at)}.</p>
                  <div className="again">
                    <button type="button" className="btn secondary" onClick={onTriage} disabled={!!triage}>Run again</button>
                    <span>A new run takes about 40 to 60 s on the local model; the last one took {dur(ag.ms)}.</span>
                  </div>
                  {triageError && <p className="err" role="alert">{triageError}</p>}
                </div>
              )}
            </div>
          </div>
        </li>
      </ol>
    </>
  );
}

