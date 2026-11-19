// Data health: every feed, the records that failed a check, and the Outage drill switch.
import Frame from "@/components/Frame";
import HealthView from "@/components/HealthView";
import { currentRole, loadFrame } from "@/lib/server/load";
import { getJson } from "@/lib/server/tower";
import type { DeadLetters, Feed, Outage } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata = { title: "Data health, Tarangini control tower" };

export default async function Page({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const role = await currentRole();
  const sp = await searchParams;
  const record = Number(Array.isArray(sp.record) ? sp.record[0] : sp.record) || null;
  const [frame, feeds, dead, outage] = await Promise.all([
    loadFrame(role), getJson<{ feeds: Feed[] }>(role, "/health/feeds"),
    getJson<DeadLetters>(role, "/dead-letters?status=waiting&limit=500"), getJson<Outage>(role, "/outage"),
  ]);
  return (
    <Frame role={role} seed={frame.clock} page="health">
      <HealthView key={role} initial={{ role, me: frame.me, network: frame.network, feeds: feeds.data?.feeds ?? [], dead: { status: dead.status, data: dead.data }, outage: outage.data, record }} />
    </Frame>
  );
}
