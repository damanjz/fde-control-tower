// The single case. The first paint comes from the server with real data (no empty frame), made with the selected
// role's token. ?incident=N opens that case; with none, the most urgent one opens (lib/case.ts mostUrgent). Every
// visible incident's brief is loaded up front, so switching cases is instant and the strip can show which ones wait
// for an approval.
import CaseView from "@/components/CaseView";
import Frame from "@/components/Frame";
import { mostUrgent } from "@/lib/case";
import { currentRole, loadDone, loadFrame } from "@/lib/server/load";
import { getJson } from "@/lib/server/tower";
import type { Brief, Incident } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function Page({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const role = await currentRole();
  const sp = await searchParams;
  const asked = Number(Array.isArray(sp.incident) ? sp.incident[0] : sp.incident) || null;
  const [frame, inc] = await Promise.all([loadFrame(role), getJson<{ items: Incident[] }>(role, "/incidents")]);
  const incidents = inc.data?.items ?? [];
  const [done, replies] = await Promise.all([
    loadDone(role, frame.live),
    Promise.all(incidents.map((i) => getJson<Brief>(role, `/incidents/${i.id}/brief?limit=200`))),
  ]);
  const briefs: Record<number, Brief> = {};
  replies.forEach((r, k) => { if (r.data) briefs[incidents[k].id] = r.data; });
  const visible = asked != null && incidents.some((i) => i.id === asked);
  const id = visible ? asked : mostUrgent(incidents, done, frame.clock.now);
  return (
    <Frame role={role} seed={frame.clock} page="incidents">
      <CaseView key={role} initial={{
        role, me: frame.me, network: frame.network, incidents, done, id, briefs, briefStatus: id != null && briefs[id] ? 200 : id != null ? 502 : 0,
        askedHidden: asked != null && !visible ? asked : null, down: frame.down,
      }} />
    </Frame>
  );
}
