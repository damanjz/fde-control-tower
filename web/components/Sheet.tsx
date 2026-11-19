"use client";
// The paper sheet: one incident in four stages (1 Caught, 2 Priced, 3 Contract, 4 Decision), the replay of the agent's
// recorded run, and the trace. Everything here is evidence from GET /incidents/{id}/brief: real clause text,
// code-priced rupees, the shipments as the role may see them, the agent's own steps and timings.
import Link from "next/link";
import { useEffect, useState } from "react";
import LocatorMap from "./LocatorMap";
import { Badge, Chev } from "./Marks";
import { best, net as netOf, ordered, revealed } from "@/lib/case";
import { dur, group, hhmm, listWords, plural, rs, rsShort } from "@/lib/format";
import type { Brief, BriefShipment, Done, Me, Network, Playbook } from "@/lib/types";
import { ROLE_PERSON, SOURCE, STAGE_OF, carrierName, clauseDrives, dcName, driverLine, fallbackWhy, fixTitle, plain, stageWords, stepLine, who } from "@/lib/words";

export type Triage = { incident: number; started: number } | null;
export type Hi = { cust: string | null; clause: string | null };

const chev = <Chev />;
const SHIPS_FIRST = 12;

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
    const how = top.action_type === "swap_carrier" ? "A carrier swap" : top.action_type === "reroute_via_dc" ? "A re-route" : "A stock transfer";
    return `${how} saves ${top.est_saving_inr >= m.net_inr - 1 ? "all of it" : rsShort(top.est_saving_inr)}, but it moves ${rsShort(top.goods_value_inr)} of goods.`;
  }
  if (top && netOf(top) > 0) return `Acting now saves ${rsShort(top.est_saving_inr)} of it.`;
  return "No fix saves more than it costs, so customers get warned and the penalty is absorbed.";
}

/** A clause citation that opens the chain's signed PDF at the page, in the browser's own viewer (a new tab). */
function PdfLink({ chain, clauseId, page }: { chain: string; clauseId: string; page: number }) {
  return (
    <a className="quiet pdf" href={`/api/contracts/${encodeURIComponent(chain)}/pdf#page=${page}`} target="_blank" rel="noopener"
      title={`Open ${chain}'s contract at page ${page} (PDF, new tab)`}>
      section {clauseId.replace("MSA-", "")}, page {page}
    </a>
  );
}

const secOf = (cl: string[] | null | undefined) => (cl ?? []).map((c) => c.replace("MSA-", "section ")).join(", ") || "none";

