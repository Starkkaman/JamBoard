// Browser check of the card's command controls against card/preview-hass.html (fake hass: a snapshot every second,
// crew_hub/command answered after 600 ms). Run: pw-node card/verify-controls.js  (exit code 0 = all checks passed)
// 0.6.0: Jam look (design/callsheet-harmony): Plans panel with segmented tracks, waiting sessions first (oldest prompt
// first), the ask with Allow / Deny / Add a note, Message and More (Interrupt / Stop, two-tap) behind quiet keys, the
// queue behind a NEXT row, idle rows hidden at once with ×, New session behind "+ New", fonts served next to the card.
// 0.5.1: stable order (sessions, plans, agents keep their first-seen places; finished agents stay dimmed in place),
// fixed agent-row height, ellipsis + title on long text. Layout jumps under load: card/measure-jumps.js.
// 0.5.0: token counts (session "312k tok", cache % in More; agent "48k tok"), a tasks-sourced plan with "from tasks".
// 0.4.0: plans (tap scrolls, +N more), idle section (collapsed, rows expand), finished agents toggle,
// Clear finished / × send crew_hub/dismiss and the items go once the fake connection pushes the filtered machine.
const http = require("http");
const fs = require("fs");
const path = require("path");
const {chromium} = require("playwright");

const DIR = __dirname;
const TYPES = {".html": "text/html", ".js": "text/javascript", ".json": "application/json", ".png": "image/png", ".ttf": "font/ttf", ".woff2": "font/woff2"};
const MANAGER = "4d2b9c71-8a0e-4f3b-b5d6-91c7e2a0f4b8";
const CREW = "b8e41f03-2c6a-47d9-8e15-3a90c6d2f7e1";
const MUSIC = "6a0e7d52-1f84-4c3b-9d27-58b1e3c4a9f6";
const ERRDEMO = "e5c7a913-0b2d-4f6e-8a14-c3d5e7f9a1b2";
const JAMFIN = "8c1f2a4e-3b7d-4e19-9a61-0f2d5c7b9e10";
const ECHO = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const JAMFIN_FINISHED = ["a2e8d4c6b1f07a92", "a19b8e7c2d3f4a55", "a5c1e0d2b7f94e31", "a6b0c2d4e8f13579", "a4d6f8b0c2e13579", "a8e0a2c4e6f13579"];
const HHMM = /^\d{2}:\d{2}$/;

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
      await page.goto(base + "/preview.html");
      await page.waitForSelector("crew-hub-card >> .sess");
      check(await page.locator("crew-hub-card >> textarea").count() === 0, "plain preview shows no controls");
      check(await page.locator("crew-hub-card >> button.hb", {hasText: "New"}).isHidden()
        && await page.locator("crew-hub-card >> .newp").isHidden(), "plain preview shows no New session");
      check(await page.locator("crew-hub-card >> button.x").count() === 0, "plain preview: no hide (×) buttons");
      // More than 4 plans: 4 rows + "+N more"
      await page.evaluate(() => {
        for (let i = 1; i < 10000; i++) clearInterval(i); // stop the preview's own updates
        const now = Date.now(), iso = t => new Date(t).toISOString();
        const sessions = [...Array(6)].map((_, i) => ({id: "s" + i, name: "Session " + i, status: "working", updatedAt: iso(now),
          plan: {title: "Plan " + i, phase: "build", done: i, total: 6, finished: false, updatedAt: iso(now - i * 60000), tasks: []}}));
        document.querySelector("crew-hub-card").setSnapshots({m: {machine: {id: "m", name: "m", updatedAt: iso(now)}, sessions}});
      });
      const strips = page.locator("crew-hub-card >> .plans .plan:not(.none)");
      check(await strips.count() === 4, "more than 4 plans: 4 rows shown");
      check(await strips.first().locator(".pn").textContent() === "Plan 0", "newest plan first");
      const more = page.locator("crew-hub-card >> .plans .ph button");
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
      check(JSON.stringify(await page.locator("crew-hub-card >> .plans .plan .pn").allTextContents())
        === JSON.stringify(["Plan 0", "Plan 1", "Plan 2", "Plan 3"]), "plan rows keep their places when plans update or finish");
      await more.click();
      check((await page.locator("crew-hub-card >> .plans .plan .pn").allTextContents()).join() === "Plan 0,Plan 1,Plan 2,Plan 3,Plan 4,Plan 5,Plan 6",
        "a new plan goes to the end");
      check(JSON.stringify(await page.locator("crew-hub-card >> .sessions > article.sess .sn").allTextContents())
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
      const rows = page.locator("crew-hub-card >> .thread.main > .ag");
      const names = () => rows.locator(".agn").allTextContents();
      const heights = () => rows.evaluateAll(es => es.map(e => Math.round(e.getBoundingClientRect().height)));
      check((await names()).join() === "Agent a,Agent b,Agent c", "agents in first-seen order (not by latest step)");
      const h0 = await heights();
      await agentsAt([["a", "running", long, 1], ["b", "done", long, 5], ["c", "running", "x", 2], ["d", "running", "new", 0]]);
      check((await names()).join() === "Agent a,Agent b,Agent c,Agent d", "newer steps don't reorder; a new agent goes to the end");
      check(await rows.nth(1).evaluate(e => e.classList.contains("held") && e.classList.contains("done")), "finished agent stays in place, dimmed");
      const h1 = await heights();
      check(new Set([...h0, ...h1]).size === 1, `agent rows keep one height (${[...new Set([...h0, ...h1])].join(", ")} px)`);
      check(await rows.nth(0).locator(".now").getAttribute("title") === long, "long note cut to one line, full text in title");
      // The trail opens on a tap (only on agents that have one)
      await rows.nth(1).locator(".agb").click();
      check(await rows.nth(1).locator(".trail").isVisible() && (await rows.nth(1).locator(".trail").textContent()).includes("one"), "a tap opens the agent's trail");
      check(await rows.nth(0).locator(".agb").getAttribute("role") === null, "agent without a trail isn't a button");
      await page.evaluate(() => document.querySelector("crew-hub-card").setSnapshots({}));
      check(await strips.count() === 0 && await page.locator("crew-hub-card >> .plans .plan.none").textContent() === "No plan running", "no plans: one 'No plan running' line");
      await page.close();
    }

    const page = await browser.newPage({viewport: {width: 390, height: 900}});
    const errors = [], external = [];
    page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
    page.on("pageerror", e => errors.push(String(e)));
    page.on("request", r => { if (!r.url().startsWith(base)) external.push(r.url()); });
    await page.goto(base + "/preview-hass.html?theme=light");
    await page.waitForSelector("crew-hub-card >> .sess .ask");
    const card = page.locator("crew-hub-card");
    const sess = name => card.locator("article.sess", {has: page.locator(`h3.sn:text-is("${name}")`)});
    const fold = card.locator(".idle .fold");
    const row = name => card.locator(".irow", {has: page.locator(`.ib .in:text-is("${name}")`)});
    const openFold = async () => { if (await fold.getAttribute("aria-expanded") !== "true") await fold.click(); };
    const closeFold = async () => { if (await fold.getAttribute("aria-expanded") === "true") await fold.click(); };
    const expand = async name => {
      await openFold();
      if (await row(name).locator(".ib").getAttribute("aria-expanded") !== "true") await row(name).locator(".ib").click();
    };
    const key = (scope, text) => scope.locator("button", {hasText: new RegExp("^" + text + "$")});
    const openMore = async scope => { const b = key(scope, "More").first(); if (await b.getAttribute("aria-expanded") !== "true") await b.click(); };
    const openMsg = async scope => { const b = key(scope, "Message").first(); if (await b.getAttribute("aria-expanded") !== "true") await b.click(); };

    // Day/Night follows HA: hass.themes.darkMode false -> data-theme light
    check(await card.getAttribute("data-theme") === "light", "host data-theme follows hass.themes.darkMode (light)");
    // Plans: both unfinished plans + the one finished 4 min ago, newest first; the old finished plan isn't there
    const strips = card.locator(".plans .plan:not(.none)");
    const titles = await strips.locator(".pn").allTextContents();
    check(JSON.stringify(titles) === JSON.stringify(["NerveOS: resume stopped agent work", "Jamfin: verify on the TV and fix", "Crew Hub: 3 tasks", "Flicker meter: 7 Pro readout fix"]),
      "plan rows: " + titles.join(" | "));
    check(await strips.nth(1).locator(".pc").textContent() === "6/7" && await strips.nth(1).locator(".seg i").count() === 7
      && await strips.nth(1).locator(".seg i.done").count() === 6 && await strips.nth(1).locator(".seg i.running").count() === 1,
      "plan row: count 6/7 and one segment per task (6 done, 1 running)");
    check(await strips.nth(1).locator(".src").count() === 0, "file plan: no 'from tasks' tag");
    // 0.5.0: a plan derived from the built-in task list looks like a file plan, plus a dim "from tasks" tag
    check(await strips.nth(2).locator(".src").textContent() === "from tasks"
      && await strips.nth(2).locator(".phase").textContent() === "delegate"
      && await strips.nth(2).locator(".pc").textContent() === "1/3", "tasks-sourced plan: phase, count, 'from tasks' tag");
    check(/^Done \d{2}:\d{2}$/.test(await strips.nth(3).locator(".pc").textContent())
      && await strips.nth(3).locator(".seg i.done").count() === 3, "recently finished plan: Done HH:MM, track full");
    // Sessions: waiting first (oldest prompt first), then working; the 7 idle/ended ones in a folded section
    const order = await card.locator(".sessions > article.sess .sn").allTextContents();
    check(JSON.stringify(order.slice(0, 2)) === JSON.stringify(["Home Assistant Mr Manager", "Music Assistant tidy-up"]) && order.length === 5,
      "5 panels, waiting first, oldest prompt first (" + order.join(" | ") + ")");
    check(await card.locator(".sessions > article.sess .st.you").count() === 2, "two 'Needs you' pills");
    check(await card.locator(".head").textContent() === "2 need you, 3 working.", "headline: 2 need you, 3 working.");
    check(await fold.getAttribute("aria-expanded") === "false" && await card.locator(".idle .cnt").textContent() === "7"
      && await card.locator(".irows").isHidden(), "idle section folded: Idle 7");
    // The one orange key: the oldest answerable prompt's Allow
    check(await card.locator(".go").count() === 1 && await sess("Home Assistant Mr Manager").locator("button.go").textContent() === "Allow",
      "exactly one orange key (.go): Mr Manager's Allow");
    // Queue: one NEXT/TASKS row that opens the full list
    const openQueue = async name => { const b = sess(name).locator(".qw .next"); if (await b.getAttribute("aria-expanded") !== "true") await b.click(); };
    check((await sess("Jamfin").locator(".qw .next").textContent()).startsWith("Tasks"), "Jamfin queue row: TASKS (nothing queued)");
    await openQueue("Jamfin");
    check(await sess("Jamfin").locator(".tasks li.done").count() === 6 && await sess("Jamfin").locator(".tasks li.running").count() === 1,
      "Jamfin task list: 6 done + 1 running");
    const nn = await sess("NerveOS: resume stopped agent work").locator(".qw .next").textContent();
    check(nn === "NextBattery report every 15 min+1", "NerveOS queue row: NEXT Battery report every 15 min +1 (" + nn + ")");
    await openQueue("NerveOS: resume stopped agent work");
    const nq = await sess("NerveOS: resume stopped agent work").locator(".tasks li .tw").allTextContents();
    check(JSON.stringify(nq) === JSON.stringify(["Done", "Running", "Queued", "Queued"]), "NerveOS task list (" + nq.join(",") + ")");
    // Tasks-sourced plan: tiers in the list, task titles on joined agents
    await openQueue("Crew Hub");
    const cqt = await sess("Crew Hub").locator(".tasks li .tt").allTextContents();
    check(JSON.stringify(cqt) === JSON.stringify(["Write the snapshot fixture  light", "Build the Lovelace card  careful", "Draft the crew_hub integration  medium"]),
      "tasks-sourced list with tiers (" + cqt.join(",") + ")");
    // Meta line + More
    const metaOf = name => sess(name).locator(".meta .ml").textContent();
    check((await metaOf("Crew Hub")).includes("312k tok"), "session meta: 312k tok");
    check((await metaOf("Jamfin")).includes("3.3M tok") && (await metaOf("Jamfin")).includes("$3.84"), "session meta: 3.3M tok · $3.84");
    check(!(await metaOf("NerveOS: resume stopped agent work")).includes("tok"), "no token count without session.tokens");
    check(await sess("Home Assistant Mr Manager").locator(".meta .mc").textContent() === "▲ 91% context"
      && await sess("Home Assistant Mr Manager").locator(".ctx.warn").count() === 1, "context ≥ 85 %: ▲ 91% context, warn bar");
    await openMore(sess("Crew Hub"));
    const ckv = await sess("Crew Hub").locator(".more .kv").textContent();
    check(ckv.includes("Cached94%") && ckv.includes("Turns96") && ckv.includes("Local nameproject-c1"), "More: cache %, turns, local name");
    await openMore(sess("NerveOS: resume stopped agent work"));
    check((await sess("NerveOS: resume stopped agent work").locator(".more .kv").textContent()).includes("project-4f"), "localName shown in More");
    const jam = sess("Jamfin").locator(".thread.main .ag .agm");
    check((await jam.textContent()).includes("48k tok"), "agent meta: 48k tok");
    const cha = sess("Crew Hub").locator(".thread.main .ag");
    check(await cha.locator(".agn").textContent() === "Build the Lovelace card"
      && (await cha.locator(".agm").textContent()).includes("careful") && (await cha.locator(".agm").textContent()).includes("76k tok"),
      "joined agent: task title, tier and 76k tok");
    // Finished agents folded; ghost agent (no title, 0 steps) not rendered
    check(await sess("Jamfin").locator(".thread.main .ag").count() === 1, "Jamfin: only the running agent shown");
    check(await sess("Jamfin").locator("button", {hasText: /^6 finished$/}).count() === 1, "Jamfin: '6 finished' toggle");
    await sess("NerveOS: resume stopped agent work").locator("button", {hasText: /^3 finished$/}).click();
    const stopped = sess("NerveOS: resume stopped agent work").locator(".thread.old .ag.stopped");
    check(await stopped.count() === 2 && (await stopped.first().locator(".tw").textContent()).startsWith("Stopped")
      && (await stopped.first().locator(".why").textContent()).includes("Remote Control session restarted"), "stopped agents: Stopped + stopReason");
    check(await card.locator(".agn, .agl", {hasText: "general-purpose"}).count() === 0, "agent with no title and 0 steps not rendered");

    // Tapping a plan row scrolls to its session
    await page.evaluate(() => window.scrollTo(0, 0));
    await strips.nth(0).click();
    await page.waitForTimeout(900);
    const top = await sess("NerveOS: resume stopped agent work").evaluate(e => e.getBoundingClientRect().top);
    check(top >= -2 && top < 40, `plan tap scrolls to the session (top ${Math.round(top)} px)`);
    // ...and opens an idle session first
    await page.evaluate(() => window.scrollTo(0, 0));
    await strips.nth(3).click();
    await page.waitForTimeout(900);
    const ftop = await row("Flicker meter").evaluate(e => e.getBoundingClientRect().top);
    check(await fold.getAttribute("aria-expanded") === "true" && await row("Flicker meter").locator(".idet").isVisible() && ftop >= -2 && ftop < 300,
      `finished plan's row opens the idle section and its row (top ${Math.round(ftop)} px)`);
    await row("Flicker meter").locator(".ib").click();
    check(await row("Flicker meter").locator(".idet").count() === 0, "tapping the row again collapses it");
    await closeFold();
    await page.evaluate(() => window.scrollTo(0, 0));

    // Which sessions get which controls
    await openMsg(sess("Jamfin"));
    check(await sess("Jamfin").locator("textarea").count() === 1, "Jamfin (interactive) has a message box behind Message");
    await openMore(sess("Jamfin"));
    check(await key(sess("Jamfin"), "Stop").count() === 0, "interactive session has no Stop");
    check(await key(sess("Crew Hub"), "Stop").count() === 1, "bg session has Stop (in More)");
    check(await key(sess("Crew Hub"), "Interrupt").count() === 1, "bg session has Interrupt (in More)");
    check(await key(sess("Home Assistant Mr Manager"), "Allow").count() === 1, "permission: Allow");
    check(await key(sess("Home Assistant Mr Manager"), "Deny").count() === 1, "permission: Deny");
    check(await sess("Music Assistant tidy-up").locator("button", {hasText: "Kitchen Pi"}).count() === 1, "question: option keys");
    await expand("Artemis - iPad kiosk");
    check(await row("Artemis - iPad kiosk").locator(".idet").isVisible() && await key(row("Artemis - iPad kiosk"), "Message").isHidden()
      && await row("Artemis - iPad kiosk").locator("textarea").count() === 0, "offline machine: no controls");
    check((await row("Artemis - iPad kiosk").locator(".im .off").textContent()) === "mac offline", "offline machine marked in the idle row");
    await row("Artemis - iPad kiosk").locator(".ib").click();
    await closeFold();

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
    await box.fill("");
    check(await key(sess("Jamfin"), "Send").isDisabled() && await sess("Jamfin").locator("label.tick").isHidden(), "empty box: Send disabled, no Urgent tick");
    await box.fill("Hello from the card");

    // Send (urgent) and feedback
    await sess("Jamfin").locator("label.tick input").check();
    await key(sess("Jamfin"), "Send").click();
    const fb = sess("Jamfin").locator(".fb[data-fb$='|msg']");
    check(await fb.textContent() === "Sending…", "feedback: Sending…");
    await page.waitForFunction(() => {
      const r = document.querySelector("crew-hub-card").shadowRoot;
      return [...r.querySelectorAll(".fb.ok")].some(e => e.textContent.startsWith("Sent"));
    }, null, {timeout: 3000});
    const sentText = await fb.textContent();
    check(/^Sent urgent \d{2}:\d{2}$/.test(sentText) && await fb.evaluate(e => e.classList.contains("ok")), "feedback: " + sentText);
    check(await box.inputValue() === "", "message box cleared after send");
    let cmds = await page.evaluate(() => window.__cmds);
    const sent = cmds[cmds.length - 1];
    check(sent.type === "crew_hub/command" && sent.machine === "nas" && sent.action === "send"
      && sent.args.session === JAMFIN && sent.args.text === "Hello from the card" && sent.args.urgent === true,
      "send message shape");

    // Permission answer with a note (the note field opens on "Add a note")
    const mgr = sess("Home Assistant Mr Manager");
    check(await mgr.locator("input.note").isHidden(), "note field closed at first");
    await key(mgr, "Add a note").click();
    await mgr.locator("input.note").fill("ok, go");
    await page.waitForTimeout(1200); // a re-render in between
    check(await mgr.locator("input.note").inputValue() === "ok, go" && await mgr.locator("input.note").isVisible(), "note kept (and open) over re-render");
    await key(mgr, "Allow").click();
    await page.waitForTimeout(800);
    cmds = await page.evaluate(() => window.__cmds);
    const ans = cmds[cmds.length - 1];
    check(ans.action === "answer" && ans.args.session === MANAGER && ans.args.requestId === "req_7f31a2" && ans.args.decision === "allow"
      && ans.args.message === "ok, go", "permission answer shape");
    check(/^Allowed \d{2}:\d{2} · with your note$/.test(await mgr.locator(".fb[data-fb$='|ans']").textContent()), "feedback: Allowed HH:MM · with your note");

    // Question: one tap answers
    await sess("Music Assistant tidy-up").locator("button", {hasText: "OnePlus 5T"}).click();
    await page.waitForTimeout(800);
    cmds = await page.evaluate(() => window.__cmds);
    const q = cmds[cmds.length - 1];
    check(q.action === "answer" && q.args.session === MUSIC && q.args.requestId === "req_c41d09"
      && JSON.stringify(q.args.answers) === JSON.stringify({"Which player should stay the default?": "OnePlus 5T"}), "question answer shape");
    check(/^Answered \d{2}:\d{2} · OnePlus 5T$/.test(await sess("Music Assistant tidy-up").locator(".fb[data-fb$='|ans']").textContent()), "feedback: Answered HH:MM · OnePlus 5T");

    // Two-tap Stop (in More): first tap arms (and survives a re-render), second sends
    const stop = sess("Crew Hub").locator("button[data-confirm$='|stop']");
    const n0 = (await page.evaluate(() => window.__cmds)).length;
    await stop.click();
    check(await stop.textContent() === "Tap again to stop" && await stop.evaluate(e => e.classList.contains("dark")), "first tap arms: Tap again to stop (dark ink)");
    await page.waitForTimeout(1100);
    check(await stop.textContent() === "Tap again to stop", "armed state survives a re-render");
    check((await page.evaluate(() => window.__cmds)).length === n0, "first tap sends nothing");
    await stop.click();
    await page.waitForTimeout(800);
    cmds = await page.evaluate(() => window.__cmds);
    check(cmds.length === n0 + 1 && cmds[n0].action === "stop" && cmds[n0].args.session === CREW, "second tap sends stop");
    check(await stop.textContent() === "Stop", "key back to Stop");
    check(/^Stop sent \d{2}:\d{2}$/.test(await sess("Crew Hub").locator(".fb[data-fb$='|bg']").textContent()), "feedback: Stop sent HH:MM");
    // Arm expires after 4 s
    const intr = sess("Crew Hub").locator("button[data-confirm$='|interrupt']");
    await intr.click();
    await page.waitForTimeout(4300);
    check(await intr.textContent() === "Interrupt", "armed Interrupt resets after 4 s");

    // Error session (idle: expand its row first; the open state survives re-renders)
    await expand("error demo");
    await page.waitForTimeout(1200);
    const es = row("error demo");
    check(await fold.getAttribute("aria-expanded") === "true" && await es.locator(".idet").isVisible(), "idle row expands and stays open");
    await openMsg(es);
    check(await es.locator("textarea").count() === 1, "expanded idle session has its message box");
    await openMore(es);
    await key(es, "Stop").click();
    await key(es, "Tap again to stop").click();
    await page.waitForTimeout(800);
    check(await es.locator(".fb[data-fb$='|bg']").textContent() === "No answer from the machine" && await es.locator(".fb[data-fb$='|bg']").evaluate(e => e.classList.contains("err")),
      "error text shown in alert ink");
    cmds = await page.evaluate(() => window.__cmds);
    check(cmds[cmds.length - 1].args.session === ERRDEMO, "error demo command sent");

    // Clear finished: sends crew_hub/dismiss with the finished ids; they go once the filtered machine arrives
    await key(sess("Jamfin"), "Clear finished").click();
    await page.waitForFunction(() => window.__dismissed.length >= 1);
    const dm = (await page.evaluate(() => window.__dismissed))[0];
    check(dm.type === "crew_hub/dismiss" && dm.machine === "nas" && dm.session === JAMFIN && !dm.session_dismissed
      && JSON.stringify([...dm.agents].sort()) === JSON.stringify([...JAMFIN_FINISHED].sort()), "Clear finished sends the finished ids (" + dm.agents.length + ")");
    await page.waitForTimeout(700);
    check(await sess("Jamfin").locator("button", {hasText: /finished$/}).count() === 0 && await sess("Jamfin").locator(".thread .ag").count() === 1,
      "finished agents gone after the filtered update");
    check(/^Cleared \d{2}:\d{2}$/.test(await sess("Jamfin").locator(".fb[data-fb$='|clear']").textContent()), "feedback: Cleared HH:MM in place");
    // Hide (×) on an idle row: gone at once, no Undo
    await openFold();
    await row("Echo Show kiosk").locator("button.x").click();
    check(await row("Echo Show kiosk").count() === 0 && await card.locator(".idle .cnt").textContent() === "6", "× hides the row at once: Idle 6");
    await page.waitForFunction(() => window.__dismissed.length >= 2);
    const dx = (await page.evaluate(() => window.__dismissed))[1];
    check(dx.machine === "nas" && dx.session === ECHO && dx.session_dismissed === true && !dx.agents, "× sends session_dismissed");
    await page.waitForTimeout(700);
    check(await row("Echo Show kiosk").count() === 0 && await card.locator(".idle .cnt").textContent() === "6", "hidden row stays gone after the filtered update");
    check(await card.locator("button", {hasText: /^Undo$/}).count() === 0, "no Undo");
    await es.locator(".ib").click(); // collapse again
    await closeFold();

    // New session: "+ New" opens it, fill, survives re-render, two-tap start
    const det = card.locator(".newp");
    const newBtn = card.locator("button.hb", {hasText: "New"});
    check(await det.isHidden() && await newBtn.getAttribute("aria-expanded") === "false", "New session starts closed");
    await newBtn.click();
    await det.locator("select[data-key='new|folder']").selectOption("crew-hub");
    await det.locator("select[data-key='new|mode']").selectOption("auto");
    check((await det.locator("select[data-key='new|machine'] option[disabled]").allTextContents()).join() === "mac (offline)", "offline machine listed greyed out");
    await det.locator("input[data-key='new|name']").fill("Preview start");
    await det.locator("textarea[data-key='new|prompt']").click();
    await page.keyboard.type("Check the card");
    await page.waitForTimeout(1500);
    check(await det.isVisible(), "New session stays open over re-renders");
    check(await det.locator("textarea[data-key='new|prompt']").inputValue() === "Check the card", "prompt kept");
    check(await page.evaluate(() => document.querySelector("crew-hub-card").shadowRoot.activeElement?.dataset.key) === "new|prompt", "prompt keeps focus");
    const startBtn = det.locator("button[data-confirm='new|startbtn']");
    await startBtn.click();
    check(await startBtn.textContent() === "Tap again to start" && await startBtn.evaluate(e => e.classList.contains("dark")), "Start arms");
    await startBtn.click();
    await page.waitForTimeout(800);
    cmds = await page.evaluate(() => window.__cmds);
    const start = cmds[cmds.length - 1];
    check(start.action === "start" && start.machine === "nas" && JSON.stringify(start.args) === JSON.stringify(
      {folder: "crew-hub", model: "claude-opus-5-5", permissionMode: "auto", name: "Preview start", prompt: "Check the card"}), "start shape");
    check(/^Started \d{2}:\d{2} · Preview start$/.test(await det.locator(".fb").textContent()), "start feedback");
    // empty form doesn't arm
    await startBtn.click();
    check(await det.locator(".fb").textContent() === "Name and prompt are needed" && await startBtn.textContent() === "Start", "empty form refused without arming");

    // Feedback clears after 6 s
    await page.waitForTimeout(6300);
    check(await card.locator(".fb.ok, .fb.err").count() === 0, "feedback cleared after 6 s");

    // Layout
    const overflow = async () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(await overflow() <= 0, "no horizontal overflow at 390 px");
    const smallOf = sel => page.evaluate(sel => [...document.querySelector("crew-hub-card").shadowRoot.querySelectorAll(sel)]
      .filter(e => e.offsetParent !== null).map(e => e.getBoundingClientRect().height).filter(h => h < 44).length, sel);
    const small = await smallOf("button, textarea, input[type=text], select, label.tick, [role=button]");
    check(small === 0, `touch targets ≥ 44 px (${small} smaller)`);
    await expand("error demo");
    await page.waitForTimeout(300);
    check(await overflow() <= 0, "no horizontal overflow at 390 px with the idle section and a row open");
    const small2 = await smallOf("button, [role=button]");
    check(small2 === 0, `idle rows, toggles and × ≥ 44 px (${small2} smaller)`);
    await es.locator(".ib").click();
    await page.setViewportSize({width: 1280, height: 900});
    await closeFold();
    await page.waitForTimeout(300);
    check(await overflow() <= 0, "no horizontal overflow at 1280 px");

    check(external.length === 0, "no outside requests" + (external.length ? ": " + external.join(" | ") : ""));
    check(errors.length === 0, "no console errors" + (errors.length ? ": " + errors.join(" | ") : ""));
  } finally {
    await browser.close();
    server.close();
  }
  console.log(failures ? `${failures} check(s) failed` : "all checks passed");
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
