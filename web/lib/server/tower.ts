// Server-side only: the bearer tokens from the project .env and the HTTP call to the tower.
// Imported by the route handler and the page (both run on the server); never by a client component.
import { readFileSync, statSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import type { RoleId } from "@/lib/roles";

const ENV_FILE = process.env.CT_ENV_FILE || path.resolve(process.cwd(), "..", ".env");
let cache: { mtime: number; vars: Record<string, string> } | null = null;

function envVars(): Record<string, string> {
  try {
    const mtime = statSync(/*turbopackIgnore: true*/ ENV_FILE).mtimeMs;   // runtime only: never traced into the build
    if (cache && cache.mtime === mtime) return cache.vars;
    const vars: Record<string, string> = {};
    for (const line of readFileSync(/*turbopackIgnore: true*/ ENV_FILE, "utf8").split(/\r?\n/)) {
      const m = /^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/.exec(line);
      if (m && !line.trimStart().startsWith("#")) vars[m[1]] = m[2].replace(/^(["'])(.*)\1$/, "$2");
    }
    cache = { mtime, vars };
    return vars;
  } catch {
    return {};
  }
}

export function tokenFor(role: RoleId): string | undefined {
  const key = `CT_TOKEN_${role.toUpperCase()}`;
  return process.env[key] || envVars()[key];
}

export function towerBase(): string {
  return process.env.CT_TOWER_URL || `http://127.0.0.1:${envVars().CT_PORT || 8500}`;
}

export type TowerReply = { status: number; body: string };
export type RawReply = { status: number; body: Buffer; type: string };

/** One call to the tower as `role`. node:http rather than fetch: a triage can take minutes, longer than fetch's
 *  default header timeout. A fresh connection per call (agent: false): uvicorn closes idle keep-alive sockets after
 *  5 s, and reusing one it has just closed resets the request. A GET that still fails on the connection is retried
 *  once. A tower that does not answer comes back as a 502 with a plain reason. */
export async function callTower(role: RoleId, method: string, pathAndQuery: string, body?: string, timeoutMs = 30_000): Promise<TowerReply> {
  const r = await callTowerRaw(role, method, pathAndQuery, body, timeoutMs);
  return { status: r.status, body: r.body.toString("utf8") };
}

/** The same call, keeping the bytes and the content type (a contract PDF). */
export async function callTowerRaw(role: RoleId, method: string, pathAndQuery: string, body?: string, timeoutMs = 30_000): Promise<RawReply> {
  const first = await once(role, method, pathAndQuery, body, timeoutMs);
  if (first.status === 502 && first.retry && method === "GET") return once(role, method, pathAndQuery, body, timeoutMs);
  return first;
}

const json = (status: number, detail: string, retry?: boolean): RawReply & { retry?: boolean } =>
  ({ status, body: Buffer.from(JSON.stringify({ detail })), type: "application/json", retry });

function once(role: RoleId, method: string, pathAndQuery: string, body: string | undefined, timeoutMs: number): Promise<RawReply & { retry?: boolean }> {
  const token = tokenFor(role);
  if (!token) return Promise.resolve(json(500, `no CT_TOKEN_${role.toUpperCase()} in the project .env`));
  const url = new URL(pathAndQuery, towerBase());
  return new Promise((resolve) => {
    const req = http.request(url, {
      method,
      agent: false,
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json", ...(body ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) } : {}) },
    }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (c: Buffer) => chunks.push(c));
      res.on("end", () => resolve({ status: res.statusCode ?? 502, body: Buffer.concat(chunks), type: String(res.headers["content-type"] ?? "application/json") }));
      res.on("error", () => resolve(json(502, "the tower closed the connection")));
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error("timeout")));
    req.on("error", (e) => resolve(json(502, e.message === "timeout" ? "the tower did not answer in time" : "the tower is not answering", e.message !== "timeout")));
    if (body) req.write(body);
    req.end();
  });
}

export async function getJson<T>(role: RoleId, p: string): Promise<{ status: number; data: T | null }> {
  const r = await callTower(role, "GET", p);
  try {
    return { status: r.status, data: r.status < 300 ? (JSON.parse(r.body) as T) : null };
  } catch {
    return { status: 502, data: null };
  }
}
