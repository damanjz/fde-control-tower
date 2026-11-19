// Server-side loading for the pages: the role from the cookie, and the first paint's data from the tower (no empty
// frame). The browser then keeps it fresh through the /api proxy.
import { cookies } from "next/headers";
import { ROLE_COOKIE, isRole, type RoleId } from "@/lib/roles";
import { getJson } from "@/lib/server/tower";
import { savedBy } from "@/lib/case";
import type { Done, Live, Me, Network, Playbook } from "@/lib/types";

export async function currentRole(): Promise<RoleId> {
  const asked = (await cookies()).get(ROLE_COOKIE)?.value;
  return isRole(asked) ? asked : "asha";
}

/** The scenario clock as the tower reports it, and the server's wall time at that moment (the browser ticks from it). */
export type ClockSeed = { now: number; wall: number };
export const wallNow = () => Date.now();

/** What every page needs: who I am, the network (names, masked phones), the scenario clock and the executed plans. */
export async function loadFrame(role: RoleId) {
  const [me, network, live] = await Promise.all([
    getJson<Me>(role, "/me"), getJson<Network>(role, "/network"), getJson<Live>(role, "/live"),
  ]);
  const wall = wallNow();
  const clock: ClockSeed = { now: live.data ? Date.parse(live.data.now) : wall, wall };
  return { me: me.data, network: network.data, live: live.data, clock, down: live.status === 502 || live.status === 0 };
}

/** Executed fixes per incident, with the money they avoided (GET /playbooks/{id} carries the run's own result). */
export async function loadDone(role: RoleId, live: Live | null): Promise<Record<number, Done>> {
  const plans = Object.values(live?.plans ?? {});
  const rows = await Promise.all(plans.map((p) => getJson<Playbook>(role, `/playbooks/${p.id}`)));
  const out: Record<number, Done> = {};
  for (const r of rows) if (r.data) out[r.data.incident_id] = { pb: r.data, saved: savedBy(r.data) };
  return out;
}
