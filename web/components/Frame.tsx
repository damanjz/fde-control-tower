"use client";
// The page frame on the indigo: the header (name, scenario clock, quiet links, View as) and the scenario clock every
// page ticks from. The clock is the tower's (GET /live `now`), not the wall: seeded by the server render, ticking in
// the browser, re-synced whenever a page polls the tower.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useTransition, type KeyboardEvent, type ReactNode } from "react";
import { stamp, hhmm } from "@/lib/format";
import { ROLES, ROLE_COOKIE, type RoleId } from "@/lib/roles";

type Clock = { now: number; resync: (iso: string) => void };
const ClockCtx = createContext<Clock>({ now: 0, resync: () => {} });
export const useClock = () => useContext(ClockCtx);

const PAGES = [["incidents", "/", "Incidents"], ["health", "/data-health", "Data health"], ["audit", "/audit", "Audit"]] as const;
export type PageId = (typeof PAGES)[number][0];

const TITLE: Record<RoleId, string> = {
  asha: "Asha, VP Operations: all of India, approves large moves",
  kiran: "Kiran, dispatcher: Hyderabad only, phones masked",
  meera: "Meera, finance: sees the money, cannot act",
};

/** The role the proxy calls the tower as, kept for a year in a cookie the server pages read. */
function rememberRole(r: RoleId) {
  document.cookie = `${ROLE_COOKIE}=${r}; path=/; max-age=31536000; samesite=lax`;
}

export default function Frame({ role, seed, page, children }: { role: RoleId; seed: { now: number; wall: number }; page: PageId; children: ReactNode }) {
  const router = useRouter();
  const [pending, start] = useTransition();
  const [asked, setAsked] = useState<RoleId>(role);
  const shown = pending ? asked : role;
  const offset = useRef(seed.now - seed.wall);
  const [now, setNow] = useState(seed.now);
  const segRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() + offset.current), 1000);
    return () => clearInterval(t);
  }, []);
  const resync = useCallback((iso: string) => { offset.current = Date.parse(iso) - Date.now(); }, []);
  const clock = useMemo(() => ({ now, resync }), [now, resync]);

  const pick = (r: RoleId) => {
    if (r === role && !pending) return;
    rememberRole(r);
    setAsked(r);
    start(() => router.refresh());
  };
  const onSegKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = ROLES.findIndex((x) => x.id === shown);
    const j = e.key === "ArrowRight" || e.key === "ArrowDown" ? (i + 1) % ROLES.length
      : e.key === "ArrowLeft" || e.key === "ArrowUp" ? (i + ROLES.length - 1) % ROLES.length : -1;
    if (j < 0) return;
    e.preventDefault();
    pick(ROLES[j].id);
    segRefs.current[ROLES[j].id]?.focus();
  };

  return (
    <ClockCtx.Provider value={clock}>
      <div className="wrap">
        <header className="top">
          <div className="brand">
            <strong>Tarangini control tower</strong>
            <span className="when num" title="The scenario clock: the demo morning runs from 10:40 IST in real time">
              <span className="long">{stamp(now)}</span><span className="short">{hhmm(now)} IST</span>
            </span>
          </div>
          <div className="tools">
            <nav className="links" aria-label="Pages">
              {PAGES.map(([id, href, label]) => (
                <Link key={id} href={href} className="quiet" aria-current={id === page ? "page" : undefined}>{label}</Link>
              ))}
            </nav>
            <div className="viewas">
              <span className="role-note" id="viewas-label">View as</span>
              <div className="seg" role="radiogroup" aria-labelledby="viewas-label" aria-busy={pending} onKeyDown={onSegKey}>
                {ROLES.map((r) => (
                  <button key={r.id} type="button" role="radio" aria-checked={shown === r.id} tabIndex={shown === r.id ? 0 : -1}
                    title={TITLE[r.id]} ref={(n) => { segRefs.current[r.id] = n; }} onClick={() => pick(r.id)}>{r.name}</button>
                ))}
              </div>
            </div>
          </div>
        </header>
        {children}
      </div>
    </ClockCtx.Provider>
  );
}
