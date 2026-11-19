// Browser side: every call goes through the Next proxy (/api/...), naming the role it is made for. The proxy attaches
// that role's token on the server; the browser never holds one.
import { ROLE_HEADER, STATUS_HEADER, type RoleId } from "./roles";

export type Reply<T> = { status: number; data: T | null; body: unknown; detail: string | null; role: RoleId };

export async function api<T>(role: RoleId, path: string, init?: { method?: "GET" | "POST"; body?: unknown }): Promise<Reply<T>> {
  const hasBody = init?.body !== undefined;
  try {
    const r = await fetch(`/api/${path}`, {
      method: init?.method ?? "GET",
      headers: { [ROLE_HEADER]: role, ...(hasBody ? { "Content-Type": "application/json" } : {}) },
      body: hasBody ? JSON.stringify(init!.body) : undefined,
      cache: "no-store",
    });
    let json: unknown = null;
    try { json = await r.json(); } catch { json = null; }
    const status = Number(r.headers.get(STATUS_HEADER)) || r.status;   // the tower's own status (see the proxy)
    const ok = status >= 200 && status < 300;
    const detail = ok ? null : ((json as { detail?: unknown })?.detail ? String((json as { detail: unknown }).detail) : `HTTP ${status}`);
    return { status, data: ok ? (json as T) : null, body: json, detail, role };
  } catch {
    return { status: 0, data: null, body: null, detail: "the UI server is not answering", role };
  }
}
