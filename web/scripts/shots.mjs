// Proof screenshots of the single-case UI: the case (as Asha, Kiran and Meera), Data health and Audit, at 1440x900,
// 1920x1080, 2560x1440 and 375x812, plus the approved mockup beside the 1440 case.
// Needs the UI on :4400 (python scripts/dev.py --no-llm) and, for the comparison, the mockup server
// (node .design/serve.mjs, :4180). Read-only: it approves nothing.
// Usage: node scripts/shots.mjs [out-dir]   -> <out-dir>/<view>-<width>.png and compare-main-1440.png (default .shots/latest)
// Prints the horizontal overflow per page and every console error (cdp.mjs).
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const OUT = resolve(process.argv[2] ?? ".shots/latest").replaceAll("\\", "/");
mkdirSync(OUT, { recursive: true });
const APP = "http://127.0.0.1:4400";
const SIZES = [[1440, 900], [1920, 1080], [2560, 1440], [375, 812]];
const VIEWS = [["main-asha", "asha", "/"], ["kiran", "kiran", "/"], ["meera", "meera", "/?incident=3"], ["data-health", "asha", "/data-health"], ["audit", "asha", "/audit"]];
const MEASURE = "JSON.stringify({page: location.pathname + location.search, overflow: document.documentElement.scrollWidth - innerWidth})";
// the recorded-run replay plays once per case per session; mark every case as seen so the shots show the settled page
const SEEN = "sessionStorage.setItem('ct-replayed', JSON.stringify([1, 2, 3, 4, 5, 6])); 'seen'";

const steps = [{ nav: APP + "/", settle: 1500 }, { eval: SEEN }];
for (const [w, h] of SIZES) {
  steps.push({ viewport: [w, h] });
  for (const [name, role, path] of VIEWS) {
    steps.push({ cookie: { name: "ct_role", value: role, url: APP + "/" } }, { nav: APP + path, settle: 3500 }, { eval: MEASURE }, { shot: `${OUT}/${name}-${w}.png` });
  }
}
const file = `${OUT}/steps.json`;
writeFileSync(file, JSON.stringify(steps));
execFileSync("node", ["scripts/cdp.mjs", file], { stdio: "inherit" });

// the approved mockup beside the built case, 1440x900
const mock = resolve("../.design/v2/shots/indigo-1440.png").replaceAll("\\", "/");
const html = `<!doctype html><meta charset=utf-8><style>body{margin:0;background:#000;color:#ddd;font:600 22px Segoe UI}div{display:flex;gap:12px;padding:12px}figure{margin:0}figcaption{padding:4px 0 8px}img{display:block;width:1440px;height:900px;border:1px solid #333}</style><div><figure><figcaption>Approved mockup</figcaption><img src="file:///${mock}"></figure><figure><figcaption>Built app, as Asha</figcaption><img src="file:///${OUT}/main-asha-1440.png"></figure></div>`;
writeFileSync(`${OUT}/compare.html`, html);
writeFileSync(`${OUT}/compare.json`, JSON.stringify([{ viewport: [2930, 970, 0.5] }, { nav: `file:///${OUT}/compare.html`, settle: 800 }, { shot: `${OUT}/compare-main-1440.png` }]));
execFileSync("node", ["scripts/cdp.mjs", `${OUT}/compare.json`], { stdio: "inherit" });
