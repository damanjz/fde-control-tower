// Map projection and lanes, from the concept (.design/shared.js).
import type { Network } from "./types";

export type LatLon = { lat: number; lon: number };
export type Bounds = { lat0: number; lat1: number; lon0: number; lon1: number };
export type Proj = (lat: number, lon: number) => [number, number];
export type Lane = { d: string; at: (t: number) => [number, number] };

export function bounds(net: Network): Bounds {
  const pts: LatLon[] = [...net.warehouses, ...net.zones, ...net.suppliers];
  return {
    lat0: Math.min(...pts.map((p) => p.lat)) - 1.2, lat1: Math.max(...pts.map((p) => p.lat)) + 1.2,
    lon0: Math.min(...pts.map((p) => p.lon)) - 1.2, lon1: Math.max(...pts.map((p) => p.lon)) + 1.2,
  };
}

/** Equirectangular, longitude squeezed by cos 22 degrees, fitted into w x h with padding. */
export function projector(B: Bounds, w: number, h: number, pad = 24): Proj {
  const k = Math.cos((22 * Math.PI) / 180);
  const sx = (B.lon1 - B.lon0) * k, sy = B.lat1 - B.lat0;
  const s = Math.min((w - 2 * pad) / sx, (h - 2 * pad) / sy);
  const ox = (w - sx * s) / 2, oy = (h - sy * s) / 2;
  return (lat, lon) => [ox + (lon - B.lon0) * k * s, oy + (B.lat1 - lat) * s];
}

/** A lane: a gentle arc from the warehouse to the city. */
export function lanePath(P: Proj, a: LatLon, b: LatLon): Lane {
  const [x1, y1] = P(a.lat, a.lon), [x2, y2] = P(b.lat, b.lon);
  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2, dx = x2 - x1, dy = y2 - y1;
  const bend = 0.12, cx = mx - dy * bend, cy = my + dx * bend;
  return {
    d: `M${x1.toFixed(1)},${y1.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`,
    at: (t) => {
      const u = 1 - t;
      return [u * u * x1 + 2 * u * t * cx + t * t * x2, u * u * y1 + 2 * u * t * cy + t * t * y2];
    },
  };
}

/** How far along its lane a truck is: its last GPS fix projected onto the warehouse-to-city line, or, with no fix,
 *  the share of its planned transit time that has passed on the scenario clock. */
export function progress(dc: LatLon, city: LatLon, fix: LatLon | null, departed: string | null, eta: string | null, nowMs: number): number {
  const clamp = (t: number) => Math.max(0.02, Math.min(0.985, t));
  if (fix) {
    const k = Math.cos((22 * Math.PI) / 180);
    const ax = dc.lon * k, ay = dc.lat, bx = city.lon * k - ax, by = city.lat - ay;
    const len2 = bx * bx + by * by;
    if (len2 > 1e-9) return clamp(((fix.lon * k - ax) * bx + (fix.lat - ay) * by) / len2);
  }
  if (departed && eta) {
    const a = Date.parse(departed), b = Date.parse(eta);
    if (b > a) return clamp((nowMs - a) / (b - a));
  }
  return 0.5;
}
