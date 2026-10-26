// Browser check of the card's command controls against card/preview-hass.html (fake hass: a snapshot every second,
// crew_hub/command answered after 600 ms). Run: pw-node card/verify-controls.js  (exit code 0 = all checks passed)
// Writes card/screenshots/card-controls-390.png and card-controls-1280.png.
const http = require("http");
const fs = require("fs");
const path = require("path");
const {chromium} = require("playwright");

const DIR = __dirname;
const TYPES = {".html": "text/html", ".js": "text/javascript", ".json": "application/json", ".png": "image/png"};
const MANAGER = "4d2b9c71-8a0e-4f3b-b5d6-91c7e2a0f4b8";
const CREW = "b8e41f03-2c6a-47d9-8e15-3a90c6d2f7e1";
const MUSIC = "6a0e7d52-1f84-4c3b-9d27-58b1e3c4a9f6";
const ERRDEMO = "e5c7a913-0b2d-4f6e-8a14-c3d5e7f9a1b2";

let failures = 0;
function check(cond, what) { console.log((cond ? "PASS " : "FAIL ") + what); if (!cond) failures++; }

const server = http.createServer((req, res) => {
  const p = path.join(DIR, decodeURIComponent(new URL(req.url, "http://x").pathname));
  if (!p.startsWith(DIR) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, {"content-type": TYPES[path.extname(p)] || "application/octet-stream"});
  fs.createReadStream(p).pipe(res);
});

