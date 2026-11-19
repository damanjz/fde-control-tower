// The live map, ported from the concept's drawing code (.design/command.html: drawMap, update, flyTo, applySel, the
// hover cards). It draws straight into an SVG because the camera moves every frame during a flight; React owns the
// rest of the page. Trucks and incident markers are keyed by id, so a poll updates them in place (no restarted pulses,
// no lost keyboard focus).
import { INDIA } from "./india";
import { bounds, lanePath, progress, projector, type Bounds, type Lane, type LatLon, type Proj } from "./geo";
import { day, dcCity, hhmm, inr, short } from "./format";
import type { Incident, LiveShipment, Network, Zone } from "./types";

const NS = "http://www.w3.org/2000/svg";
const esc = (s: unknown) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
type Cam = { s: number; tx: number; ty: number };
type Item = { n: Element; u: () => void };

export type MapData = {
  network: Network;
  shipments: LiveShipment[];
  incidents: Incident[];
  nowMs: number;
  stale: Set<string>;     // carriers whose circuit is not closed: their trucks show the last known status
  scoped: boolean;        // a dispatcher: with nothing selected, his own lanes are lit
};

export class MapEngine {
  private P0: Proj = () => [0, 0];
  private B: Bounds | null = null;
  private cam: Cam = { s: 1, tx: 0, ty: 0 };
  private items: Item[] = [];
  private lanes = new Map<string, Lane>();
  private trucks = new Map<string, { node: SVGCircleElement; s: LiveShipment; t: number }>();
  private marks = new Map<number, { g: SVGGElement; u: () => void; d: Incident; t1: SVGTextElement; t2: SVGTextElement; pos: { at: LatLon } }>();
  private layers: Record<"grat" | "land" | "lanes" | "trucks" | "cities" | "dcs" | "marks", SVGGElement>;
  private data: MapData | null = null;
  private sel: number | null = null;
  private flight: ((now: number) => void) | null = null;
  private raf = 0;
  private size = { w: 0, h: 0 };
  private staticKey = "";
  private reduced: boolean;

  constructor(private wrap: HTMLElement, private world: SVGGElement, private tip: HTMLElement, private onOpen: (id: number) => void) {
    const mk = (cls: string) => { const g = document.createElementNS(NS, "g"); g.setAttribute("class", cls); world.appendChild(g); return g; };
    this.layers = { grat: mk("l-grat"), land: mk("l-land"), lanes: mk("l-lanes"), trucks: mk("l-trucks"), cities: mk("l-cities"), dcs: mk("l-dcs"), marks: mk("l-marks") };
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    wrap.addEventListener("pointermove", this.onMove);
    wrap.addEventListener("pointerleave", this.onLeave);
    world.addEventListener("click", this.onClick);
    world.addEventListener("keydown", this.onKey);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    for (const g of Object.values(this.layers)) g.remove();
    this.wrap.removeEventListener("pointermove", this.onMove);
    this.wrap.removeEventListener("pointerleave", this.onLeave);
    this.world.removeEventListener("click", this.onClick);
    this.world.removeEventListener("keydown", this.onKey);
  }

  // ------------------------------------------------------------------ data in
  setData(d: MapData) {
    const first = !this.data;
    this.data = d;
    this.B = bounds(d.network);
    const r = this.wrap.getBoundingClientRect();
    if (first || r.width !== this.size.w || r.height !== this.size.h) this.measure();
    this.drawStatic();
    this.syncTrucks();
    this.syncMarks();
    this.update();
    this.applySel(this.sel, false);
  }

  /** The container changed size: reproject, back to all India, then frame the selection again (the concept's resize). */
  resize() {
    if (!this.data) return;
    this.measure();
    this.cam = { s: 1, tx: 0, ty: 0 };
    this.staticKey = "";
    this.drawStatic();
    this.update();
    this.applySel(this.sel, this.sel != null);
  }

  /** Reproject for the current container size, keeping the camera (the concept's drawMap after the brief opens). */
  redraw() {
    if (!this.data) return;
    this.measure();
    this.drawStatic();
    this.update();
  }

