"use client";
// The locator in "Caught": the warehouses, the destination cities and the affected trucks at their last GPS position,
// cropped from the India outline, with the event's radius or the supplier's hub. Pure SVG from /network and the brief.
// Trucks show a tooltip on hover or focus (driver and phone exactly as the role may see them); a warehouse click
// highlights its lanes; a customer highlighted in "Priced" lights its trucks here.
import { useState, type KeyboardEvent } from "react";
import { INDIA } from "@/lib/india";
import { cityOf, plural, rs } from "@/lib/format";
import type { Brief, BriefShipment, Network } from "@/lib/types";
import { driverLine } from "@/lib/words";

const W = 342, H = 100;   // the same aspect as the .map box (26rem x 7.6rem)

type Tip = { s: BriefShipment; x: number; y: number } | null;

export default function LocatorMap({ brief, network, hiCust, driverHidden = false }: { brief: Brief; network: Network | null; hiCust: string | null; driverHidden?: boolean }) {
  const [dc, setDc] = useState<string | null>(null);
  const [tip, setTip] = useState<Tip>(null);
  if (!network) return null;
  const inc = brief.incident;
  const whs = brief.chain.warehouses.map((w) => network.warehouses.find((x) => x.id === w.id)).filter((x) => !!x);
  const cities = brief.chain.cities.map((c) => network.zones.find((z) => z.id === c.id)).filter((x) => !!x);
  const hub = inc.supplier_id ? network.suppliers.find((s) => s.id === inc.supplier_id) ?? null : null;
  const trucks = brief.shipments.items.filter((s) => s.last_lat != null && s.last_lon != null);
  const pts: [number, number][] = [...whs, ...cities].map((p) => [p.lon, p.lat]);
  for (const t of trucks) pts.push([t.last_lon!, t.last_lat!]);
  if (inc.lat != null && inc.lon != null) pts.push([inc.lon, inc.lat]);
  if (hub) pts.push([hub.lon, hub.lat]);
  if (!pts.length) return null;

  const x0 = Math.min(...pts.map((p) => p[0])), x1 = Math.max(...pts.map((p) => p[0]));
  const y0 = Math.min(...pts.map((p) => p[1])), y1 = Math.max(...pts.map((p) => p[1]));
  const kx = Math.cos(((y0 + y1) / 2) * Math.PI / 180), pad = 1.4, padY = 2.1;   // extra room above and below for labels
  const box = Math.max(((x1 - x0) * kx + pad * 2) / W, (y1 - y0 + padY * 2) / H, 5 / H);
  const cx = ((x0 + x1) / 2) * kx, cy = (y0 + y1) / 2;
  const P = (lon: number, lat: number): [number, number] => [W / 2 + (lon * kx - cx) / box, H / 2 - (lat - cy) / box];
  const f = (v: number) => v.toFixed(1);
  const land = INDIA.map((ring) => "M" + ring.map((q) => P(q[0], q[1]).map(f).join(",")).join("L") + "Z").join("");
  // labels go on the side of their dot with the fewest other marks under them (trucks cluster along the lanes)
  const marks: [number, number][] = [...trucks.map((t) => P(t.last_lon!, t.last_lat!)), ...cities.map((z) => P(z.lon, z.lat)), ...whs.map((w) => P(w.lon, w.lat))];
  const placed: { x0: number; y0: number; x1: number; y1: number }[] = [];
  const label = (x: number, y: number, t: string) => {
    const w = t.length * 8.4, h = 16;
    const options: [number, number, "start" | "end" | "middle"][] = [[x + 9, y + 5, "start"], [x - 9, y + 5, "end"], [x, y - 10, "middle"], [x, y + 19, "middle"]];
    const box = ([lx, ly, a]: (typeof options)[number]) => {
      const x0 = a === "start" ? lx : a === "end" ? lx - w : lx - w / 2;
      return { x0, y0: ly - h + 4, x1: x0 + w, y1: ly + 4 };
    };
    const cost = (o: (typeof options)[number]) => {
      const b = box(o);
      const out = b.x0 < 2 || b.x1 > W - 2 || b.y0 < 2 || b.y1 > H - 2 ? 100 : 0;
      const hits = marks.filter(([mx, my]) => !(mx === x && my === y) && mx > b.x0 - 4 && mx < b.x1 + 4 && my > b.y0 - 4 && my < b.y1 + 4).length;
      const over = placed.some((p) => b.x0 < p.x1 && b.x1 > p.x0 && b.y0 < p.y1 && b.y1 > p.y0) ? 50 : 0;
      return out + hits * 10 + over;
    };
    const best = options.reduce((a, o) => (cost(o) < cost(a) ? o : a), options[0]);
    placed.push(box(best));
    return <text key={`l-${t}`} x={f(best[0])} y={f(best[1])} textAnchor={best[2]}>{t}</text>;
  };
  const toggleDc = (id: string) => setDc((d) => (d === id ? null : id));
  const onDcKey = (e: KeyboardEvent, id: string) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDc(id); } };
  const show = (s: BriefShipment) => { const [x, y] = P(s.last_lon!, s.last_lat!); setTip({ s, x, y }); };

  return (
    <div className="map">
      <svg viewBox={`0 0 ${W} ${H}`} role="group" aria-label={`Map: ${whs.map((w) => cityOf(w.city)).join(", ")} to ${cities.length === 1 ? cities[0].city : `${cities.length} cities`}, ${plural(trucks.length, "truck")}`}>
        <path d={land} fill="var(--surface)" stroke="var(--line2)" strokeWidth="1" />
        {inc.lat != null && inc.lon != null && inc.radius_km ? (() => {
          const [a, c] = P(inc.lon, inc.lat);
          return <circle cx={f(a)} cy={f(c)} r={f(Math.max(7, inc.radius_km / 111 / box))} fill="var(--area)" stroke="var(--mut)" strokeDasharray="3 3" />;
        })() : null}
        {whs.flatMap((w) => cities.filter((z) => whs.length === 1 || z.serving_dc === w.id).map((z) => {
          const [a, c] = P(w.lon, w.lat), [d, e] = P(z.lon, z.lat);
          const cls = dc == null ? "" : dc === w.id ? "on" : "dim";
          return <line key={`${w.id}-${z.id}`} className={`lane ${cls}`} x1={f(a)} y1={f(c)} x2={f(d)} y2={f(e)} stroke="var(--edge)" strokeWidth="1.5" opacity=".8" />;
        }))}
        {hub && (() => { const [a, c] = P(hub.lon, hub.lat); return <rect x={f(a - 4)} y={f(c - 4)} width="8" height="8" fill="var(--soft)" />; })()}
        {cities.map((z) => { const [a, c] = P(z.lon, z.lat); return <circle key={z.id} cx={f(a)} cy={f(c)} r="3" fill="var(--soft)" />; })}
        {trucks.map((s) => {
          const [a, c] = P(s.last_lon!, s.last_lat!);
          const hot = hiCust === s.customer, dim = (hiCust != null && !hot) || (dc != null && s.dc_id !== dc);
          return (
            <g key={s.id} className={`truck ${hot ? "hot" : ""} ${dim ? "dim" : ""}`} tabIndex={0} role="img"
              aria-label={`Truck for ${s.customer}, ${rs(s.value_inr)} of goods`}
              onMouseEnter={() => show(s)} onMouseLeave={() => setTip(null)} onFocus={() => show(s)} onBlur={() => setTip(null)}>
              <circle cx={f(a)} cy={f(c)} r={hot ? 5.5 : 4} />
            </g>
          );
        })}
        {whs.map((w) => {
          const [a, c] = P(w.lon, w.lat);
          return (
            <g key={w.id} className="dc" tabIndex={0} role="button" aria-pressed={dc === w.id} aria-label={`${cityOf(w.city)} warehouse: highlight its lanes`}
              onClick={() => toggleDc(w.id)} onKeyDown={(e) => onDcKey(e, w.id)}>
              <title>{`${cityOf(w.city)} warehouse: click to highlight its lanes`}</title>
              <circle className="hit" cx={f(a)} cy={f(c)} r="12" />
              <circle className="dot" cx={f(a)} cy={f(c)} r="5.5" fill="var(--ink)" />
            </g>
          );
        })}
        {whs.map((w) => { const [a, c] = P(w.lon, w.lat); return label(a, c, cityOf(w.city)); })}
        {cities.length === 1 && (() => { const [a, c] = P(cities[0].lon, cities[0].lat); return label(a, c, cities[0].city); })()}
      </svg>
      {tip && (() => {
        const d = driverLine(tip.s);
        // above the truck unless that would cover a label (or leave the map); then below
        const coversLabel = placed.some((p) => p.y1 < tip.y && p.y1 > tip.y - 60 && p.x1 > tip.x - 90 && p.x0 < tip.x + 90);
        const below = tip.y < H * 0.4 || coversLabel;
        const cls = [below ? "below" : "", tip.x < W * 0.3 ? "l" : tip.x > W * 0.7 ? "r" : ""].join(" ");
        return (
          <div className={`tip ${cls}`} role="tooltip" style={{ left: `${(tip.x / W) * 100}%`, top: `${(tip.y / H) * 100}%` }}>
            <b>{tip.s.customer}</b><br />
            {rs(tip.s.value_inr)} of goods{tip.s.days_late > 0 ? `, ${plural(tip.s.days_late, "day")} late` : ""}<br />
            {driverHidden ? `Shipment ${tip.s.id}, ${tip.s.zone_city}` : d.line}
          </div>
        );
      })()}
    </div>
  );
}
