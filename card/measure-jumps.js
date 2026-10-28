// Layout-jump measurement for the card under a busy evening (preview-hass.html?stress=1&manual=1).
// Scrolls to the middle, then for each of N updates records the viewport y (what the eye sees) of every working
// session's header row, every plan strip and every agent row, runs one update, waits two frames and records again.
// An element's own content changes only its height, never its top, so any top movement is caused by something else.
// Run: pw-node card/measure-jumps.js [--card <file>] [--steps 20] [--json]   (exit 0; prints a table)
// --card serves another card file as crew-hub-card.js (e.g. the previous version for a before/after comparison).
// --verbose lists every visible element that moved. --max-visible <px>: exit 1 if any element visible before an update moves more than this in any configuration.
const http = require("http");
const fs = require("fs");
const path = require("path");
const {chromium} = require("playwright");

const DIR = __dirname;
const TYPES = {".html": "text/html", ".js": "text/javascript", ".json": "application/json"};
const argv = process.argv.slice(2);
const opt = (name, dflt) => { const i = argv.indexOf("--" + name); return i >= 0 ? argv[i + 1] : dflt; };
const CARD = opt("card", null);
const STEPS = Number(opt("steps", 20));
const MAX_VISIBLE = opt("max-visible", null);
const VERBOSE = argv.includes("--verbose");
const CONFIGS = [];
for (const width of [390, 1280]) for (const scroller of [false, true]) for (const noanchor of [false, true]) CONFIGS.push({width, scroller, noanchor});

const server = http.createServer((req, res) => {
  const p = path.join(DIR, decodeURIComponent(new URL(req.url, "http://x").pathname));
  if (!p.startsWith(DIR) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, {"content-type": TYPES[path.extname(p)] || "application/octet-stream"});
  fs.createReadStream(p).pipe(res);
});

// Runs in the page: viewport tops of the tracked elements, keyed by what they show.
function positions() {
  const card = document.querySelector("crew-hub-card"), r = card.shadowRoot, out = {};
  const host = document.getElementById("fake-ha-view");
  const sc = host ? host.shadowRoot.querySelector(".scroller") : null;
  const vTop = sc ? sc.getBoundingClientRect().top : 0, vH = sc ? sc.clientHeight : innerHeight;
  const put = (k, el) => { const b = el.getBoundingClientRect(); if (b.height) out[k] = {y: b.top - vTop, vis: b.bottom > vTop && b.top < vTop + vH}; };
  for (const a of r.querySelectorAll(".sessions > article.sess")) {
    const n = a.querySelector(".name");
    if (!n) continue;
    put("sess:" + n.textContent, a.querySelector(".row"));
    for (const g of a.querySelectorAll(".agent")) put("agent:" + n.textContent + "/" + (g.querySelector(".name") || {}).textContent, g);
  }
  for (const s of r.querySelectorAll(".plans .strip")) put("plan:" + s.querySelector("h3").textContent, s);
  return out;
}

async function run(browser, base, cfg) {
  const page = await browser.newPage({viewport: {width: cfg.width, height: 900}});
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  await page.route("https://fonts.googleapis.com/**", r => r.abort());
  if (CARD) await page.route("**/crew-hub-card.js", r => r.fulfill({contentType: "text/javascript", body: fs.readFileSync(CARD, "utf8")}));
  await page.goto(base + "/preview-hass.html?stress=1&manual=1" + (cfg.scroller ? "&scroller=1" : "") + (cfg.noanchor ? "&noanchor=1" : ""));
  await page.waitForFunction(() => window.__stressTick && document.querySelector("crew-hub-card").shadowRoot.querySelector(".sessions article.sess"));
  for (let i = 0; i < 3; i++) { await page.evaluate(() => window.__stressTick()); await page.waitForTimeout(50); }
  await page.evaluate(() => {
    const host = document.getElementById("fake-ha-view"), sc = host ? host.shadowRoot.querySelector(".scroller") : document.scrollingElement;
    sc.scrollTop = Math.round((sc.scrollHeight - sc.clientHeight) / 2);
  });
  await page.waitForTimeout(100);
  const frames = () => page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
  const st = {pairs: 0, moved: 0, sum: 0, max: 0, vPairs: 0, vMoved: 0, vSum: 0, vMax: 0, watch: [], perStep: []};
  for (let i = 0; i < STEPS; i++) {
    const before = await page.evaluate(positions);
    await page.evaluate(() => window.__stressTick());
    await frames();
    const after = await page.evaluate(positions);
    let stepMax = 0;
    for (const [k, b] of Object.entries(before)) {
      const a = after[k];
      if (!a) continue;
      const d = Math.abs(a.y - b.y);
      st.pairs++; st.sum += d; st.max = Math.max(st.max, d); if (d > 1) st.moved++;
      if (b.vis) { st.vPairs++; st.vSum += d; st.vMax = Math.max(st.vMax, d); if (d > 1) st.vMoved++; stepMax = Math.max(stepMax, d);
        if (d > 1 && VERBOSE) console.log(`  [${cfg.width}${cfg.scroller ? " div" : ""}${cfg.noanchor ? " noanchor" : ""}] update ${i + 1}: ${k} moved ${Math.round(a.y - b.y)} px (was at ${Math.round(b.y)})`); }
      if (k.includes("/Watch:")) st.watch.push(Math.round(a.y - b.y));
    }
    st.perStep.push(Math.round(stepMax));
    await page.waitForTimeout(30);
  }
  st.errors = errors;
  await page.close();
  return st;
}

(async () => {
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  const results = [];
  let bad = false;
  try {
    for (const cfg of CONFIGS) {
      const st = await run(browser, base, cfg);
      results.push({cfg, st});
      if (MAX_VISIBLE != null && st.vMax > Number(MAX_VISIBLE)) bad = true;
      if (st.errors.length) bad = true;
    }
  } finally {
    await browser.close();
    server.close();
  }
  if (argv.includes("--json")) { console.log(JSON.stringify(results, null, 1)); }
  else {
    console.log(`card: ${CARD || "card/crew-hub-card.js"}, ${STEPS} updates per configuration, scrolled to the middle`);
    console.log("width scroll     anchor | all: moved/pairs  sum px  max px | visible: moved/pairs  sum px  max px | Watch agent dy per update");
    for (const {cfg, st} of results) {
      console.log(`${String(cfg.width).padEnd(5)} ${(cfg.scroller ? "shadow-div" : "document").padEnd(10)} ${(cfg.noanchor ? "off" : "browser").padEnd(7)}| `
        + `${(st.moved + "/" + st.pairs).padStart(10)} ${st.sum.toFixed(0).padStart(7)} ${st.max.toFixed(0).padStart(7)} | `
        + `${(st.vMoved + "/" + st.vPairs).padStart(10)} ${st.vSum.toFixed(0).padStart(8)} ${st.vMax.toFixed(0).padStart(7)} | ${st.watch.join(",")}`
        + (st.errors.length ? "  ERRORS: " + st.errors.join(" | ") : ""));
    }
  }
  process.exit(bad ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
