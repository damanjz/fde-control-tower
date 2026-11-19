// The small locator in "Caught": the warehouses and destination cities of the affected shipments, cropped from the
// India outline, with the event's radius or the supplier's hub. Pure SVG from /network and the brief's chain.
import { INDIA } from "@/lib/india";
import { cityOf } from "@/lib/format";
import type { Brief, Network } from "@/lib/types";

const W = 216, H = 104;

export default function LocatorMap({ brief, network }: { brief: Brief; network: Network | null }) {
  if (!network) return null;
  const inc = brief.incident;
  const whs = brief.chain.warehouses.map((w) => network.warehouses.find((x) => x.id === w.id)).filter((x) => !!x);
  const cities = brief.chain.cities.map((c) => network.zones.find((z) => z.id === c.id)).filter((x) => !!x);
  const hub = inc.supplier_id ? network.suppliers.find((s) => s.id === inc.supplier_id) ?? null : null;
  const pts: [number, number][] = [...whs, ...cities].map((p) => [p.lon, p.lat]);
  if (inc.lat != null && inc.lon != null) pts.push([inc.lon, inc.lat]);
  if (hub) pts.push([hub.lon, hub.lat]);
  if (!pts.length) return null;

  const x0 = Math.min(...pts.map((p) => p[0])), x1 = Math.max(...pts.map((p) => p[0]));
  const y0 = Math.min(...pts.map((p) => p[1])), y1 = Math.max(...pts.map((p) => p[1]));
  const kx = Math.cos(((y0 + y1) / 2) * Math.PI / 180), pad = 1.4;
  const box = Math.max(((x1 - x0) * kx + pad * 2) / W, (y1 - y0 + pad * 2) / H, 5 / H);
  const cx = ((x0 + x1) / 2) * kx, cy = (y0 + y1) / 2;
  const P = (lon: number, lat: number): [number, number] => [W / 2 + (lon * kx - cx) / box, H / 2 - (lat - cy) / box];
  const f = (v: number) => v.toFixed(1);
  const land = INDIA.map((ring) => "M" + ring.map((q) => P(q[0], q[1]).map(f).join(",")).join("L") + "Z").join("");
  const label = (x: number, y: number, t: string) => {
    const right = x > W - 70;
    return <text key={`l-${t}`} x={f(right ? x - 8 : x + 8)} y={f(y + 4)} textAnchor={right ? "end" : "start"}>{t}</text>;
  };

  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Map: ${whs.map((w) => cityOf(w.city)).join(", ")} to ${cities.length === 1 ? cities[0].city : `${cities.length} cities`}`}>
      <path d={land} fill="var(--surface)" stroke="var(--line2)" strokeWidth="1" />
      {inc.lat != null && inc.lon != null && inc.radius_km ? (() => {
        const [a, c] = P(inc.lon, inc.lat);
        return <circle cx={f(a)} cy={f(c)} r={f(Math.max(6, inc.radius_km / 111 / box))} fill="var(--risk-wash)" stroke="var(--risk)" strokeDasharray="3 3" />;
      })() : null}
      {whs.flatMap((w) => cities.filter((z) => whs.length === 1 || z.serving_dc === w.id).map((z) => {
        const [a, c] = P(w.lon, w.lat), [d, e] = P(z.lon, z.lat);
        return <line key={`${w.id}-${z.id}`} x1={f(a)} y1={f(c)} x2={f(d)} y2={f(e)} stroke="var(--risk)" strokeWidth="1.5" opacity=".55" />;
      }))}
      {hub && (() => { const [a, c] = P(hub.lon, hub.lat); return <rect x={f(a - 4)} y={f(c - 4)} width="8" height="8" fill="var(--risk)" />; })()}
      {cities.map((z) => { const [a, c] = P(z.lon, z.lat); return <circle key={z.id} cx={f(a)} cy={f(c)} r="3" fill="var(--risk)" />; })}
      {whs.map((w) => { const [a, c] = P(w.lon, w.lat); return <circle key={w.id} cx={f(a)} cy={f(c)} r="4.5" fill="var(--ink)" />; })}
      {whs.map((w) => { const [a, c] = P(w.lon, w.lat); return label(a, c, cityOf(w.city)); })}
      {cities.length === 1 && (() => { const [a, c] = P(cities[0].lon, cities[0].lat); return label(a, c, cities[0].city); })()}
    </svg>
  );
}