(async () => {
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  try {
    // Plain preview (no hass): no controls at all.
    {
      const page = await browser.newPage({viewport: {width: 390, height: 900}});
      await page.route("https://fonts.googleapis.com/**", r => r.abort());
      await page.goto(base + "/preview.html");
      await page.waitForSelector("crew-hub-card >> .sess");
      check(await page.locator("crew-hub-card >> textarea").count() === 0, "plain preview shows no controls");
      check(await page.locator("crew-hub-card >> details").count() === 0, "plain preview shows no New session");
      await page.close();
    }

    const page = await browser.newPage({viewport: {width: 390, height: 900}});
    const errors = [];
    // Font load failures (offline runs) aren't the card's errors.
    page.on("console", m => { if (m.type() === "error" && !/fonts\.(googleapis|gstatic)\.com/.test(m.location().url || "")) errors.push(m.text()); });
    page.on("pageerror", e => errors.push(String(e)));
    await page.goto(base + "/preview-hass.html");
    await page.waitForSelector("crew-hub-card >> .sess textarea");
    const card = page.locator("crew-hub-card");
    const sess = name => card.locator("article.sess", {has: page.locator(`h3:text-is("${name}")`)});

    // Which sessions get which controls
    check(await sess("Jamfin").locator("textarea").count() === 1, "Jamfin (interactive) has a message box");
    check(await sess("Jamfin").locator("button", {hasText: /^Stop$/}).count() === 0, "interactive session has no Stop");
    check(await sess("Crew Hub").locator("button", {hasText: /^Stop$/}).count() === 1, "bg session has Stop");
    check(await sess("Crew Hub").locator("button", {hasText: /^Interrupt$/}).count() === 1, "bg session has Interrupt");
    check(await sess("Home Assistant Mr Manager").locator("button", {hasText: /^Allow$/}).count() === 1, "permission: Allow");
    check(await sess("Home Assistant Mr Manager").locator("button", {hasText: /^Deny$/}).count() === 1, "permission: Deny");
    check(await sess("Music Assistant tidy-up").locator("button", {hasText: "Kitchen Pi"}).count() === 1, "question: option buttons");
    check(await sess("Artemis - iPad kiosk").locator("textarea").count() === 0, "offline machine: no controls");

    // Typed text, focus and caret survive snapshot re-renders
    const box = sess("Jamfin").locator("textarea");
    await page.waitForFunction(() => window.__feed.updates >= 1);
    await box.click();
    await page.keyboard.type("Hello from the card, line one");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    const before = await page.evaluate(() => window.__feed.updates);
    await page.waitForTimeout(2000);
    const after = await page.evaluate(() => window.__feed.updates);
    check(after - before >= 2, `snapshots kept arriving while typing (${after - before} updates)`);
    check(await box.inputValue() === "Hello from the card, line one", "typed text kept");
    const st = await page.evaluate(() => {
      const r = document.querySelector("crew-hub-card").shadowRoot, a = r.activeElement;
      return {tag: a && a.tagName, start: a && a.selectionStart, end: a && a.selectionEnd};
    });
    check(st.tag === "TEXTAREA", "focus kept in the message box");
    check(st.start === 27 && st.end === 27, `caret kept (at ${st.start}-${st.end})`);
    await page.keyboard.type("X");
    check(await box.inputValue() === "Hello from the card, line oXne", "typing continues at the caret");

    // Grows to 3 lines max
    const h1 = await box.evaluate(e => e.getBoundingClientRect().height);
    await box.fill("1\n2\n3\n4\n5\n6");
    await box.dispatchEvent("input");
    const h3 = await box.evaluate(e => e.getBoundingClientRect().height);
    check(h1 >= 40 && h3 > h1 && h3 < h1 * 3.2, `message box grows from ${Math.round(h1)} px to ${Math.round(h3)} px (≤ 3 lines)`);
    await box.fill("Hello from the card");

    // Send (urgent) and feedback
    await sess("Jamfin").locator("label.check input").check();
    await sess("Jamfin").locator("button", {hasText: /^Send$/}).click();
    const fb = sess("Jamfin").locator(".fb[data-fb$='|msg']");
    check(await fb.textContent() === "Sending…", "feedback: Sending…");
    await page.waitForFunction(() => {
      const r = document.querySelector("crew-hub-card").shadowRoot;
      return [...r.querySelectorAll(".fb.ok")].some(e => e.textContent === "Sent ✓");
    }, null, {timeout: 3000});
    check(await fb.textContent() === "Sent ✓" && await fb.evaluate(e => e.classList.contains("ok")), "feedback: Sent ✓ (teal)");
    check(await box.inputValue() === "", "message box cleared after send");
    let cmds = await page.evaluate(() => window.__cmds);
    const sent = cmds[cmds.length - 1];
    check(sent.type === "crew_hub/command" && sent.machine === "nas" && sent.action === "send"
      && sent.args.session === "8c1f2a4e-3b7d-4e19-9a61-0f2d5c7b9e10" && sent.args.text === "Hello from the card" && sent.args.urgent === true,
      "send message shape");

    // Permission answer with a note
    await sess("Home Assistant Mr Manager").locator("input[type=text]").fill("ok, go");
    await page.waitForTimeout(1200); // a re-render in between
    check(await sess("Home Assistant Mr Manager").locator("input[type=text]").inputValue() === "ok, go", "note kept over re-render");
    await sess("Home Assistant Mr Manager").locator("button", {hasText: /^Allow$/}).click();
    await page.waitForTimeout(800);
    cmds = await page.evaluate(() => window.__cmds);
    const ans = cmds[cmds.length - 1];
    check(ans.action === "answer" && ans.args.session === MANAGER && ans.args.requestId === "req_7f31a2" && ans.args.decision === "allow"
      && ans.args.message === "ok, go", "permission answer shape");

    // Question: one tap answers
    await sess("Music Assistant tidy-up").locator("button", {hasText: "OnePlus 5T"}).click();
    await page.waitForTimeout(800);
    cmds = await page.evaluate(() => window.__cmds);
    const q = cmds[cmds.length - 1];
    check(q.action === "answer" && q.args.session === MUSIC && q.args.requestId === "req_c41d09"
      && JSON.stringify(q.args.answers) === JSON.stringify({"Which player should stay the default?": "OnePlus 5T"}), "question answer shape");

    // Two-tap Stop: first tap arms (and survives a re-render), second sends
    const stop = sess("Crew Hub").locator("button[data-confirm$='|stop']");
    const n0 = (await page.evaluate(() => window.__cmds)).length;
    await stop.click();
    check(await stop.textContent() === "Confirm stop?" && await stop.evaluate(e => e.classList.contains("armed")), "first tap arms: Confirm stop? (red)");
    await page.waitForTimeout(1100);
    check(await stop.textContent() === "Confirm stop?", "armed state survives a re-render");
    check((await page.evaluate(() => window.__cmds)).length === n0, "first tap sends nothing");
    await stop.click();
    await page.waitForTimeout(800);
    cmds = await page.evaluate(() => window.__cmds);
    check(cmds.length === n0 + 1 && cmds[n0].action === "stop" && cmds[n0].args.session === CREW, "second tap sends stop");
    check(await stop.textContent() === "Stop", "button back to Stop");
    // Arm expires after 4 s
    const intr = sess("Crew Hub").locator("button[data-confirm$='|interrupt']");
    await intr.click();
    await page.waitForTimeout(4300);
    check(await intr.textContent() === "Interrupt", "armed Interrupt resets after 4 s");

    // Error session
    const es = sess("error demo");
    await es.locator("button", {hasText: /^Stop$/}).click();
    await es.locator("button", {hasText: /^Confirm stop\?$/}).click();
    await page.waitForTimeout(800);
    check(await es.locator(".fb[data-fb$='|bg']").textContent() === "No answer from the machine" && await es.locator(".fb[data-fb$='|bg']").evaluate(e => e.classList.contains("err")),
      "error text shown in red");
    cmds = await page.evaluate(() => window.__cmds);
    check(cmds[cmds.length - 1].args.session === ERRDEMO, "error demo command sent");

    // New session: open, fill, survives re-render, two-tap start
    const det = card.locator("details.diag");
    check(await det.evaluate(e => !e.open), "New session starts collapsed");
    await det.locator("summary").click();
    await det.locator("select[data-key='new|folder']").selectOption("crew-hub");
    await det.locator("select[data-key='new|mode']").selectOption("auto");
    await det.locator("input[data-key='new|name']").fill("Preview start");
    await det.locator("textarea[data-key='new|prompt']").click();
    await page.keyboard.type("Check the card");
    await page.waitForTimeout(1500);
    check(await det.evaluate(e => e.open), "New session stays open over re-renders");
    check(await det.locator("textarea[data-key='new|prompt']").inputValue() === "Check the card", "prompt kept");
    check(await page.evaluate(() => document.querySelector("crew-hub-card").shadowRoot.activeElement?.dataset.key) === "new|prompt", "prompt keeps focus");
    const startBtn = det.locator("button", {hasText: /^Start$/});
    await startBtn.click();
    check(await det.locator("button.armed").textContent() === "Confirm start?", "Start arms");
    await det.locator("button.armed").click();
    await page.waitForTimeout(800);
    cmds = await page.evaluate(() => window.__cmds);
    const start = cmds[cmds.length - 1];
    check(start.action === "start" && start.machine === "nas" && JSON.stringify(start.args) === JSON.stringify(
      {folder: "crew-hub", model: "claude-opus-5-5", permissionMode: "auto", name: "Preview start", prompt: "Check the card"}), "start shape");
    check(await det.locator(".fb").textContent() === "Sent ✓", "start feedback");
    // empty form doesn't arm
    await startBtn.click();
    check(await det.locator(".fb").textContent() === "Name and prompt are needed", "empty form refused without arming");

    // Feedback clears after 6 s
    await page.waitForTimeout(6300);
    check(await card.locator(".fb.ok, .fb.err").count() === 0, "feedback cleared after 6 s");

    // Layout
    const overflow = async () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(await overflow() <= 0, "no horizontal overflow at 390 px");
    const small = await page.evaluate(() => [...document.querySelector("crew-hub-card").shadowRoot.querySelectorAll("button, textarea, input[type=text], select, label.check")]
      .filter(e => e.offsetParent !== null).map(e => e.getBoundingClientRect().height).filter(h => h < 40).length);
    check(small === 0, `touch targets ≥ 40 px (${small} smaller)`);
    await page.evaluate(() => window.scrollTo(0, 0));
    await det.locator("summary").click(); // collapse for the overview shot
    await page.locator("crew-hub-card").screenshot({path: path.join(DIR, "screenshots", "card-controls-390.png")});
    await page.setViewportSize({width: 1280, height: 900});
    await det.locator("summary").click();
    await page.waitForTimeout(300);
    check(await overflow() <= 0, "no horizontal overflow at 1280 px");
    await page.locator("crew-hub-card").screenshot({path: path.join(DIR, "screenshots", "card-controls-1280.png")});

    check(errors.length === 0, "no console errors" + (errors.length ? ": " + errors.join(" | ") : ""));
  } finally {
    await browser.close();
    server.close();
  }
  console.log(failures ? `${failures} check(s) failed` : "all checks passed");
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
