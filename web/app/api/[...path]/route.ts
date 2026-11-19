// The browser's only way to the tower: /api/<path> -> http://127.0.0.1:8500/<path>, with the bearer token of the
// selected role attached here on the server. Only the endpoints the UI uses pass.
// A refusal (401, 403, 404, 409, 422) is an expected answer for a role, not a failure of the page: it comes back with
// HTTP 200 and the tower's own status in X-Tower-Status, so the browser does not log it as a network error. The UI
// reads that header (lib/client.ts) and shows the state the tower meant.
import type { NextRequest } from "next/server";
import { ROLE_COOKIE, ROLE_HEADER, STATUS_HEADER, isRole, type RoleId } from "@/lib/roles";
import { callTower, callTowerRaw } from "@/lib/server/tower";

export const dynamic = "force-dynamic";

const ALLOWED: [string, RegExp][] = [
  ["GET", /^me$/], ["GET", /^live$/], ["GET", /^network$/], ["GET", /^incidents$/], ["GET", /^incidents\/\d+\/brief$/],
  ["POST", /^incidents\/\d+\/triage$/], ["GET", /^playbooks\/\d+$/], ["POST", /^playbooks\/\d+\/(run|approve)$/], ["GET", /^audit$/],
  ["GET", /^health\/feeds$/], ["GET", /^dead-letters$/], ["POST", /^dead-letters\/\d+\/(replay|discard)$/],
  ["GET", /^outage$/], ["POST", /^outage$/], ["GET", /^contracts\/[^/]+\/pdf$/],
];

const PDF = /^contracts\/[^/]+\/pdf$/;
const REFUSED: Record<number, string> = { 403: "This role may not read that contract.", 404: "There is no such contract." };

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const p = (await ctx.params).path.join("/");
  if (!ALLOWED.some(([m, re]) => m === req.method && re.test(p))) return Response.json({ detail: "not found" }, { status: 404 });
  const asked = req.headers.get(ROLE_HEADER) ?? req.cookies.get(ROLE_COOKIE)?.value;
  const role: RoleId = isRole(asked) ? asked : "asha";
  if (PDF.test(p)) {   // a contract opened from a citation, in the browser's own PDF viewer
    const raw = await callTowerRaw(role, "GET", `/${p.split("/").map((x) => encodeURIComponent(decodeURIComponent(x))).join("/")}`, undefined, 60_000);
    if (raw.status !== 200) return new Response(REFUSED[raw.status] ?? "The tower did not answer.", { status: raw.status, headers: { "Content-Type": "text/plain; charset=utf-8" } });
    return new Response(new Uint8Array(raw.body), { status: 200, headers: { "Content-Type": "application/pdf", "Content-Disposition": "inline", "Cache-Control": "no-store" } });
  }
  const body = req.method === "POST" ? (await req.text()) || undefined : undefined;
  const slow = /triage$/.test(p) ? 15 * 60_000 : 60_000;
  const r = await callTower(role, req.method, `/${p}${req.nextUrl.search}`, body, slow);
  const refusal = r.status >= 400 && r.status < 500;
  return new Response(r.body, {
    status: refusal ? 200 : r.status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", [ROLE_HEADER]: role, [STATUS_HEADER]: String(r.status) },
  });
}

export { proxy as GET, proxy as POST };
