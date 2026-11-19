"use client";
// The map column: the SVG (drawn by MapEngine), the caption, zoom, legend, hover card and the status banner.
import { useEffect, useRef, type ReactNode, type RefObject } from "react";
import { MapEngine, type MapData } from "@/lib/mapEngine";

type Props = {
  data: MapData | null; sel: number | null; caption: ReactNode; banner: ReactNode; bannerClass: string;
  onOpen: (id: number) => void; engineRef: RefObject<MapEngine | null>;
};

export default function MapView({ data, sel, caption, banner, bannerClass, onOpen, engineRef }: Props) {
  const wrap = useRef<HTMLDivElement>(null), world = useRef<SVGGElement>(null), tip = useRef<HTMLDivElement>(null);
  const openRef = useRef(onOpen);
  const resizing = useRef(false);
  useEffect(() => { openRef.current = onOpen; }, [onOpen]);

  useEffect(() => {
    const e = new MapEngine(wrap.current!, world.current!, tip.current!, (id) => openRef.current(id));
    engineRef.current = e;
    // like the concept: a size change reprojects and goes back to all India, then frames the selection again.
    // Watch the map's own box, not the window: the column also changes size while the brief opens and during first layout.
    let t = 0, last = "";
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      const key = `${Math.round(width)}x${Math.round(height)}`;
      if (key === last) return;
      const first = last === "";
      last = key;
      if (first) return;   // the first observation is the size the engine already measured
      clearTimeout(t);
      resizing.current = true;
      t = window.setTimeout(() => { resizing.current = false; e.resize(); }, 120);
    });
    ro.observe(wrap.current!);
    return () => { ro.disconnect(); clearTimeout(t); e.destroy(); engineRef.current = null; };
  }, [engineRef]);

  useEffect(() => { if (data) engineRef.current?.setData(data); }, [data, engineRef]);

  // the map column narrows while the brief opens; frame the incident once it has settled (the concept waits 560 ms)
  useEffect(() => {
    const e = engineRef.current;
    if (!e) return;
    e.applySel(sel, false);
    // when the column is still changing size, the resize observer reframes once it settles; doing it here too would fly twice
    const t = setTimeout(() => { if (resizing.current) return; e.redraw(); e.applySel(sel, true); }, 560);
    return () => clearTimeout(t);
  }, [sel, engineRef]);

  return (
    <div className="map" id="mapwrap" ref={wrap}>
      <svg id="map" role="img" aria-label="India: warehouses, lanes, trucks and incidents"><g id="world" ref={world} /></svg>
      <div className="cap" id="cap">{caption}</div>
      <div className="zoom">
        <button aria-label="Zoom in" onClick={() => engineRef.current?.zoomBy(1.5)}>+</button>
        <button aria-label="Zoom out" onClick={() => engineRef.current?.zoomBy(1 / 1.5)}>&minus;</button>
        <button aria-label="Show all India" onClick={() => engineRef.current?.fit()}>&#8634;</button>
      </div>
      <div className="legend">
        <span><i style={{ background: "var(--ok)" }} />On time</span>
        <span><i style={{ background: "var(--watch)" }} />Watch</span>
        <span><i style={{ background: "var(--risk)" }} />At risk</span>
      </div>
      <div className="tip" id="tip" ref={tip} />
      <div className={`banner ${bannerClass}`} id="banner" role="status" aria-live="polite">{banner}</div>
    </div>
  );
}
