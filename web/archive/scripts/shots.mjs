// Proof screenshots: every view, for every role, at 1440x900, 1920x1080 and 375x812, next to the concept.
// Needs the UI on :4400 (and the concept server, node .design/serve.mjs, on :4180).
// Usage: node scripts/shots.mjs            -> writes .shots/app/*.png, .shots/concept/*.png, .shots/compare/*.png
// Prints the horizontal overflow per page and every console error (cdp.mjs).
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const OUT = resolve(".shots").replaceAll("\\", "/");
for (const d of ["app", "concept", "compare"]) mkdirSync(`${OUT}/${d}`, { recursive: true });
const SIZES = [[1440, 900], [1920, 1080], [375, 812]];
const ROLES = ["asha", "kiran", "meera"];
const APP = "http://127.0.0.1:4400/";
const CONCEPT = "http://127.0.0.1:4180/command.html";
const OVERFLOW = "JSON.stringify({page: location.search, overflow: document.documentElement.scrollWidth - innerWidth})";
// incident to open per role: the top one asha sees; kiran's own one (his only incident)
const BRIEF = { asha: 4, kiran: 1, meera: 4 };

const steps = [];
const views = [["map", null], ["brief", null], ["inc", "[data-v=inc]"], ["health", "[data-v=health]"], ["audit", "[data-v=audit]"]];
for (const [w, h] of SIZES) {
  steps.push({ viewport: [w, h] });
  for (const role of ROLES) {
    steps.push({ cookie: { name: "ct_role", value: role, url: APP } });
    for (const [v, tab] of views) {
      steps.push({ nav: v === "brief" ? `${APP}?incident=${BRIEF[role]}` : APP, settle: 3500 });
      if (tab) steps.push({ click: tab, after: 2500 });
      steps.push({ eval: OVERFLOW });
      steps.push({ shot: `${OUT}/app/${v}-${role}-${w}.png` });
    }
  }
  // the concept, same views
  for (const [v, tab] of views) {
    steps.push({ nav: CONCEPT, settle: 1500 });
    if (v === "brief") steps.push({ click: ".card", after: 1400 });
    if (tab) steps.push({ click: tab, after: 400 });
    steps.push({ shot: `${OUT}/concept/${v}-${w}.png` });
  }
}
const file = `${OUT}/steps.json`;
writeFileSync(file, JSON.stringify(steps));
execFileSync("node", ["scripts/cdp.mjs", file], { stdio: "inherit" });

// side by side: concept | asha | kiran | meera, one image per view and size
const cmp = [];
for (const [w, h] of SIZES) {
  for (const [v] of views) {
    const cols = [["Concept", `concept/${v}-${w}.png`], ...ROLES.map((r) => [`App as ${r}`, `app/${v}-${r}-${w}.png`])];
    const html = `<!doctype html><meta charset=utf-8><style>body{margin:0;background:#000;color:#ddd;font:600 22px Segoe UI}div{display:flex;gap:12px;padding:12px}figure{margin:0}figcaption{padding:4px 0 8px}img{display:block;width:${w}px;height:${h}px;object-fit:contain;object-position:top;border:1px solid #333}</style><div>${cols.map(([c, src]) => `<figure><figcaption>${c} · ${v} · ${w}x${h}</figcaption><img src="file:///${OUT}/${src}"></figure>`).join("")}</div>`;
    const page = `${OUT}/compare/${v}-${w}.html`;
    writeFileSync(page, html);
    cmp.push({ viewport: [4 * w + 70, h + 70, w > 400 ? 0.5 : 1] }, { nav: `file:///${page}`, settle: 800 }, { shot: `${OUT}/compare/${v}-${w}.png` });
  }
}
writeFileSync(`${OUT}/compare.json`, JSON.stringify(cmp));
execFileSync("node", ["scripts/cdp.mjs", `${OUT}/compare.json`], { stdio: "inherit" });
