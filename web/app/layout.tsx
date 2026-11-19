import type { Metadata, Viewport } from "next";
import "@fontsource-variable/anek-latin/wdth.css";
import "@fontsource-variable/hanken-grotesk/wght.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Tarangini control tower",
  description: "An AI agent catches a disruption, prices it in rupees from real contracts, and proposes a fix a person approves. Fictional Indian FMCG distributor, local model.",
};
export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#16184a" };

// The direction contract (the approved design, .design/v2/a-indigo.html). React cannot emit a bare comment node, so
// it sits in a hidden element that is the first child of <body>; the comment itself is in the emitted HTML.
const CONTRACT = `<!--
THESIS: Evidence on paper, the decision on the machine side. One incident at a time, read top to bottom; it refuses the full-bleed map console.
OWN-WORLD: Indigo #16184a ground; one paper sheet #f3f4f8 with the only shadow, lines inside it; marigold money on the indigo; Anek Latin wide display, Hanken Grotesk text.
STORY: The recruiter reads one incident caught, priced from the contract, decided; then approves, and sees the rupees avoided.
FIRST VIEWPORT: The strip of incidents on top, the selected tab joining the sheet; the sheet left with the 4 stages; the rail right with the Rs figure, the countdown and Approve.
FORM: Layout A, single case (the picked mockup), seed key "daman-pick-A3".
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md
-->`;

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <div hidden data-direction-contract="" dangerouslySetInnerHTML={{ __html: CONTRACT }} />
        {children}
      </body>
    </html>
  );
}