/** The affected shipments, driver and phone exactly as the API returns them for this role. */
function ShipTable({ rows, network }: { rows: BriefShipment[]; network: Network | null }) {
  return (
    <div className="tw">
      <table className="tbl">
        <thead><tr><th>Shipment</th><th>Customer</th><th>Carrier</th><th className="r">Goods</th><th className="r">Late</th><th>Driver</th><th>Phone</th></tr></thead>
        <tbody>
          {rows.map((s) => {
            const d = driverLine(s);
            return (
              <tr key={s.id}>
                <td>{s.id}</td><td>{s.customer}</td><td>{carrierName(network, s.carrier_id)}</td>
                <td className="r num">{rs(s.value_inr)}</td><td className="r num">{s.days_late ? plural(s.days_late, "day") : "On time"}</td>
                <td className={d.hidden ? "masked" : ""}>{d.name}</td><td className={`num nw ${d.hidden || d.phone.includes("*") ? "masked" : ""}`}>{d.phone || "None"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

type Props = {
  brief: Brief; me: Me | null; network: Network | null; done: Done | undefined; now: number;
  pick: number | null; onPick: (id: number) => void; locked: boolean;
  traceOpen: boolean; onTrace: () => void; triage: Triage; triageError: string | null; onTriage: () => void;
  replay: number | null; onWatch: () => void; onSkip: () => void; hi: Hi; setHi: (h: Partial<Hi>) => void;
};

export default function Sheet(props: Props) {
  const { brief: b, me, network, done, now, pick, onPick, locked, traceOpen, onTrace, triage, triageError, onTriage, replay, onWatch, onSkip, hi, setHi } = props;
  const [openCust, setOpenCust] = useState<Record<string, boolean>>({});
  const [shipsOpen, setShipsOpen] = useState(false);
  const [shipsAll, setShipsAll] = useState(false);
  const [alsoOpen, setAlsoOpen] = useState<Record<string, boolean>>({});
  const inc = b.incident, m = b.money, ag = b.agent;
  const person = who(me?.id) || "you";
  const ps = ordered(b.playbooks);
  const p: Playbook | undefined = ps.find((x) => x.id === pick) ?? ps[0];
  const running = triage?.incident === inc.id;
  const steps = ag?.steps ?? [];
  const open = revealed(steps, replay, STAGE_OF);
  const stageBy = (k: number) => {
    const s = steps.filter((x) => (STAGE_OF[x.tool] ?? 1) === k);
    return s.length ? `${stageWords(s.map((x) => x.tool))} · ${dur(s.reduce((a, x) => a + x.ms, 0))}` : null;
  };
  const drives = hi.clause ? clauseDrives(hi.clause).key : null;
  const litMoney = drives === "late" || drives === "fill";

  // 1 caught
  const whs = b.chain.warehouses.map((w) => dcName(network, w.id));
  const cities = b.chain.cities.map((c) => c.label);
  const lane = whs.length && cities.length
    ? `${listWords(whs)} ${whs.length > 1 ? "warehouses" : "warehouse"} to ${cities.length <= 2 ? listWords(cities) : `${cities.slice(0, 2).join(", ")} and ${plural(cities.length - 2, "more city", "more cities")}`}`
    : null;
  const topCarrier = b.chain.carriers.slice().sort((a, c) => c.shipments - a.shipments)[0];
  const car = topCarrier ? network?.carriers.find((c) => c.id === topCarrier.id) : undefined;
  const desk = car ? ("dispatch_phone" in car && car.dispatch_phone ? `, desk ${car.dispatch_phone}` : ", desk number hidden from finance") : "";
  const ships = b.shipments.items;

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
  const stage = (k: number) => `stage ${open(k) ? "" : "wait"}`;
  const by = (k: number) => { const t = stageBy(k); return t && open(k) ? <span className="by">{t}</span> : null; };

  return (
    <>
      <section className="head split">
        <div className="hrow">
          <h1>{inc.title}</h1>
          {ag && (
            <div className="watch-ctl">
              <button type="button" className="btn secondary" onClick={replay != null ? onSkip : onWatch}>{replay != null ? "Skip to the result" : "Watch the agent work"}</button>
              <span aria-live="polite">
                {replay != null ? `Replaying step ${Math.min(replay, steps.length)} of ${steps.length}, 1 s each` : `Recorded run: ${plural(steps.length, "step")}, ${dur(ag.ms)}`}
              </span>
            </div>
          )}
        </div>
        <p className={`litable ${drives === "waiver" ? "lit" : ""}`}>{lede(b, done, now)}</p>
      </section>

      {ag && replay != null && (
        <ol className="wlog" aria-label="The agent's recorded run, replayed">
          {steps.slice(0, replay).map((s, i) => (
            <li key={s.seq} className={i === replay - 1 ? "now" : ""}>
              <span className="n">{s.seq}</span><b>{stepLine(s.tool, s.result_summary, network)[0]}</b>
              {i === replay - 1 && <i className="dot" aria-hidden="true" />}
              <span className="ms">{dur(s.ms)}</span>
            </li>
          ))}
        </ol>
      )}

      <ol className="stages">
        <li className={stage(1)}>
          <div className="rail"><div className="rt"><span className="n">1</span><h2>Caught</h2></div>{by(1)}</div>
          <div className="sb">
            <p className="say">{SOURCE[inc.source] ?? "A feed"} at {hhmm(inc.started_at)}. {ag ? "The agent found every shipment it touches." : "Code matched every shipment it touches."}</p>
            <div className="art caught">
              <div className="facts">
                <p className="detail">{inc.detail}</p>
                {m.shipments > 0 ? (
                  <>
                    <span className="bigrow">
                      <button type="button" className="big num" aria-expanded={shipsOpen} aria-controls={`ships-${inc.id}`} onClick={() => setShipsOpen((o) => !o)}>
                        {chev}{plural(m.shipments, "shipment")}
                      </button>
                      <span className="num">{rs(m.value_inr)} of goods</span>
                    </span>
                    {lane && <span>{lane}</span>}
                    {car && <span>{b.chain.carriers.length > 1 ? "Most with " : "All with "}{car.name}<span className="num nw">{desk}</span></span>}
                  </>
                ) : (
                  <>
                    <span className="big">No shipments affected now</span>
                    <span>The fix moved them out of the incident&apos;s way.</span>
                  </>
                )}
              </div>
              <div className="map-wrap">
                <LocatorMap brief={b} network={network} hiCust={hi.cust} driverHidden={me?.field_access.driver_contact === "hidden"} />
                {ships.some((s) => s.last_lat != null) && <p className="map-note">Point at a truck for its {me?.field_access.driver_contact === "hidden" ? "shipment" : "driver"}, a warehouse for its lanes.</p>}
              </div>
            </div>
            {shipsOpen && ships.length > 0 && (
              <div id={`ships-${inc.id}`}>
                <ShipTable rows={shipsAll ? ships : ships.slice(0, SHIPS_FIRST)} network={network} />
                {ships.length > SHIPS_FIRST && !shipsAll && (
                  <p className="showall"><button type="button" className="quiet tog" aria-expanded="false" onClick={() => setShipsAll(true)}>{chev}Show all {plural(ships.length, "shipment")}</button></p>
                )}
              </div>
            )}
          </div>
        </li>

        <li className={stage(2)}>
          <div className="rail"><div className="rt"><span className="n">2</span><h2>Priced</h2></div>{by(2)}</div>
          <div className="sb">
            <div className="sayrow">
              <p className="say">
                {custs.length
                  ? <>{done ? "Before the fix, each" : "Each"} penalty comes from the customer&apos;s own contract{late > 0 ? `: up to ${plural(late, "day")} late.` : m.fill_inr > 0 ? ": all of it for short-filled orders." : "."}</>
                  : "No penalty is left to price for these shipments."}
              </p>
              {custs.length > 0 && <Badge>Priced by code, not the model</Badge>}
            </div>
            {custs.length > 0 && (
              <div className="art">
                <div className="cust">
                  {shown.map((c) => {
                    const isOpen = !!openCust[c.customer], rows = ships.filter((s) => s.customer === c.customer);
                    return (
                      <div className={`c ${isOpen ? "open" : ""} ${hi.cust === c.customer ? "hot" : ""}`} key={c.customer}
                        onMouseEnter={() => setHi({ cust: c.customer })} onMouseLeave={() => setHi({ cust: null })}>
                        <button type="button" className="cb" aria-expanded={isOpen} onFocus={() => setHi({ cust: c.customer })} onBlur={() => setHi({ cust: null })}
                          onClick={() => setOpenCust((o) => ({ ...o, [c.customer]: !o[c.customer] }))}>
                          <span className="row"><span>{chev}{c.customer}</span><span className={`num litable ${litMoney ? "lit" : ""}`}>{rs(amt(c))}</span></span>
                          <span className="bar" style={{ display: "block" }}><i style={{ width: `${((amt(c) / cmax) * 100).toFixed(1)}%` }} /></span>
                        </button>
                        {isOpen && (
                          <div className="tw">
                            <table className="tbl">
                              <thead><tr><th>Shipment</th><th className="r">Days late</th><th className="r">Penalty</th><th>Clause</th><th>Waived</th></tr></thead>
                              <tbody>
                                {rows.map((s) => (
                                  <tr key={s.id}>
                                    <td>{s.id}</td><td className="r num">{s.days_late}</td>
                                    <td className="r num">{rs(s.waived ? s.gross_penalty_inr : s.net_penalty_inr)}</td>
                                    <td>{secOf(s.clauses)}</td><td>{s.waived ? "Yes" : "No"}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </div>
                    );
                  })}
                  {restN > 0 && restAmt >= 1 && (
                    <div className="c rest"><div className="row"><span>{plural(restN, "more customer")}</span><span className={`num litable ${litMoney ? "lit" : ""}`}>{rs(restAmt)}</span></div></div>
                  )}
                </div>
                {m.unverified_rows > 0 && (
                  <p className="health-line">
                    {plural(m.unverified_rows, "shipment")} priced from a contract figure that failed a source check.{" "}
                    {(() => {
                      const src = m.unverified_sources?.find((x) => x.dead_letter_id != null);
                      return src
                        ? <Link className="quiet" href={`/data-health?record=${src.dead_letter_id}`}>See the held-back record ({src.chain}, {plain(src.clause_id, network)})</Link>
                        : <>Asha can open the held-back record in <Link className="quiet" href="/data-health">data health</Link>.</>;
                    })()}
                  </p>
                )}
              </div>
            )}
          </div>
        </li>

        <li className={stage(3)}>
          <div className="rail"><div className="rt"><span className="n">3</span><h2>Contract</h2></div>{by(3)}</div>
          <div className="sb">
            <p className="say">
              {cl.length
                ? `${ag ? "The agent quoted" : "The fixes quote"} ${listWords(chains.map((c) => `${c}'s`))} ${chains.length > 1 ? "contracts" : "contract"}; code checked ${cl.length > 1 ? "each quote" : "the quote"} against the PDF.`
                : "No contract clause is quoted for this case yet."}
            </p>
            {cl.length > 0 && (
              <div className="art clauses">
                {cl.map((c, i) => {
                  const check = c.verified
                    ? <Badge>Matches the PDF</Badge>
                    : <span className="warnword">Differs from the contract master; the PDF figure is used</span>;
                  const quote = <blockquote>&ldquo;{c.text}&rdquo;</blockquote>;
                  const d = clauseDrives(c.clause_id);
                  const on = { onMouseEnter: () => setHi({ clause: c.clause_id }), onMouseLeave: () => setHi({ clause: null }), onFocus: () => setHi({ clause: c.clause_id }), onBlur: () => setHi({ clause: null }) };
                  if (i > 0) return null;
                  const others = cl.slice(1);
                  return (
                    <div className="clause lead" key={c.chain + c.clause_id} tabIndex={0} aria-label={`Quoted clause, section ${sec(c.clause_id)}. ${d.what}`} {...on}>
                      {quote}
                      <cite>
                        <span>{c.heading}, {c.chain}, <PdfLink chain={c.chain} clauseId={c.clause_id} page={c.page} /></span>{check}{d.what && <span className="drives">{d.what}</span>}
                      </cite>
                      {others.map((o) => (
                        <button type="button" key={o.chain + o.clause_id} className="quiet tog also" aria-expanded={!!alsoOpen[o.clause_id]}
                          onClick={(e) => { e.stopPropagation(); setAlsoOpen((x) => ({ ...x, [o.clause_id]: !x[o.clause_id] })); }}
                          onMouseEnter={() => setHi({ clause: o.clause_id })} onMouseLeave={() => setHi({ clause: c.clause_id })}
                          onFocus={() => setHi({ clause: o.clause_id })}>
                          {chev}<span>Also quoted: section {sec(o.clause_id)}</span>{o.verified && <Badge>Matches the PDF</Badge>}
                        </button>
                      ))}
                      {others.filter((o) => alsoOpen[o.clause_id]).map((o) => (
                        <div className="also-q" key={o.chain + o.clause_id}>
                          <blockquote>&ldquo;{o.text}&rdquo;</blockquote>
                          <cite><span>{o.heading}, {o.chain}, <PdfLink chain={o.chain} clauseId={o.clause_id} page={o.page} /></span>
                            {o.verified ? <Badge>Matches the PDF</Badge> : <span className="warnword">Differs from the contract master; the PDF figure is used</span>}
                            {clauseDrives(o.clause_id).what && <span className="drives">{clauseDrives(o.clause_id).what}</span>}</cite>
                        </div>
                      ))}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </li>

        <li className={stage(4)}>
          <div className="rail"><div className="rt"><span className="n">4</span><h2>Decision</h2></div>{by(4)}</div>
          <div className="sb">
            <p className="say">
              <span>
                {!ag
                  ? (ps.length ? `The agent has not run as ${person} yet. These fixes come from ${fromRun}'s run; code priced each one.` : `The agent has not run as ${person} yet, so there is no fix to approve.`)
                  : fb ? fallbackWhy(ag) : `The agent proposed ${plural(ps.length, "fix", "fixes")}; code priced each one.`}
              </span>
              {ag && !running && (
                <> <button type="button" className="quiet tog trace-btn" aria-expanded={traceOpen} aria-controls="trace" onClick={onTrace}>{chev}{traceOpen ? "Hide trace" : "Show trace"}</button></>
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

