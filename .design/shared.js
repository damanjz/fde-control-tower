// Shared helpers for the three control-tower concepts. Needs data.js first.
const C = window.CT;
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const dcOf = (id) => C.dcs.find((d) => d.id === id);
const zoneOf = (id) => C.zones.find((z) => z.id === id);
const carrierOf = (id) => C.carriers.find((c) => c.id === id);
const shipOf = (id) => C.shipments.find((s) => s.id === id);
const supOf = (id) => C.suppliers.find((s) => s.id === id);

// money the Indian way: ₹3.99 L, ₹13.93 Cr, ₹84,000
function inr(v, { short = true } = {}) {
  if (short && v >= 1e7) return `₹${(v / 1e7).toFixed(2)} Cr`;
  if (short && v >= 1e5) return `₹${(v / 1e5).toFixed(2)} L`;
  return "₹" + Math.round(v).toLocaleString("en-IN");
}
const hhmm = (iso) => new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
const day = (iso) => new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: "Asia/Kolkata" });

// map projection for the network (equirectangular, longitude squeezed by cos 22°)
const BOUNDS = (() => {
  const pts = [...C.dcs, ...C.zones, ...C.suppliers];
  return { lat0: Math.min(...pts.map((p) => p.lat)) - 1.2, lat1: Math.max(...pts.map((p) => p.lat)) + 1.2, lon0: Math.min(...pts.map((p) => p.lon)) - 1.2, lon1: Math.max(...pts.map((p) => p.lon)) + 1.2 };
})();
function projector(w, h, pad = 24) {
  const k = Math.cos((22 * Math.PI) / 180);
  const sx = (BOUNDS.lon1 - BOUNDS.lon0) * k, sy = BOUNDS.lat1 - BOUNDS.lat0;
  const s = Math.min((w - 2 * pad) / sx, (h - 2 * pad) / sy);
  const ox = (w - sx * s) / 2, oy = (h - sy * s) / 2;
  return (lat, lon) => [ox + (lon - BOUNDS.lon0) * k * s, oy + (BOUNDS.lat1 - lat) * s];
}
// a lane: a gentle arc from the warehouse to the city
function lanePath(P, a, b) {
  const [x1, y1] = P(a.lat, a.lon), [x2, y2] = P(b.lat, b.lon);
  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2, dx = x2 - x1, dy = y2 - y1;
  const bend = 0.12;
  return { d: `M${x1.toFixed(1)},${y1.toFixed(1)} Q${(mx - dy * bend).toFixed(1)},${(my + dx * bend).toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`, at: (t) => {
    const cx = mx - dy * bend, cy = my + dx * bend, u = 1 - t;
    return [u * u * x1 + 2 * u * t * cx + t * t * x2, u * u * y1 + 2 * u * t * cy + t * t * y2];
  } };
}

const totals = () => ({
  moving: C.shipments.reduce((a, s) => a + s.value, 0),
  exposure: C.disruptions.reduce((a, d) => a + d.exposure, 0),
  atRisk: C.shipments.filter((s) => s.status === "at risk").length,
  watch: C.shipments.filter((s) => s.status === "watch").length,
});

// role-based view: what a role may see. The South dispatcher sees Hyderabad DC lanes only.
const ROLE_SCOPE = { vp: () => true, south: (s) => s.dc === "DC-HYD", fin: () => true };
const mask = (v, role, kind) => {
  if (kind === "bank" && role !== "fin") return "•••• masked";
  if (kind === "phone" && role !== "vp") return "+91 ••••• •••••";
  return v;
};

// a live clock that starts at the scenario's "now" and runs in real time
function clock(el, fmt = (d) => d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Kolkata" })) {
  const t0 = Date.now(), base = Date.parse(C.now);
  const tick = () => (el.textContent = fmt(new Date(base + (Date.now() - t0))));
  tick();
  setInterval(tick, 1000);
}

// bar across the top of each concept (not part of the design)
function conceptbar(id, name, note) {
  const all = [["a-control-room", "A Control Room"], ["b-triage-desk", "B Triage Desk"], ["c-ripple", "C Ripple"]];
  const bar = document.createElement("div");
  bar.className = "mockbar";
  bar.innerHTML = `<b>Concept ${id}: ${name}</b><span>${note}</span><nav>${all.map(([f, n]) => `<a href="${f}.html"${n.startsWith(id + " ") ? ' class="on"' : ""}>${n}</a>`).join("")}</nav>`;
  document.body.prepend(bar);
}
