// Browser check of the card's command controls against card/preview-hass.html (fake hass: a snapshot every second,
// crew_hub/command answered after 600 ms). Run: pw-node card/verify-controls.js  (exit code 0 = all checks passed)
// 0.5.1: stable order (sessions, strips, agents keep their first-seen places; finished agents stay dimmed in place),
// fixed agent-row height, ellipsis + title on long text. Layout jumps under load: card/measure-jumps.js.
// 0.5.0: token chips (session "312k tok · 94% cached", agent "48k tok"), a tasks-sourced plan strip with "from tasks".
// 0.4.0: active plans stack (strips, tap scrolls, +N more), idle section (collapsed, rows expand), finished agents toggle,
// Clear finished / Dismiss send crew_hub/dismiss and the items go once the fake connection pushes the filtered machine.
// Writes card/screenshots/card-v051-390.png and card-v051-1280.png.
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
const JAMFIN = "8c1f2a4e-3b7d-4e19-9a61-0f2d5c7b9e10";
const ECHO = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const JAMFIN_FINISHED = ["a2e8d4c6b1f07a92", "a19b8e7c2d3f4a55", "a5c1e0d2b7f94e31", "a6b0c2d4e8f13579", "a4d6f8b0c2e13579", "a8e0a2c4e6f13579"];

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
      check(await page.locator("crew-hub-card >> details.diag:not(.idle)").count() === 0, "plain preview shows no New session");
      check(await page.locator("crew-hub-card >> button.x").count() === 0, "plain preview: no Dismiss buttons");
      // More than 4 plans: 4 strips + "+N more"
      await page.evaluate(() => {
        for (let i = 1; i < 10000; i++) clearInterval(i); // stop the preview's own updates
        const now = Date.now(), iso = t => new Date(t).toISOString();
        const sessions = [...Array(6)].map((_, i) => ({id: "s" + i, name: "Session " + i, status: "working", updatedAt: iso(now),
          plan: {title: "Plan " + i, phase: "build", done: i, total: 6, finished: false, updatedAt: iso(now - i * 60000), tasks: []}}));
        document.querySelector("crew-hub-card").setSnapshots({m: {machine: {id: "m", name: "m", updatedAt: iso(now)}, sessions}});
      });
      const strips = page.locator("crew-hub-card >> .plans .strip");
      check(await strips.count() === 4, "more than 4 plans: 4 strips shown");
      check(await strips.first().locator("h3").textContent() === "Plan 0", "newest plan first");
      const more = page.locator("crew-hub-card >> .plans button.more");
      check(await more.textContent() === "+2 more", "+2 more toggle");
      await more.click();
      check(await strips.count() === 6 && await more.textContent() === "Show fewer", "+N more shows all plans");
      // 0.5.1: newer plan activity, a finished plan and a new plan don't reorder; the new plan goes to the end
      await more.click();
      await page.evaluate(() => {
        const now = Date.now(), iso = t => new Date(t).toISOString();
        const sessions = [...Array(7)].map((_, i) => ({id: "s" + i, name: "Session " + i, status: "working", updatedAt: iso(now + i * 1000),
          plan: {title: "Plan " + i, phase: "build", done: i, total: 6, finished: i === 1, updatedAt: iso(now + i * 60000), tasks: []}}));
        document.querySelector("crew-hub-card").setSnapshots({m: {machine: {id: "m", name: "m", updatedAt: iso(now)}, sessions}});
      });
      check(JSON.stringify(await page.locator("crew-hub-card >> .plans .strip h3").allTextContents())
        === JSON.stringify(["Plan 0", "Plan 1", "Plan 2", "Plan 3"]), "strips keep their places when plans update or finish");
      await more.click();
      check((await page.locator("crew-hub-card >> .plans .strip h3").allTextContents()).join() === "Plan 0,Plan 1,Plan 2,Plan 3,Plan 4,Plan 5,Plan 6",
        "a new plan goes to the end");
      check(JSON.stringify(await page.locator("crew-hub-card >> .sessions article.sess .head .name").allTextContents())
        === JSON.stringify([...Array(7)].map((_, i) => "Session " + i)), "sessions keep their places when newer activity arrives");
      // Agents: first-seen order; a finished one stays in place (dimmed); rows have one height whatever the text length
      const agentsAt = async (list) => page.evaluate(list => {
        const now = Date.now(), iso = t => new Date(t).toISOString();
        const agents = list.map(([id, status, text, at]) => ({id, title: "Agent " + id, status, startedAt: iso(now - 60000), steps: 3,
          endedAt: status === "running" ? null : iso(now), current: {text, at: iso(now - at * 1000)}, summary: status === "running" ? null : text,
          trail: id === "b" ? [{text: "one", at: iso(now - 90000)}] : []}));
        document.querySelector("crew-hub-card").setSnapshots({m: {machine: {id: "m", name: "m", updatedAt: iso(now)},
          sessions: [{id: "s", name: "S", status: "working", updatedAt: iso(now), agents}]}});
      }, list);
      const long = "A very long note about what the agent is doing right now, ".repeat(4);
      await agentsAt([["a", "running", "short", 50], ["b", "running", "short", 10], ["c", "running", "short", 30]]);
      const rows = page.locator("crew-hub-card >> .agents > .agent");
      const names = () => rows.locator(".name").allTextContents();
      const heights = () => rows.evaluateAll(es => es.map(e => Math.round(e.getBoundingClientRect().height)));
      check((await names()).join() === "Agent a,Agent b,Agent c", "agents in first-seen order (not by latest step)");
      const h0 = await heights();
      await agentsAt([["a", "running", long, 1], ["b", "done", long, 5], ["c", "running", "x", 2], ["d", "running", "new", 0]]);
      check((await names()).join() === "Agent a,Agent b,Agent c,Agent d", "newer steps don't reorder; a new agent goes to the end");
      check(await rows.nth(1).evaluate(e => e.classList.contains("held") && e.classList.contains("done")), "finished agent stays in place, dimmed");
      const h1 = await heights();
      check(new Set([...h0, ...h1]).size === 1, `agent rows keep one height (${[...new Set([...h0, ...h1])].join(", ")} px)`);
      check(await rows.nth(0).locator(".now span").getAttribute("title") === long, "long note cut to one line, full text in title");
      await page.evaluate(() => document.querySelector("crew-hub-card").setSnapshots({}));
      check(await strips.count() === 1 && await strips.first().locator("h3").textContent() === "No plan running", "no plans: one 'No plan running' strip");
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
    const idleDet = card.locator("details.idle");
    const row = name => card.locator(".irow", {has: page.locator(`.iopen .nm:text-is("${name}")`)});
    const expand = async name => {
      if (!await idleDet.evaluate(e => e.open)) await idleDet.locator("summary").click();
      if (await row(name).locator(".iopen").getAttribute("aria-expanded") !== "true") await row(name).locator(".iopen").click();
    };

    // Active plans: both unfinished plans + the one finished 4 min ago, newest first; the old finished plan isn't there
    const strips = card.locator(".plans .strip");
    const titles = await strips.locator("h3").allTextContents();
    check(JSON.stringify(titles) === JSON.stringify(["NerveOS: resume stopped agent work", "Jamfin: verify on the TV and fix", "Crew Hub: 3 tasks", "Flicker meter: 7 Pro readout fix"]),
      "plan strips: " + titles.join(" | "));
    const s0 = await strips.nth(1).textContent();
    check(s0.includes("6 of 7 accepted") && s0.includes("Jamfin · nas"), "strip: count + session and machine");
    check(await strips.nth(1).locator(".src").count() === 0, "file plan: no 'from tasks' tag");
    // 0.5.0: a plan derived from the built-in task list looks like a file plan, plus a dim "from tasks" tag
    check(await strips.nth(2).locator(".src").textContent() === "from tasks"
      && await strips.nth(2).locator(".phase").textContent() === "delegate"
      && (await strips.nth(2).textContent()).includes("1 of 3 accepted"), "tasks-sourced plan: phase, count, 'from tasks' tag");
    check(await strips.nth(3).locator(".phase").textContent() === "finished"
      && await strips.nth(3).locator(".bar i").evaluate(e => e.style.width) === "100%", "recently finished plan: chip finished, bar full");
    // Sessions: working + waiting get full blocks, the 7 idle/ended ones a collapsed section
    check(await card.locator(".sessions > article.sess").count() === 5, "5 full blocks (3 working, 2 waiting)");
    check(await idleDet.evaluate(e => !e.open) && await idleDet.locator("summary").textContent() === "Idle sessions (7)", "idle section collapsed: Idle sessions (7)");
    // Queue inside the session block
    const jq = sess("Jamfin").locator(".qbox");
    check(await jq.locator("ul.queue li").count() === 1 && await jq.locator("button", {hasText: "6 done"}).count() === 1, "Jamfin queue: 1 running + '6 done' toggle");
    const nq = await sess("NerveOS: resume stopped agent work").locator(".qbox ul.queue li b").allTextContents();
    check(JSON.stringify(nq) === JSON.stringify(["working", "queued", "queued"]), "NerveOS queue: running + next queued (" + nq.join(",") + ")");
    check(await sess("NerveOS: resume stopped agent work").locator(".meta").first().textContent().then(t => t.includes("project-4f")), "localName shown in the meta row");
    // Tasks-sourced plan: tiers in the queue, task titles on joined agents
    const cq = sess("Crew Hub").locator(".qbox ul.queue li");
    const cqt = await cq.allTextContents();
    check(JSON.stringify(cqt) === JSON.stringify(["workingBuild the Lovelace card careful", "queuedDraft the crew_hub integration medium"])
      && await sess("Crew Hub").locator(".qbox button", {hasText: "1 done"}).count() === 1, "tasks-sourced queue with tiers (" + cqt.join(",") + ")");
    // Token chips
    const metaOf = name => sess(name).locator(".meta").first().textContent();
    check((await metaOf("Crew Hub")).includes("312k tok · 94% cached"), "session meta: 312k tok · 94% cached");
    check((await metaOf("Jamfin")).includes("3.3M tok · 93% cached"), "session meta: 3.3M tok · 93% cached");
    check(!(await metaOf("NerveOS: resume stopped agent work")).includes("tok"), "no token chip without session.tokens");
    const jam = sess("Jamfin").locator(".agents .agent .meta");
    check((await jam.textContent()).includes("48k tok"), "agent meta: 48k tok");
    const cha = sess("Crew Hub").locator(".agents .agent");
    check(await cha.locator(".name").textContent() === "Build the Lovelace card"
      && (await cha.locator(".meta").textContent()).includes("careful") && (await cha.locator(".meta").textContent()).includes("76k tok"),
      "joined agent: task title, tier and 76k tok");
    // Finished agents collapsed; ghost agent (no title, 0 steps) not rendered
    check(await sess("Jamfin").locator(".agents .agent").count() === 1, "Jamfin: only the running agent shown");
    check(await sess("Jamfin").locator("button", {hasText: /^6 finished$/}).count() === 1, "Jamfin: '6 finished' toggle");
    await sess("NerveOS: resume stopped agent work").locator("button", {hasText: /^3 finished$/}).click();
    const stopped = sess("NerveOS: resume stopped agent work").locator(".agent.stopped");
    check(await stopped.count() === 2 && await stopped.first().locator(".chip").textContent() === "stopped"
      && (await stopped.first().locator(".meta").textContent()).includes("Remote Control session restarted"), "stopped agents: dim chip + stopReason");
    check(await card.locator(".agent .name", {hasText: "general-purpose"}).count() === 0, "agent with no title and 0 steps not rendered");

    // Tapping a strip scrolls to its session
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await strips.nth(0).click().catch(() => {});
    await page.evaluate(() => window.scrollTo(0, 0));
    await strips.nth(0).click();
    await page.waitForTimeout(900);
    const top = await sess("NerveOS: resume stopped agent work").evaluate(e => e.getBoundingClientRect().top);
    check(top >= -2 && top < 40, `strip tap scrolls to the session (top ${Math.round(top)} px)`);
    // ...and opens an idle session first
    await page.evaluate(() => window.scrollTo(0, 0));
    await strips.nth(3).click();
    await page.waitForTimeout(900);
    // Near the end of the page the block can't reach the very top; it must be in view.
    const ftop = await sess("Flicker meter").evaluate(e => e.getBoundingClientRect().top);
    check(await idleDet.evaluate(e => e.open) && ftop >= -2 && ftop < 300, `finished plan's strip opens the idle section and its row (top ${Math.round(ftop)} px)`);
    await row("Flicker meter").locator(".iopen").click();
    check(await sess("Flicker meter").count() === 0, "tapping the row again collapses it");
    await idleDet.locator("summary").click();
    await page.evaluate(() => window.scrollTo(0, 0));

    // Which sessions get which controls
    check(await sess("Jamfin").locator("textarea").count() === 1, "Jamfin (interactive) has a message box");
    check(await sess("Jamfin").locator("button", {hasText: /^Stop$/}).count() === 0, "interactive session has no Stop");
    check(await sess("Crew Hub").locator("button", {hasText: /^Stop$/}).count() === 1, "bg session has Stop");
    check(await sess("Crew Hub").locator("button", {hasText: /^Interrupt$/}).count() === 1, "bg session has Interrupt");
    check(await sess("Home Assistant Mr Manager").locator("button", {hasText: /^Allow$/}).count() === 1, "permission: Allow");
    check(await sess("Home Assistant Mr Manager").locator("button", {hasText: /^Deny$/}).count() === 1, "permission: Deny");
    check(await sess("Music Assistant tidy-up").locator("button", {hasText: "Kitchen Pi"}).count() === 1, "question: option buttons");
    await expand("Artemis - iPad kiosk");
    check(await sess("Artemis - iPad kiosk").count() === 1 && await sess("Artemis - iPad kiosk").locator("textarea").count() === 0, "offline machine: no controls");
    await row("Artemis - iPad kiosk").locator(".iopen").click();
    await idleDet.locator("summary").click();

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

    // Error session (idle: expand its row first; the open state survives re-renders)
    await expand("error demo");
    await page.waitForTimeout(1200);
    check(await idleDet.evaluate(e => e.open) && await sess("error demo").count() === 1, "idle row expands to the full block and stays open");
    check(await sess("error demo").locator("textarea").count() === 1, "expanded idle session has its message box");
    const es = sess("error demo");
    await es.locator("button", {hasText: /^Stop$/}).click();
    await es.locator("button", {hasText: /^Confirm stop\?$/}).click();
    await page.waitForTimeout(800);
    check(await es.locator(".fb[data-fb$='|bg']").textContent() === "No answer from the machine" && await es.locator(".fb[data-fb$='|bg']").evaluate(e => e.classList.contains("err")),
      "error text shown in red");
    cmds = await page.evaluate(() => window.__cmds);
    check(cmds[cmds.length - 1].args.session === ERRDEMO, "error demo command sent");

    // Clear finished: sends crew_hub/dismiss with the finished ids; they go once the filtered machine arrives
    await sess("Jamfin").locator("button", {hasText: /^Clear finished$/}).click();
    await page.waitForFunction(() => window.__dismissed.length >= 1);
    const dm = (await page.evaluate(() => window.__dismissed))[0];
    check(dm.type === "crew_hub/dismiss" && dm.machine === "nas" && dm.session === JAMFIN && !dm.session_dismissed
      && JSON.stringify([...dm.agents].sort()) === JSON.stringify([...JAMFIN_FINISHED].sort()), "Clear finished sends the finished ids (" + dm.agents.length + ")");
    await page.waitForTimeout(700);
    check(await sess("Jamfin").locator("button", {hasText: /finished$/}).count() === 0 && await sess("Jamfin").locator(".agent").count() === 1,
      "finished agents gone after the filtered update");
    // Dismiss (x) on an idle row
    if (!await idleDet.evaluate(e => e.open)) await idleDet.locator("summary").click();
    await row("Echo Show kiosk").locator("button.x").click();
    await page.waitForFunction(() => window.__dismissed.length >= 2);
    const dx = (await page.evaluate(() => window.__dismissed))[1];
    check(dx.machine === "nas" && dx.session === ECHO && dx.session_dismissed === true && !dx.agents, "Dismiss sends session_dismissed");
    await page.waitForTimeout(700);
    check(await row("Echo Show kiosk").count() === 0 && await idleDet.locator("summary").textContent() === "Idle sessions (6)", "dismissed row gone: Idle sessions (6)");
    await row("error demo").locator(".iopen").click(); // collapse again
    await idleDet.locator("summary").click();

    // New session: open, fill, survives re-render, two-tap start
    const det = card.locator("details.diag:not(.idle)");
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
    await expand("error demo");
    await page.waitForTimeout(300);
    check(await overflow() <= 0, "no horizontal overflow at 390 px with the idle section and a row open");
    const small2 = await page.evaluate(() => [...document.querySelector("crew-hub-card").shadowRoot.querySelectorAll("button, summary")]
      .filter(e => e.offsetParent !== null).map(e => e.getBoundingClientRect().height).filter(h => h < 40).length);
    check(small2 === 0, `idle rows, toggles and Dismiss ≥ 40 px (${small2} smaller)`);
    await row("error demo").locator(".iopen").click();
    await page.evaluate(() => window.scrollTo(0, 0));
    await det.locator("summary").click(); // collapse New session for the overview shot (idle section left open)
    await page.locator("crew-hub-card").screenshot({path: path.join(DIR, "screenshots", "card-v051-390.png")});
    await page.setViewportSize({width: 1280, height: 900});
    await idleDet.locator("summary").click(); // collapsed, the default
    await page.waitForTimeout(300);
    check(await overflow() <= 0, "no horizontal overflow at 1280 px");
    await page.locator("crew-hub-card").screenshot({path: path.join(DIR, "screenshots", "card-v051-1280.png")});

    check(errors.length === 0, "no console errors" + (errors.length ? ": " + errors.join(" | ") : ""));
  } finally {
    await browser.close();
    server.close();
  }
  console.log(failures ? `${failures} check(s) failed` : "all checks passed");
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
