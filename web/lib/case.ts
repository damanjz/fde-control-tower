// The case logic both the server page and the browser use: which incident opens, what is recommended, what is done.
import type { Brief, Done, Incident, Playbook } from "./types";

export const net = (p: Playbook) => p.est_saving_inr - p.est_cost_inr;

/** The recommendation: the fix that saves the most after its own cost (ties: the first proposed). */
export const best = (ps: Playbook[]): Playbook | null =>
  ps.length ? ps.reduce((a, p) => (net(p) > net(a) ? p : a), ps[0]) : null;

/** Recommended first, then by what each saves after its cost. */
export function ordered(ps: Playbook[]): Playbook[] {
  const top = best(ps);
  return ps.slice().sort((a, b) => (b === top ? 1 : 0) - (a === top ? 1 : 0) || net(b) - net(a) || a.id - b.id);
}

/** Money avoided by an executed fix: the exposure before it minus after it, from the run's own result. */
export function savedBy(p: Playbook): number {
  const r = p.result;
  if (!r || r.exposure_before_inr == null) return 0;
  return Math.max(0, Number(r.exposure_before_inr) - Number(r.exposure_after_inr ?? 0));
}

/** A case's size in the strip: its priced exposure, or for a handled case the exposure before the fix ran (a carrier
 *  swap moves the shipments out of the incident, so its own gross drops to nothing afterwards). */
export const weight = (i: Incident, done: Record<number, Done>) =>
  Math.max(i.gross_inr, Number(done[i.id]?.pb.result?.exposure_before_inr ?? 0));

/** The strip's order: largest priced exposure first, so a handled case keeps its place. */
export const stripOrder = (xs: Incident[], done: Record<number, Done>) =>
  xs.slice().sort((a, b) => weight(b, done) - weight(a, done) || a.id - b.id);

/** A deadline is running while it is in the future and something can still be waived. */
export const deadlineRunning = (i: { notify_deadline: string | null; waivable_inr: number }, now: number) =>
  !!i.notify_deadline && Date.parse(i.notify_deadline) > now && i.waivable_inr > 0;

/** The case to open with no ?incident: the earliest running notify deadline, else the largest open exposure. */
export function mostUrgent(xs: Incident[], done: Record<number, Done>, now: number): number | null {
  const open = xs.filter((i) => !done[i.id]);
  const running = open.filter((i) => deadlineRunning(i, now)).sort((a, b) => Date.parse(a.notify_deadline!) - Date.parse(b.notify_deadline!));
  if (running.length) return running[0].id;
  const pool = open.length ? open : xs;
  return pool.length ? pool.reduce((a, i) => (i.net_inr > a.net_inr ? i : a), pool[0]).id : null;
}

/** The fix to show selected: one waiting for approval, else the executed one, else the recommendation. */
export function defaultPick(b: Brief): number | null {
  const ps = b.playbooks;
  return (ps.find((p) => p.status === "pending_approval") ?? ps.find((p) => p.status === "executed") ?? best(ps))?.id ?? null;
}

/** The case to offer next: the most urgent open one other than this (same rule as the first open). */
export function nextOpen(xs: Incident[], from: number, done: Record<number, Done>, now: number): Incident | null {
  const open = xs.filter((i) => i.id !== from && !done[i.id]);
  const id = mostUrgent(open, done, now);
  return open.find((i) => i.id === id) ?? null;
}

/** How far a replay of the recorded run has got: stages 1 to 3 open as their first step is shown, stage 4 (the
 *  decision) only once the last step is. `shown` null means no replay is running: everything is open. */
export function revealed(steps: { tool: string }[], shown: number | null, stageOf: Record<string, number>): (stage: number) => boolean {
  if (shown == null) return () => true;
  const upTo = steps.slice(0, shown).reduce((m, s) => Math.max(m, stageOf[s.tool] ?? 1), 0);
  return (k) => (k >= 4 ? shown >= steps.length : k <= upTo);
}
