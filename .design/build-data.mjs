// Builds data.js for the control-tower concepts from the Supply Chain project's CSVs.
// Network (warehouses, cities, suppliers, carriers, products) is real project data; the live state
// (shipments in transit, disruptions, exposures, health numbers) is simulated for the mockups, seeded.
// Run: node .design/build-data.mjs
import { readFileSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
// the Supply Chain project's raw CSVs (design time only): set SUPPLY_CHAIN_RAW
const RAW = process.env.SUPPLY_CHAIN_RAW ?? join(here, "../../supply-chain/data/raw");
const csv = (f) => {
  const [h, ...rows] = readFileSync(join(RAW, f), "utf8").trim().split(/\r?\n/);
  const k = h.split(",");
  return rows.map((r) => Object.fromEntries(r.split(",").map((v, i) => [k[i], v.trim()])));
};
let seed = 20261113;
const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
const pick = (a) => a[Math.floor(rnd() * a.length)];

const dcs = csv("warehouses.csv").map((d) => ({ id: d.dc_id, city: d.dc_city, lat: +d.lat, lon: +d.lon }));
const zones = csv("delivery_zones.csv").map((z) => ({ id: z.zone_id, city: z.city, state: z.state, lat: +z.lat, lon: +z.lon, dc: z.serving_dc, km: +z.road_km, days: +z.std_transit_days }));
const suppliers = csv("suppliers.csv").map((s) => ({ id: s.supplier_id, name: s.supplier.replace(/\s+/g, " ").trim().replace(/\b(\w)(\w*)/g, (m, a, b) => a + b.toLowerCase()), category: s.category, hub: s.hub, lat: +s.lat, lon: +s.lon, lead: +s.quoted_lead_days }));
const products = csv("products.csv").map((p) => ({ sku: p.sku, category: p.category, supplier: p.supplier_id, price: +p.unit_price, caseQty: +p.case_qty }));
const carriers = [
  { id: "C1", name: "Deccan Express Logistics", bank: "HDFC ****4471", contact: "+91 98490 22713" },
  { id: "C2", name: "Ganga Freight", bank: "SBI ****0938", contact: "+91 94150 61182" },
  { id: "C3", name: "Sahyadri Roadlines", bank: "ICICI ****5520", contact: "+91 98220 47306" },
];

// shipments in transit "now" (2026-11-13 10:40 IST): DC -> city, value from real product prices
const NOW = Date.parse("2026-11-13T10:40:00+05:30");
const shipments = [];
let n = 4812;
for (const z of zones) {
  const count = z.km < 60 ? 6 : 7 + Math.floor(rnd() * 8);
  for (let i = 0; i < count; i++) {
    const lines = Array.from({ length: 3 + Math.floor(rnd() * 5) }, () => {
      const p = pick(products);
      return { sku: p.sku, cases: 8 + Math.floor(rnd() * 40), value: 0, p };
    });
    lines.forEach((l) => (l.value = Math.round(l.cases * l.p.caseQty * l.p.price)));
    const value = lines.reduce((a, l) => a + l.value, 0);
    const hours = Math.max(6, z.days * 24);
    const progress = Math.round(rnd() * 90 + 5) / 100;
    const departed = NOW - progress * hours * 3600e3;
    shipments.push({
      id: `SH-${n++}`,
      dc: z.dc,
      zone: z.id,
      carrier: pick(carriers).id,
      cases: lines.reduce((a, l) => a + l.cases, 0),
      value,
      skus: lines.map((l) => l.sku),
      progress,
      departed: new Date(departed).toISOString(),
      eta: new Date(departed + hours * 3600e3).toISOString(),
      order: `SO-${(880000 + Math.floor(rnd() * 99999)).toString()}`,
      customer: pick(["Ratnadeep", "Vijetha", "Spencer's", "More Retail", "Big Basket", "Reliance Smart", "DMart", "Star Bazaar", "Nilgiris", "Spar", "Easyday", "Kirana network"]) + ` (${z.city})`,
      status: "on time",
    });
  }
}
const zoneOf = (id) => zones.find((z) => z.id === id);
const dcOf = (id) => dcs.find((d) => d.id === id);
// distance from a point to the straight DC->city lane, in km (good enough for a mockup)
const km = (a, b) => Math.hypot((a.lat - b.lat) * 111, (a.lon - b.lon) * 111 * Math.cos((a.lat * Math.PI) / 180));
function nearLane(s, pt, r) {
  const a = dcOf(s.dc), b = zoneOf(s.zone);
  for (let t = 0; t <= 1; t += 0.02) if (km({ lat: a.lat + (b.lat - a.lat) * t, lon: a.lon + (b.lon - a.lon) * t }, pt) < r) return true;
  return false;
}

// SLA contract clauses (simulated) the agent quotes
const clauses = {
  late: { id: "MSA-7.2", text: "Late delivery: 1.5% of invoice value per day late, capped at 10% of invoice." },
  stockout: { id: "MSA-9.1", text: "Fill rate below 95% for a listed SKU in a week: 2% of that week's SKU invoice value." },
  force: { id: "MSA-14.3", text: "Force majeure (declared weather events): penalties waived if the customer is notified within 6 hours." },
};
const D = [
  { id: "D-118", kind: "Weather", title: "NH44 closed near Adilabad", detail: "Heavy rain; Telangana police closed NH44 between Adilabad and Nirmal. Reopening estimate 26 hours.", lat: 19.67, lon: 78.53, r: 70, delay: 1.1, clause: "force", started: "09:12" },
  { id: "D-117", kind: "Facility", title: "Kolkata DC dock 2 conveyor down", detail: "Sorter fault at dock 2; outbound loading at 40% capacity until the part arrives (ETA 18:00).", dc: "DC-KOL", delay: 0.6, clause: "late", started: "08:47" },
  { id: "D-116", kind: "Carrier", title: "Ganga Freight drivers on strike in UP", detail: "Union strike across Lucknow and Kanpur depots; Ganga Freight trucks into UP are holding at the border.", carrier: "C2", states: ["Uttar Pradesh"], delay: 2.0, clause: "late", started: "07:30" },
  { id: "D-115", kind: "Weather", title: "Cyclone watch on the Odisha coast", detail: "IMD cyclone watch; landfall possible near Gopalpur in 48 to 60 hours. Coastal lanes at risk from tomorrow night.", lat: 19.8, lon: 85.5, r: 160, delay: 1.5, clause: "force", started: "06:05" },
  { id: "D-114", kind: "Supplier", title: "Prakash Consumer Products inbound 5 days late", detail: "Snacks supplier missed its dispatch window again (on time fell from 75% to 40% since July 2024). 14 snack SKUs at Mumbai and Delhi run out before the late stock lands.", supplier: "S0", delay: 0, clause: "stockout", started: "05:40", stockout: true },
  { id: "D-113", kind: "Weather", title: "Dense fog on the Yamuna Expressway", detail: "Visibility under 50 m until about 11:30; trucks are holding at toll plazas.", lat: 27.6, lon: 77.6, r: 55, delay: 0.3, clause: "late", started: "05:10" },
];
const sup = suppliers.find((s) => /prakash/i.test(s.name)) ?? suppliers[0];
const disruptions = D.map((d) => {
  let hit = shipments.filter((s) =>
    d.dc ? s.dc === d.dc : d.carrier ? s.carrier === d.carrier && d.states.includes(zoneOf(s.zone).state) : d.stockout ? false : nearLane(s, d, d.r),
  );
  let exposure;
  if (d.stockout) {
    hit = shipments.filter((s) => (s.dc === "DC-MUM" || s.dc === "DC-DEL") && s.skus.some((k) => k.startsWith("SN"))).slice(0, 9);
    exposure = Math.round(hit.reduce((a, s) => a + s.value, 0) * 0.02 + 312000);
  } else {
    // days late beyond the half-day slack built into every promise (MSA-7.2: 1.5% a day, capped at 10%)
    const late = Math.max(0, Math.ceil(d.delay - 0.5));
    exposure = Math.round(hit.reduce((a, s) => a + Math.min(0.1, 0.015 * late) * s.value, 0));
  }
  hit.forEach((s) => (s.status = d.delay >= 1 ? "at risk" : s.status === "at risk" ? "at risk" : "watch"));
  const value = hit.reduce((a, s) => a + s.value, 0);
  const altDc = d.dc === "DC-KOL" ? "DC-HYD" : "DC-MUM";
  const playbooks = [
    d.kind === "Weather" && d.clause === "force"
      ? { id: "P1", title: "Notify customers under force majeure", impact: `Waives ${clauses.force.id} penalties if sent within 6 h`, cost: 0, saves: exposure, needsApproval: false }
      : null,
    !d.stockout ? { id: "P2", title: d.dc ? "Shift loading to dock 1 and run a night shift" : `Reroute via ${dcOf(altDc).city}`, impact: `Recovers about ${Math.round(Math.min(1, d.delay) * 70)}% of the delay`, cost: Math.round(value * 0.012), saves: Math.round(exposure * 0.7), needsApproval: value > 500000 } : null,
    d.carrier ? { id: "P3", title: "Hand UP loads to Deccan Express Logistics", impact: "Spot rate +18%; trucks available from Agra hub in 4 h", cost: Math.round(value * 0.018), saves: Math.round(exposure * 0.85), needsApproval: true } : null,
    d.stockout ? { id: "P4", title: "Transfer snacks stock Kolkata to Mumbai and Delhi", impact: "Covers 11 of 14 SKUs until the late stock lands", cost: 84000, saves: Math.round(exposure * 0.78), needsApproval: true } : null,
    { id: "P5", title: "Update ETAs in the ERP and tell the stores", impact: "Keeps store plans honest; no penalty effect", cost: 0, saves: 0, needsApproval: false },
  ].filter(Boolean);
  return {
    ...d,
    supplier: d.stockout ? sup.id : undefined,
    shipments: hit.map((s) => s.id),
    value,
    exposure,
    clause: clauses[d.clause],
    playbooks,
    agent: {
      steps: [
        `Matched the event to ${d.dc ? dcOf(d.dc).city + " DC" : d.carrier ? carriers.find((c) => c.id === d.carrier).name : d.stockout ? sup.name : "lanes within " + d.r + " km"}`,
        `Found ${hit.length} shipments worth ₹${(value / 1e5).toFixed(1)} L on those lanes`,
        `Read contract clause ${clauses[d.clause].id}`,
        `Priced the exposure at ₹${(exposure / 1e5).toFixed(1)} L`,
        `Drafted ${playbooks.length} playbooks; ${playbooks.filter((p) => p.needsApproval).length ? "the ones moving over ₹5 L wait for approval" : "none needs approval"}`,
      ],
      tokens: 3800 + Math.floor(rnd() * 2400),
      ms: 6000 + Math.floor(rnd() * 9000),
    },
  };
}).sort((a, b) => b.exposure - a.exposure);

const health = {
  feeds: [
    { name: "ERP order export (CSV, hourly)", ok: 0.982, last: "10:00", note: "14 rows to dead letters: blank promised date" },
    { name: "GPS pings (webhook)", ok: 0.997, last: "10:40", note: "212 out-of-order pings re-sorted" },
    { name: "Weather alerts (IMD feed)", ok: 1.0, last: "10:35", note: "" },
    { name: "Carrier status API", ok: 0.931, last: "10:39", note: "Ganga Freight API timing out; retrying with backoff" },
    { name: "Bills of lading (PDF inbox)", ok: 0.954, last: "10:31", note: "3 PDFs to review: unreadable scan" },
  ],
  dlq: 17,
  p95: 840,
  // dead letters: rows the pipeline refused, with the reason and a fix an operator can apply before replaying
  dead: [
    { id: "DL-3301", feed: "ERP order export", at: "10:00", reason: "Promised date is blank", row: "SO-948211, Z11, DC-MUM, 2026-11-13, (blank)", fix: "Use the lane's standard transit: 1 day" },
    { id: "DL-3302", feed: "ERP order export", at: "10:00", reason: "Promised date is blank", row: "SO-948219, Z12, DC-MUM, 2026-11-13, (blank)", fix: "Use the lane's standard transit: 1 day" },
    { id: "DL-3303", feed: "ERP order export", at: "10:00", reason: "Duplicate shipment id with different value", row: "SH-4977 seen twice: ₹4.12 L and ₹4.21 L", fix: "Keep the later export (₹4.21 L)" },
    { id: "DL-3304", feed: "Bills of lading", at: "10:31", reason: "Scan unreadable (OCR confidence 41%)", row: "BOL_DEL_20261113_0042.pdf", fix: "Send to a person to type in" },
    { id: "DL-3305", feed: "GPS pings", at: "10:22", reason: "Timestamp in the future (clock skew 11 min)", row: "truck MH-04-KF-2211, 10:33:08", fix: "Correct by the device's known skew" },
    { id: "DL-3306", feed: "Carrier status API", at: "10:39", reason: "Timed out after 3 retries", row: "GET /v2/consignments/GF-118204", fix: "Retry now" },
    { id: "DL-3307", feed: "ERP order export", at: "09:00", reason: "City code not in the zone table", row: "SO-947760, 'HYD-2', DC-HYD", fix: "Map 'HYD-2' to Z01 Hyderabad" },
  ],
};
const roles = [
  { id: "vp", name: "Asha Rao", title: "VP Operations", scope: "All India", sees: "everything; bank details masked" },
  { id: "south", name: "Kiran Kumar", title: "Dispatcher, South", scope: "Hyderabad DC lanes", sees: "own zone only; phone numbers masked" },
  { id: "fin", name: "Meera Iyer", title: "Finance", scope: "All India", sees: "money and contracts; no driver contacts" },
];

const data = { now: "2026-11-13T10:40:00+05:30", customer: "Tarangini Distribution", dcs, zones, suppliers, carriers, shipments, disruptions, health, roles, clauses };
writeFileSync(join(here, "data.js"), `// Generated by build-data.mjs. Network from the Supply Chain project; live state simulated.\nwindow.CT = ${JSON.stringify(data)};\n`);
console.log(`shipments ${shipments.length}, value ₹${(shipments.reduce((a, s) => a + s.value, 0) / 1e7).toFixed(2)} Cr; disruptions:`);
for (const d of disruptions) console.log(` ${d.id} ${d.title}: ${d.shipments.length} shipments, ₹${(d.value / 1e5).toFixed(1)} L moving, exposure ₹${(d.exposure / 1e5).toFixed(2)} L`);
