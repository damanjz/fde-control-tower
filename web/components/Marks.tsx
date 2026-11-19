// The small drawn marks every page shares: the trust badge (a tick and a short claim code has checked) and the
// disclosure chevron (always before the label, turning down when open).
import type { ReactNode } from "react";

export const Tick = () => (
  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
);

/** "Priced by code", "Matches the PDF": one badge component everywhere. */
export function Badge({ children }: { children: ReactNode }) {
  return <span className="badge"><Tick />{children}</span>;
}

export const Chev = () => (
  <svg className="chev" viewBox="0 0 10 10" aria-hidden="true"><path d="M3 1.5L7 5 3 8.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
);
