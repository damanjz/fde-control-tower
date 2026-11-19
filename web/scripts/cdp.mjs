// Headless-Chromium driver over the DevTools protocol (no packages), for proof screenshots.
// Usage: node scripts/cdp.mjs steps.json
// steps: {viewport:[w,h]} {nav:url, settle:ms} {wait:ms} {eval:"js"} {shot:"file.png"} {click:"css selector"}
//        {key:"Escape"|"Tab"|"Enter"} {waitFor:"js expression", timeout:ms} {cookie:{name,value,url}}
// Prints eval results, and at the end every console error and uncaught exception seen.
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, readdirSync, existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { homedir, tmpdir } from "node:os";

// a Playwright chrome-headless-shell install: set CT_CHROME_DIR to its chromium_headless_shell-<n> folder
const base = process.env.CT_CHROME_DIR ?? join(homedir(), "AppData/Local/ms-playwright/chromium_headless_shell-1234");
const exe = readdirSync(base).map((d) => join(base, d, "chrome-headless-shell.exe")).find(existsSync);
const steps = JSON.parse(readFileSync(process.argv[2], "utf8"));
const port = 9334;
const profile = process.env.CT_CHROME_PROFILE ?? join(tmpdir(), "profile-ct");
mkdirSync(profile, { recursive: true });
const proc = spawn(exe, [`--remote-debugging-port=${port}`, "--hide-scrollbars", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let target;
for (let i = 0; i < 50 && !target; i++) {
  await sleep(200);
  try {
    const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
    target = list.find((t) => t.type === "page");
  } catch {}
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let id = 0;
const pending = new Map();
const errors = [];
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); return; }
  if (d.method === "Runtime.consoleAPICalled" && (d.params.type === "error" || d.params.type === "warning"))
    errors.push(`console.${d.params.type}: ` + d.params.args.map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 300));
  if (d.method === "Runtime.exceptionThrown") errors.push("exception: " + (d.params.exceptionDetails.exception?.description ?? d.params.exceptionDetails.text).slice(0, 300));
  if (d.method === "Log.entryAdded" && d.params.entry.level === "error") errors.push(`log.${d.params.entry.source}: ${d.params.entry.text} ${d.params.entry.url ?? ""}`.slice(0, 300));
};
const send = (method, params = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
const evaluate = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  return r.result?.result?.value ?? r.result?.exceptionDetails?.exception?.description ?? null;
};
await send("Page.enable");
await send("Runtime.enable");
await send("Log.enable");
await send("Network.enable");
const KEYS = { Escape: 27, Tab: 9, Enter: 13, ArrowDown: 40, ArrowUp: 38 };
for (const s of steps) {
  if (s.viewport) await send("Emulation.setDeviceMetricsOverride", { width: s.viewport[0], height: s.viewport[1], deviceScaleFactor: s.viewport[2] ?? 1, mobile: false });
  if (s.media) await send("Emulation.setEmulatedMedia", { features: s.media });
  if (s.cookie) await send("Network.setCookie", s.cookie);
  if (s.nav) { await send("Page.navigate", { url: s.nav }); await sleep(s.settle ?? 2500); }
  if (s.wait) await sleep(s.wait);
  if (s.click) {
    const ok = await evaluate(`(() => { const e = document.querySelector(${JSON.stringify(s.click)}); if (!e) return false; e.scrollIntoView({block:"center"}); e.click(); return true; })()`);
    if (!ok) console.log("click: not found", s.click);
    await sleep(s.after ?? 600);
  }
  if (s.key) {
    const text = s.key === "Enter" ? String.fromCharCode(13) : undefined;   // a key with text activates a focused button
    for (const type of ["keyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key: s.key, code: s.key, windowsVirtualKeyCode: KEYS[s.key], ...(type === "keyDown" && text ? { text } : {}) });
    await sleep(s.after ?? 300);
  }
  if (s.waitFor) {
    const t0 = Date.now();
    while (Date.now() - t0 < (s.timeout ?? 15000) && !(await evaluate(s.waitFor))) await sleep(250);
    console.log("waitFor", JSON.stringify(s.waitFor).slice(0, 80), Date.now() - t0, "ms");
  }
  if (s.eval) console.log("eval:", JSON.stringify(await evaluate(s.eval)).slice(0, 8000));
  if (s.shot) {
    const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    writeFileSync(s.shot, Buffer.from(r.result.data, "base64"));
    console.log("shot:", s.shot);
  }
}
console.log("console errors:", errors.length);
for (const e of errors) console.log("  " + e);
ws.close();
proc.kill();