  private measure() {
    const r = this.wrap.getBoundingClientRect();
    this.size = { w: r.width, h: r.height };
    if (this.B) this.P0 = projector(this.B, r.width, r.height, 40);
  }

  private P = (lat: number, lon: number): [number, number] => {
    const [x, y] = this.P0(lat, lon);
    return [x * this.cam.s + this.cam.tx, y * this.cam.s + this.cam.ty];
  };

  private el<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number>, parent: Element): SVGElementTagNameMap[K] {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, String(attrs[k]));
    parent.appendChild(e);
    return e;
  }

  private zone = (id: string) => this.data!.network.zones.find((z) => z.id === id);
  private dc = (id: string) => this.data!.network.warehouses.find((w) => w.id === id);

  // ------------------------------------------------------------------ static layers: graticule, land, lanes, cities, DCs
  private laneKeys(): string[] {
    const net = this.data!.network;
    const keys = new Set(net.zones.map((z) => `${z.serving_dc}|${z.id}`));
    for (const s of this.data!.shipments) keys.add(`${s.dc_id}|${s.zone_id}`);   // a re-routed truck rides its own lane
    return [...keys].sort();
  }

  private drawStatic() {
    const keys = this.laneKeys();
    const key = `${this.size.w}x${this.size.h}|${keys.join(",")}`;
    if (key === this.staticKey) return;
    this.staticKey = key;
    const L = this.layers, net = this.data!.network;
    for (const g of [L.grat, L.land, L.lanes, L.cities, L.dcs]) g.replaceChildren();
    this.items = [];
    this.lanes.clear();
    const P = this.P;
    for (let lat = 8; lat <= 36; lat += 4) {
      const n = this.el("line", { class: "grat" }, L.grat);
      this.items.push({ n, u: () => { const [x1, y] = P(lat, 60), [x2] = P(lat, 100); n.setAttribute("x1", `${x1}`); n.setAttribute("x2", `${x2}`); n.setAttribute("y1", `${y}`); n.setAttribute("y2", `${y}`); } });
    }
    for (let lon = 64; lon <= 100; lon += 4) {
      const n = this.el("line", { class: "grat" }, L.grat);
      this.items.push({ n, u: () => { const [x, y1] = P(40, lon), [, y2] = P(4, lon); n.setAttribute("x1", `${x}`); n.setAttribute("x2", `${x}`); n.setAttribute("y1", `${y1}`); n.setAttribute("y2", `${y2}`); } });
    }
    for (const ring of INDIA) {
      const n = this.el("path", { class: "land" }, L.land);
      this.items.push({ n, u: () => n.setAttribute("d", "M" + ring.map(([lon, lat]) => P(lat, lon).map((v) => v.toFixed(1)).join(",")).join("L") + "Z") });
    }
    for (const k of keys) {
      const [dcId, zoneId] = k.split("|"), a = this.dc(dcId), b = this.zone(zoneId);
      if (!a || !b) continue;
      const n = this.el("path", { class: "lane", "data-k": k, "data-z": zoneId }, L.lanes);
      this.items.push({ n, u: () => { const ln = lanePath(P, a, b); this.lanes.set(k, ln); n.setAttribute("d", ln.d); } });
    }
    const dcCities = new Set(net.warehouses.map((w) => dcCity(w.city)));
    for (const z of net.zones) {
      const c = this.el("circle", { r: 2.3, class: "city", "data-z": z.id }, L.cities);
      this.items.push({ n: c, u: () => { const [x, y] = P(z.lat, z.lon); c.setAttribute("cx", `${x}`); c.setAttribute("cy", `${y}`); } });
      if (dcCities.has(z.city)) continue;
      const t = this.el("text", { class: "cityl", "data-z": z.id }, L.cities);
      t.textContent = z.city;
      this.items.push({ n: t, u: () => { const [x, y] = P(z.lat, z.lon); t.setAttribute("x", `${x + 6}`); t.setAttribute("y", `${y + 4}`); } });
    }
    for (const d of net.warehouses) {
      const g = this.el("g", { class: "dc", "data-dc": d.id }, L.dcs);
      const a = this.el("circle", { r: 9 }, g), b = this.el("circle", { r: 3.5, class: "core" }, g), t = this.el("text", {}, g);
      t.textContent = dcCity(d.city).toUpperCase();
      this.items.push({ n: g, u: () => { const [x, y] = P(d.lat, d.lon); a.setAttribute("cx", `${x}`); a.setAttribute("cy", `${y}`); b.setAttribute("cx", `${x}`); b.setAttribute("cy", `${y}`); t.setAttribute("x", `${x + 14}`); t.setAttribute("y", `${y + 4}`); } });
    }
  }

  // ------------------------------------------------------------------ trucks and markers, keyed
  private syncTrucks() {
    const { shipments, nowMs, stale } = this.data!;
    const seen = new Set<string>();
    for (const s of shipments) {
      const a = this.dc(s.dc_id), b = this.zone(s.zone_id);
      if (!a || !b) continue;
      seen.add(s.id);
      const fix: LatLon | null = s.last_lat != null && s.last_lon != null ? { lat: s.last_lat, lon: s.last_lon } : null;
      const t = progress(a, b, fix, s.departed_at, s.eta, nowMs);
      let T = this.trucks.get(s.id);
      if (!T) {
        T = { node: this.el("circle", { r: 2.7, class: "truck", "data-id": s.id }, this.layers.trucks), s, t };
        this.trucks.set(s.id, T);
      }
      T.s = s;
      T.t = t;
      T.node.setAttribute("data-s", s.risk);
      T.node.classList.toggle("stale", stale.has(s.carrier_id));
    }
    for (const [id, T] of this.trucks) if (!seen.has(id)) { T.node.remove(); this.trucks.delete(id); }
  }

  private anchor(d: Incident): LatLon | null {
    const net = this.data!.network;
    if (d.lat != null && d.lon != null) return { lat: d.lat, lon: d.lon };
    if (d.dc_id) return this.dc(d.dc_id) ?? null;
    if (d.supplier_id) { const s = net.suppliers.find((x) => x.id === d.supplier_id); if (s) return s; }
    const zs: Zone[] = d.states?.length ? net.zones.filter((z) => d.states!.includes(z.state)) : [];
    const hit = zs.length ? zs : this.data!.shipments.filter((s) => s.incidents.includes(d.id)).map((s) => this.zone(s.zone_id)!).filter(Boolean);
    if (!hit.length) return null;
    return { lat: hit.reduce((a, z) => a + z.lat, 0) / hit.length, lon: hit.reduce((a, z) => a + z.lon, 0) / hit.length };
  }

  private syncMarks() {
    const seen = new Set<number>();
    for (const d of this.data!.incidents) {
      const at = this.anchor(d);
      if (!at) continue;
      seen.add(d.id);
      let M = this.marks.get(d.id);
      if (!M) {
        const g = this.el("g", { class: "mk", "data-d": d.id, tabindex: 0, role: "button" }, this.layers.marks);
        const r1 = this.el("circle", { r: 15, class: "ring" }, g), r2 = this.el("circle", { r: 15, class: "ring b" }, g), dot = this.el("circle", { r: 3.5, class: "dot" }, g);
        const chip = this.el("g", { class: "chip" }, g), rect = this.el("rect", { rx: 6, height: 22 }, chip);
        const t1 = this.el("text", {}, chip), t2 = this.el("text", { class: "v" }, chip);
        const pos = { at };
        const u = () => {
          const [x, y] = this.P(pos.at.lat, pos.at.lon);
          for (const c of [r1, r2, dot]) { c.setAttribute("cx", `${x}`); c.setAttribute("cy", `${y}`); }
          const w1 = t1.getComputedTextLength(), w2 = t2.getComputedTextLength(), w = w1 + w2 + 26;
          const left = x + w + 20 > this.wrap.clientWidth - 60, cx = left ? x - 14 - w : x + 14;
          rect.setAttribute("x", `${cx}`); rect.setAttribute("y", `${y - 11}`); rect.setAttribute("width", `${w}`);
          t1.setAttribute("x", `${cx + 9}`); t1.setAttribute("y", `${y + 4}`); t2.setAttribute("x", `${cx + 17 + w1}`); t2.setAttribute("y", `${y + 4}`);
        };
        M = { g, u, d, t1, t2, pos };
        this.marks.set(d.id, M);
      }
      M.pos.at = at;
      M.d = d;
      M.g.classList.toggle("zero", !(d.net_inr > 0));
      M.g.setAttribute("aria-label", `${d.title}: ${d.net_inr > 0 ? inr(d.net_inr) + " at risk" : "nothing at risk"}. Open the brief`);
      M.t1.textContent = short(d.title);
      M.t2.textContent = d.net_inr > 0 ? inr(d.net_inr) : "watch";
    }
    for (const [id, M] of this.marks) if (!seen.has(id)) { M.g.remove(); this.marks.delete(id); }
  }

  // ------------------------------------------------------------------ camera
  private update = () => {
    for (const it of this.items) it.u();
    for (const T of this.trucks.values()) {
      const L = this.lanes.get(`${T.s.dc_id}|${T.s.zone_id}`);
      if (!L) continue;
      const [x, y] = L.at(T.t);
      T.node.setAttribute("cx", x.toFixed(1));
      T.node.setAttribute("cy", y.toFixed(1));
    }
    for (const M of this.marks.values()) M.u();
  };

  private flyTo(target: Cam, ms = 1000) {
    if (this.reduced || ms <= 0) { this.cam = target; this.update(); return; }
    const from = { ...this.cam }, t0 = performance.now();
    this.flight = (now) => {
      const k = Math.min(1, (now - t0) / ms), e = 1 - Math.pow(1 - k, 4);
      this.cam = { s: from.s + (target.s - from.s) * e, tx: from.tx + (target.tx - from.tx) * e, ty: from.ty + (target.ty - from.ty) * e };
      this.update();
      if (k >= 1) this.flight = null;
    };
    cancelAnimationFrame(this.raf);
    const frame = (now: number) => { if (this.flight) { this.flight(now); this.raf = requestAnimationFrame(frame); } };
    this.raf = requestAnimationFrame(frame);
  }

  zoomBy(f: number) {
    const cx = this.size.w / 2, cy = this.size.h / 2;
    const s = Math.max(1, Math.min(4, this.cam.s * f));
    this.flyTo({ s, tx: cx - ((cx - this.cam.tx) / this.cam.s) * s, ty: cy - ((cy - this.cam.ty) / this.cam.s) * s }, 450);
  }

  fit() { this.flyTo({ s: 1, tx: 0, ty: 0 }, 600); }

  /** Light the selected incident's trucks, lanes and cities (or a dispatcher's own lanes) and frame them. */
  applySel(sel: number | null, fly = true) {
    this.sel = sel;
    if (!this.data) return;
    const d = sel != null ? this.data.incidents.find((x) => x.id === sel) ?? null : null;
    const hotShips = this.data.shipments.filter((s) => (d ? s.incidents.includes(d.id) : this.data!.scoped));
    const hot = new Set(hotShips.map((s) => s.id));
    const hk = new Set(hotShips.map((s) => `${s.dc_id}|${s.zone_id}`)), hz = new Set(hotShips.map((s) => s.zone_id));
    this.world.classList.toggle("dim", hot.size > 0 || d != null);
    this.layers.lanes.querySelectorAll<SVGPathElement>(".lane").forEach((n) => n.classList.toggle("hot", hk.has(n.dataset.k!)));
    this.layers.cities.querySelectorAll<SVGElement>(".city,.cityl").forEach((n) => n.classList.toggle("hot", hz.has(n.dataset.z!)));
    for (const [id, M] of this.marks) { M.g.classList.toggle("hot", !d || id === d.id); M.g.classList.toggle("sel", d?.id === id); }
    for (const T of this.trucks.values()) T.node.classList.toggle("hot", hot.has(T.s.id));
    if (!fly || this.size.w <= 0) return;
    if (!d) { this.flyTo({ s: 1, tx: 0, ty: 0 }); return; }
    const ll: LatLon[] = [...hk].flatMap((k) => { const [a, b] = k.split("|"); return [this.dc(a)!, this.zone(b)!]; }).filter(Boolean);
    const at = this.anchor(d);
    if (at) ll.push(at);
    if (!ll.length) return;
    const pts = ll.map((p) => this.P0(p.lat, p.lon)), xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const bw = Math.max(120, Math.max(...xs) - Math.min(...xs)), bh = Math.max(120, Math.max(...ys) - Math.min(...ys));
    const s = Math.min(2.6, Math.max(1, Math.min((this.size.w - 180) / bw, (this.size.h - 200) / bh)));
    const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
    this.flyTo({ s, tx: this.size.w / 2 - cx * s, ty: this.size.h / 2 + 24 - cy * s });
  }

  // ------------------------------------------------------------------ hover cards and clicks
  private onMove = (e: PointerEvent) => {
    const target = e.target as Element;
    const t = target.closest?.(".truck") as SVGElement | null, dcEl = target.closest?.(".dc") as SVGElement | null;
    if ((!t && !dcEl) || !this.data) { this.tip.classList.remove("on"); return; }
    const r = this.wrap.getBoundingClientRect(), net = this.data.network;
    if (t) {
      const s = this.trucks.get(t.dataset.id!)?.s;
      if (!s) { this.tip.classList.remove("on"); return; }
      const c = net.carriers.find((x) => x.id === s.carrier_id), dc = this.dc(s.dc_id);
      const col = s.risk === "on time" ? "ok" : s.risk === "watch" ? "watch" : "risk";
      this.tip.innerHTML = `<b>${esc(s.id)} · ${esc(s.customer)}</b><div><span>From</span><span>${esc(dc ? dcCity(dc.city) : s.dc_id)} DC</span></div><div><span>Carrier</span><span>${esc(c?.name ?? s.carrier_id)}</span></div><div><span>Goods</span><span class="num">${inr(s.value_inr)} · ${s.cases} cases</span></div>${s.eta ? `<div><span>ETA</span><span>${day(s.eta)}, ${hhmm(s.eta)}</span></div>` : ""}<div><span>Status</span><span style="color:var(--${col})">${s.risk}${s.days_late > 0 ? ` · ${s.days_late} day${s.days_late > 1 ? "s" : ""} late` : ""}</span></div>`;
    } else {
      const d = this.dc(dcEl!.dataset.dc!)!, out = this.data.shipments.filter((s) => s.dc_id === d.id);
      this.tip.innerHTML = `<b>${esc(d.city)} DC</b><div><span>Trucks out</span><span class="num">${out.length}</span></div><div><span>Goods moving</span><span class="num">${inr(out.reduce((a, s) => a + s.value_inr, 0))}</span></div><div><span>At risk</span><span class="num">${out.filter((s) => s.risk === "at risk").length} trucks</span></div><div><span>Cities served</span><span>${net.zones.filter((z) => z.serving_dc === d.id).length}</span></div>`;
    }
    this.tip.style.left = Math.min(r.width - 240, e.clientX - r.left + 14) + "px";
    this.tip.style.top = Math.min(r.height - 150, e.clientY - r.top + 14) + "px";
    this.tip.classList.add("on");
  };

  private onLeave = () => this.tip.classList.remove("on");

  private onClick = (e: MouseEvent) => {
    const mk = (e.target as Element).closest?.(".mk") as SVGGElement | null;
    if (mk) this.onOpen(Number(mk.dataset.d));
  };

  private onKey = (e: KeyboardEvent) => {
    const mk = (e.target as Element).closest?.(".mk") as SVGGElement | null;
    if (mk && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); this.onOpen(Number(mk.dataset.d)); }
  };

  /** Focus a marker (used to return focus after the brief closes when it was opened from the map). */
  focusMark(id: number): boolean {
    const M = this.marks.get(id);
    if (!M) return false;
    (M.g as unknown as HTMLElement).focus({ preventScroll: true });
    return true;
  }
}
