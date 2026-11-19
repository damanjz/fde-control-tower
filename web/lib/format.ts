// Formatting: rupees the Indian way (Rs 1,73,622 in full, Rs 1.74 L short), times on the scenario clock in IST.
// Written by hand rather than with Intl, so the server render and the browser render give the same string.

/** 173622.33 -> "1,73,622": Indian digit grouping, rounded to the rupee. */
export function group(v: number): string {
  const n = Math.round(Math.abs(v));
  const s = String(n);
  const last3 = s.slice(-3), rest = s.slice(0, -3);
  const body = rest ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + last3 : last3;
  return (v < 0 && n > 0 ? "-" : "") + body;
}

/** Rs 1,73,622 (non-breaking spaces: a figure never splits across lines) */
const NB = String.fromCharCode(160);   // a non-breaking space
export const rs = (v: number) => `Rs${NB}${group(v)}`;

/** Rs 1.74 L, Rs 13.87 Cr, Rs 55,181 (under a lakh stays in full). */
export function rsShort(v: number): string {
  if (Math.abs(v) >= 1e7) return `Rs${NB}${(v / 1e7).toFixed(2)}${NB}Cr`;
  if (Math.abs(v) >= 1e5) return `Rs${NB}${(v / 1e5).toFixed(2)}${NB}L`;
  return rs(v);
}

/** The approval limit in words: Rs 5 L. */
export function rsLimit(v: number): string {
  const l = v / 1e5;
  return Number.isInteger(l) ? `Rs${NB}${l}${NB}L` : rsShort(v);
}

const IST_MS = 330 * 60_000;
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad2 = (n: number) => String(n).padStart(2, "0");
const ist = (t: string | number) => new Date((typeof t === "number" ? t : Date.parse(t)) + IST_MS);

/** 10:40 (IST) */
export function hhmm(t: string | number): string {
  const d = ist(t);
  return `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}

/** Fri 13 Nov 2026, 10:40 IST */
export function stamp(t: number): string {
  const d = ist(t);
  return `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${hhmm(t)} IST`;
}

/** A countdown, always h:mm:ss (0:29:57, 2:49:57), so its width never changes as it runs. */
export function countdown(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return `${h}:${pad2(m)}:${pad2(ss)}`;
}

/** "12 shipments", "1 shipment"; Indian grouping on the number. */
export const plural = (n: number, one: string, many = one + "s") => `${group(n)} ${n === 1 ? one : many}`;

/** "a and b", "a, b and c" */
export const listWords = (a: string[]) => (a.length <= 2 ? a.join(" and ") : a.slice(0, -1).join(", ") + " and " + a[a.length - 1]);

export const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** "Delhi NCR (Gurugram)" -> "Delhi NCR" */
export const cityOf = (city: string) => city.replace(/\s*\(.*\)/, "");

/** Seconds, one decimal: 91.5 s; under a second in ms. */
export const dur = (ms: number) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`);
