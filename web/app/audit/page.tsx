// Audit: who did what, when, and the money involved. Written once, never edited.
import AuditView from "@/components/AuditView";
import Frame from "@/components/Frame";
import { currentRole, loadFrame } from "@/lib/server/load";
import { getJson } from "@/lib/server/tower";
import type { AuditRow } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata = { title: "Audit, Tarangini control tower" };

export default async function Page() {
  const role = await currentRole();
  const [frame, audit] = await Promise.all([loadFrame(role), getJson<{ total: number; items: AuditRow[] }>(role, "/audit?limit=300&morning=true")]);
  return (
    <Frame role={role} seed={frame.clock} page="audit">
      <AuditView key={role} initial={{ role, network: frame.network, rows: audit.data?.items ?? [], total: audit.data?.total ?? 0, ok: audit.status === 200 }} />
    </Frame>
  );
}
