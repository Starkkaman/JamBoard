/* Crew Hub card: renders machine snapshots (docs/DESIGN.md §2) in the Crew Board v4 look.
   No build step, no deps. Snapshot text is untrusted: DOM APIs + textContent only, never innerHTML with data.
   API: el.setSnapshots({<machineId>: snapshot}), setConfig(config), set hass(h), getCardSize().
   In Home Assistant the feed is the crew_hub integration's admin-only websocket subscription (crew_hub/subscribe):
   {"type":"full","machines":{id: {status, snapshot, receivedAt, configured}}} then {"type":"machine","id","data"} per change.
   Commands (docs/COMMANDS.md): with a live hass connection and machine.commands.enabled, each session gets controls for
   the actions in session.controls, sent as hass.connection.sendMessagePromise({type:"crew_hub/command", machine, action, args}).
   The card re-renders on every snapshot (~1/s): session articles and their controls are keyed and kept, controls are
   rebuilt only when their inputs change, and drafts/focus/caret/open state live in Maps on the card.
   0.4.0: an "Active plans" stack (every unfinished plan + plans finished in the last 10 min; a tap scrolls to the session),
   per-session task queues, idle/ended sessions in one collapsed section, finished agents behind a toggle, and
   crew_hub/dismiss {machine, session, agents?, session_dismissed?} for "Clear finished" and the idle rows' Dismiss (x).
   The integration filters dismissed items out of the feed for every device.
   0.5.0: token chips (session meta "312k tok · 94% cached" from session.tokens, agent meta "48k tok" from agent.tokens)
   and a dim "from tasks" tag on plan strips whose plan.source is "tasks" (derived from the built-in task list). */
(() => {
const FONT_HREF = "https://fonts.googleapis.com/css2?family=Londrina+Solid:wght@400;900&family=Jost:wght@400;500;600&display=swap";

const CSS = `
:host{
  --ink:#141210; --slab:#1f1c19; --slab2:#2a2622; --paper:#ece2c9; --dim:#9a8f7c; --ochre:#e5a31b; --red:#d43a1c; --teal:#3f9aa5; --line:#34302a;
  --display:"Londrina Solid","Arial Black",Impact,sans-serif;
  --body:"Jost","Helvetica Neue",Arial,sans-serif;
  color-scheme:dark;
  display:block;background:var(--ink);color:var(--paper);font:15px/1.5 var(--body);
}
*{box-sizing:border-box}
.wrap{max-width:780px;margin:0 auto;padding-inline:16px;padding-block:22px 48px;display:grid;gap:22px}
h1,h2,h3{font-family:var(--display);font-weight:900;text-transform:uppercase;margin:0;line-height:.92;letter-spacing:.01em;text-wrap:balance}
h1{font-size:clamp(2.4rem,9vw,3.6rem)} h1 span{color:var(--ochre)}
h2{font-size:1.45rem} h3{font-size:1.1rem}
.label{font:600 .72rem/1.25 var(--body);letter-spacing:.16em;text-transform:uppercase;color:var(--dim)}
.top{display:grid;gap:10px}
.fresh{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.dot{width:9px;height:9px;background:var(--dim);display:inline-block;flex:none}
.dot.live{background:var(--ochre);animation:pulse 1.6s linear infinite}
@keyframes pulse{50%{opacity:.25}}

/* plan strip */
.flow{background:var(--paper);color:var(--ink);padding:16px 18px 20px;display:grid;gap:10px;clip-path:polygon(0 0,100% 0,100% 82%,97% 100%,0 100%)}
.flow .label{color:#5c5446}
.bar{height:10px;background:#cfc3a6;position:relative}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--red);width:0}
.flowmeta{display:flex;gap:6px 16px;flex-wrap:wrap;font-weight:500;font-variant-numeric:tabular-nums}
.phase{background:var(--ink);color:var(--paper);padding:2px 8px;font:600 .72rem/1.4 var(--body);letter-spacing:.14em;text-transform:uppercase}
.queue{display:grid;gap:4px;margin:0;padding:0;list-style:none}
.queue li{display:flex;gap:10px;align-items:baseline;font-weight:500;min-width:0}
.queue li b{font:600 .66rem/1 var(--body);letter-spacing:.12em;text-transform:uppercase;padding:4px 6px;border:1.5px solid currentColor;flex:none}
.queue li.done{opacity:.55}.queue li span{min-width:0;overflow-wrap:anywhere}
.queue .tier{color:var(--dim);font-weight:400}
.queue li.more{color:var(--dim);font-weight:400;font-size:.85rem}

/* active plans stack: compact paper strips, newest first; a tap scrolls to the plan's session */
.plans{display:grid;gap:10px;min-width:0}
.flow.strip{padding:12px 16px 16px;gap:8px;clip-path:polygon(0 0,100% 0,100% 74%,97% 100%,0 100%)}
.flow.strip[role=button]{cursor:pointer}
.flow.strip:focus-visible{outline:2px solid var(--ochre);outline-offset:2px}
.flow.strip h3{font-size:1.1rem;overflow-wrap:anywhere}
.flow.strip .bar{height:8px}
.flow .where{color:#5c5446;font-size:.8rem;font-weight:400;min-width:0;overflow-wrap:anywhere}
.flow .src{color:#5c5446;font-size:.72rem;font-weight:400;letter-spacing:.06em}
.plans .more{justify-self:start}
.qbox{display:grid;gap:6px;min-width:0}
.qbox .btns{gap:6px}

/* sessions */
.sessions{display:grid;gap:16px}
.sess{background:var(--slab);display:grid;gap:10px;padding:14px 16px 16px 18px;border-left:6px solid var(--line)}
.sess.working{border-left-color:var(--ochre)} .sess.blocked{border-left-color:var(--red)} .sess.failed{border-left-color:var(--red)}
.sess.idle,.sess.completed,.sess.review_ready{border-left-color:var(--teal)}
.row{display:flex;gap:8px 12px;justify-content:space-between;align-items:baseline;flex-wrap:wrap}
.name{min-width:0;overflow-wrap:anywhere}
.chip{font:600 .66rem/1 var(--body);letter-spacing:.14em;text-transform:uppercase;padding:5px 7px;border:1.5px solid currentColor;flex:none}
.c-working{color:var(--ochre)} .c-blocked,.c-failed{color:var(--red)} .c-idle,.c-completed,.c-review_ready,.c-done{color:var(--teal)} .c-queued{color:var(--dim)}
.meta{display:flex;gap:4px 14px;flex-wrap:wrap;color:var(--dim);font-size:.85rem;font-variant-numeric:tabular-nums}
.summary{min-width:0;overflow-wrap:anywhere}
.waiting{border:1.5px solid var(--red);padding:6px 10px;min-width:0;overflow-wrap:anywhere}
.waiting b{color:var(--red);font-weight:600}
.ctx{height:5px;background:var(--line);position:relative;max-width:220px}
.ctx i{position:absolute;inset:0 auto 0 0;background:var(--dim)}
.ctx.hot i{background:var(--red)}

/* agents inside a session */
.agents{display:grid;gap:8px}
.agent{background:var(--slab2);padding:10px 12px;display:grid;gap:6px;border-left:4px solid var(--line)}
.agent.running{border-left-color:var(--ochre)} .agent.done{border-left-color:var(--teal)} .agent.failed{border-left-color:var(--red)}
.agent.stopped{border-left-color:var(--dim)} .c-stopped{color:var(--dim)}
.fin{display:grid;gap:8px;min-width:0}
.fin:empty{display:none}

/* idle and ended sessions: one collapsed section, compact rows that expand into the full block */
.irows{display:grid;gap:8px;margin-top:8px;min-width:0}
.irow{display:grid;gap:8px;min-width:0}
.ihead{display:flex;gap:8px;align-items:stretch;min-width:0}
button.iopen{flex:1;min-width:0;display:grid;gap:2px;text-align:left;background:var(--slab2);color:var(--paper);font:inherit;letter-spacing:normal;text-transform:none;padding:8px 12px;border-left:4px solid var(--teal)}
.irow.blocked .iopen,.irow.failed .iopen{border-left-color:var(--red)}
.iopen .nm{font-weight:600;min-width:0;overflow-wrap:anywhere}
.iopen .im{display:flex;gap:2px 12px;flex-wrap:wrap;color:var(--dim);font-size:.8rem;font-variant-numeric:tabular-nums}
.iopen .is{color:var(--dim);font-size:.85rem;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.iopen[aria-expanded="true"]{background:var(--slab)}
button.x{background:transparent;color:var(--dim);border:1.5px solid var(--line);min-width:44px;padding:0 12px;font-size:1.1rem;letter-spacing:0}
.agent .now{display:flex;gap:8px;align-items:baseline;min-width:0}
.agent .now span{min-width:0;overflow-wrap:anywhere}
.trail{margin:0;padding:0;list-style:none;display:grid;gap:2px;color:var(--dim);font-size:.85rem}
.trail li{display:flex;gap:8px;min-width:0}.trail li span{min-width:0;overflow-wrap:anywhere}
.trail time{font-variant-numeric:tabular-nums;flex:none}
.muted{color:var(--dim);font-size:.85rem}
.empty{color:var(--dim);background:var(--slab);padding:16px}
[hidden]{display:none!important}
.slot{display:contents}

/* commands */
button{font:600 .8rem/1 var(--body);letter-spacing:.12em;text-transform:uppercase;background:var(--ochre);color:var(--ink);border:0;padding:11px 14px;cursor:pointer;min-height:40px;border-radius:0}
button:focus-visible,summary:focus-visible{outline:2px solid var(--paper);outline-offset:2px}
button:disabled{opacity:.5;cursor:default}
.btn-o{background:transparent;color:var(--paper);border:1.5px solid currentColor}
.btn-o.sm{font-size:.72rem;padding:8px 12px}
.btn-o[aria-pressed="true"]{background:var(--ochre);color:var(--ink);border-color:var(--ochre)}
button.armed{background:var(--red);color:var(--paper);border-color:var(--red)}
.in{background:var(--slab2);color:var(--paper);border:1.5px solid var(--line);padding:8px 10px;font:inherit;min-height:40px;width:100%;min-width:0;border-radius:0;margin:0}
.in:focus-visible{outline:2px solid var(--paper);outline-offset:0}
textarea.in{resize:none;display:block;line-height:1.5}
textarea.in.tall{resize:vertical;min-height:88px}
.ctl{display:grid;gap:10px;min-width:0}
.grp{display:grid;gap:8px;min-width:0}
.line{display:flex;gap:8px;align-items:flex-end;min-width:0}
.line>.in{flex:1}
.btns{display:flex;gap:8px;flex-wrap:wrap;align-items:center;min-width:0}
.check{display:inline-flex;gap:8px;align-items:center;min-height:40px;cursor:pointer;font-weight:500}
.check input{width:20px;height:20px;margin:0;accent-color:var(--ochre)}
.hint{color:var(--dim);font-size:.8rem}
.q{display:grid;gap:6px;min-width:0}
.q>div:first-child{overflow-wrap:anywhere}
.fb{font-size:.85rem;overflow-wrap:anywhere}
.fb:empty{display:none}
.fb.ok{color:var(--teal)} .fb.err{color:var(--red)}
.diag{background:var(--slab);padding:10px 14px}
.diag summary{cursor:pointer;min-height:40px;display:flex;align-items:center}
.form{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr));margin-top:8px}
.field{display:grid;gap:4px;min-width:0}
.form>.wide{grid-column:1/-1}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
`;

const SESS = {working: ["working", "working"], waiting: ["blocked", "needs you"], idle: ["idle", "idle"], ended: ["completed", "done"]};
const ORDER = {working: 0, waiting: 1, idle: 2, ended: 3};
const TASK = {pending: "queued", in_progress: "running", completed: "done"};
const ACTIVE = new Set(["working", "waiting"]);  // full blocks; everything else goes into the idle section
const AGENT = ["running", "done", "failed", "stopped"];
const PLAN_TASK = ["queued", "running", "done", "failed"];
const FINISHED_PLAN_MS = 10 * 60 * 1000;  // finished plans stay in the stack this long
const MAX_STRIPS = 4, MAX_IDLE = 40, MAX_FINISHED = 20;

// Times arrive as epoch ms (crew-hubd snapshots) or ISO strings (fixtures).
const ms = v => { const t = typeof v === "number" ? v : Date.parse(v); return Number.isFinite(t) ? t : null; };
function dur(a, b) {
  const ta = ms(a); if (ta == null) return "";
  const s = Math.max(0, Math.round(((b ? ms(b) ?? Date.now() : Date.now()) - ta) / 1000)), m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? `${h} h ${m % 60} min` : m ? `${m} min ${s % 60} s` : `${s} s`;
}
function agoMin(iso) {
  const t = ms(iso); if (t == null) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? `${h} h ${m % 60} min ago` : `${m} min ago`;
}
function hm(iso) { const t = ms(iso); if (t == null) return ""; try { return new Date(t).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false}); } catch { return ""; } }
const str = v => (v == null ? "" : String(v));
const arr = v => (Array.isArray(v) ? v : []);
const cmpDesc = (a, b) => (ms(b) ?? 0) - (ms(a) ?? 0);
// Compact token counts: 950, 4.2k, 312k, 1.4M, 12M.
function fmtTok(n) {
  n = Math.max(0, Math.round(Number(n) || 0));
  if (n < 1000) return String(n);
  if (n < 9950) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  if (n < 999500) return Math.round(n / 1000) + "k";
  if (n < 9950000) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  return Math.round(n / 1e6) + "M";
}
// "312k tok" (+ " · 94% cached" when a cache-hit ratio is given); "" without token data.
function tokText(t, withHit) {
  if (!t || typeof t !== "object") return "";
  const sum = ["in", "out", "cacheRead", "cacheWrite"].reduce((a, k) => a + (Number(t[k]) || 0), 0);
  if (!(sum > 0)) return "";
  const hit = Number(t.cacheHit);
  return fmtTok(sum) + " tok" + (withHit && t.cacheHit != null && Number.isFinite(hit) ? ` · ${Math.round(hit * 100)}% cached` : "");
}

class CrewHubCard extends HTMLElement {
  constructor() {
    super();
    this._snaps = {};
    this._config = {};
    this._hass = null;
    this._tickers = [];
    this._timer = null;
    this._hub = {};          // machineId -> store entry from the integration
    this._subToken = null;   // identifies the live subscription; callbacks from an older one are ignored
    this._subPromise = null; // Promise<unsubscribe fn> from connection.subscribeMessage
    this._subConn = null;
    this._hubError = false;  // integration missing, not loaded, or user not admin
    this._errConn = null;    // connection that refused the subscription
    this._onReady = () => { if (this._hubError) this._subscribe(); };
    this.attachShadow({mode: "open"});
    const style = document.createElement("style");
    style.textContent = CSS;
    this._root = this._el("div", "wrap");
    // Persistent layout: slots are rebuilt per render, the New-session box and session articles are kept and patched.
    this._headSlot = this._el("div", "slot");
    this._noteSlot = this._el("div", "slot");
    this._newSlot = this._el("div", "slot");
    this._flowSlot = this._el("div", "slot");
    this._sessBox = this._el("section", "sessions");
    this._sessBox.setAttribute("aria-live", "polite");
    this._emptyEl = this._el("div", "empty");
    // Idle and ended sessions: one collapsed section of compact rows (kept and patched like the articles).
    this._idleDet = this._el("details", "diag idle");
    this._idleSum = this._el("summary", "label");
    this._idleSum.dataset.key = "idle|summary";
    this._idleList = this._el("div", "irows");
    this._idleDet.append(this._idleSum, this._idleList);
    this._idleDet.hidden = true;
    this._idleOpen = false;
    this._idleDet.addEventListener("toggle", () => { this._idleOpen = this._idleDet.open; });
    this._root.append(this._headSlot, this._noteSlot, this._newSlot, this._flowSlot, this._sessBox, this._idleDet);
    this.shadowRoot.append(style, this._root);
    // Command UI state that must survive re-renders.
    this._arts = new Map();    // "mid|sid" -> {el, top, ctl, bottom, parts}
    this._drafts = new Map();  // data-key -> typed value / checkbox state / selected labels
    this._fb = new Map();      // feedback key -> {text, cls, timer}
    this._confirm = new Map(); // two-tap key -> timer
    this._busy = new Set();    // feedback keys with a command in flight
    this._newOpen = false;
    this._newSig = null;
    this._rows = new Map();    // "mid|sid" -> idle row parts
    this._open = new Set();    // open toggles: "plans", "row|<key>", "q|<key>" (done tasks), "fin|<key>" (finished agents)
    this._planSig = null;
  }

  connectedCallback() {
    CrewHubCard.loadFonts();
    this._render();
    if (!this._timer) this._timer = setInterval(() => this._tick(), 1000);
    if (this._hass && !this._subToken) this._subscribe();
  }
  disconnectedCallback() { clearInterval(this._timer); this._timer = null; this._unsubscribe(); }

  static loadFonts() {
    if (document.querySelector("link[data-crew-hub-fonts]")) return;
    const pre1 = document.createElement("link"); pre1.rel = "preconnect"; pre1.href = "https://fonts.googleapis.com";
    const pre2 = document.createElement("link"); pre2.rel = "preconnect"; pre2.href = "https://fonts.gstatic.com"; pre2.crossOrigin = "";
    const l = document.createElement("link"); l.rel = "stylesheet"; l.href = FONT_HREF; l.dataset.crewHubFonts = "1";
    document.head.append(pre1, pre2, l);
  }

  /* ---- public API ---- */
  setSnapshots(obj) { this._snaps = obj && typeof obj === "object" ? obj : {}; this._render(); }
  setConfig(config) { this._config = Object.assign({}, config || {}); if (this.isConnected) this._render(); }
  set hass(h) {
    const prev = this._hass;
    this._hass = h;
    if (!h || !h.connection) return;
    if (prev && prev.connection !== h.connection) this._unsubscribe();
    // hass is set on every state change: subscribe once per connection, and don't retry a refused one here.
    if (this.isConnected && !this._subToken && !(this._hubError && this._errConn === h.connection)) this._subscribe();
  }
  get hass() { return this._hass; }
  getCardSize() {
    const n = Math.min(12, this._sessions().filter(x => ACTIVE.has(x.s.status)).length);
    return 6 + n * 3;
  }

  /* ---- integration feed ---- */
  _subscribe() {
    const conn = this._hass && this._hass.connection;
    if (!conn || typeof conn.subscribeMessage !== "function") return;
    this._unsubscribe();
    const token = {};
    this._subToken = token;
    this._subConn = conn;
    // home-assistant-js-websocket re-sends the subscription after a reconnect (resubscribe), and the integration
    // answers with a fresh "full". "ready" only retries a subscription that failed (e.g. HA was still starting).
    if (typeof conn.addEventListener === "function") conn.addEventListener("ready", this._onReady);
    let p;
    try {
      p = Promise.resolve(conn.subscribeMessage(msg => { if (this._subToken === token) this._onHub(msg); },
        {type: "crew_hub/subscribe"}, {resubscribe: true}));
    } catch (e) { p = Promise.reject(e); }
    this._subPromise = p;
    p.then(() => {
      if (this._subToken === token && this._hubError) { this._hubError = false; this._render(); }
    }, () => {
      if (this._subToken !== token) return;
      this._subToken = null; this._subPromise = null;
      this._hubError = true; this._errConn = conn;
      this._render();
    });
  }
  _unsubscribe() {
    const p = this._subPromise, conn = this._subConn;
    this._subToken = null; this._subPromise = null; this._subConn = null;
    if (conn && typeof conn.removeEventListener === "function") conn.removeEventListener("ready", this._onReady);
    if (p) p.then(unsub => { try { if (typeof unsub === "function") unsub(); } catch (e) { /* connection gone */ } }, () => {});
  }
  _onHub(msg) {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "full") {
      this._hub = {};
      const m = msg.machines;
      if (m && typeof m === "object") for (const [id, d] of Object.entries(m)) if (d && typeof d === "object") this._hub[id] = d;
    } else if (msg.type === "machine" && typeof msg.id === "string") {
      if (msg.data && typeof msg.data === "object") this._hub[msg.id] = msg.data; else delete this._hub[msg.id];
    } else return;
    this._hubError = false;
    const snaps = {};
    for (const [id, d] of Object.entries(this._hub)) {
      if (!d.snapshot || typeof d.snapshot !== "object") continue;
      // The card reads snapshot.status for "computer offline"; the store's MQTT status is the truth.
      snaps[id] = Object.assign({}, d.snapshot, {status: str(d.status || "unknown"), configured: d.configured !== false});
    }
    this.setSnapshots(snaps);
  }

  /* ---- helpers ---- */
  _el(t, c, x) { const e = document.createElement(t); if (c) e.className = c; if (x != null) e.textContent = str(x); return e; }
  _tickText(node, fn) { node.textContent = fn(); this._tickers.push(() => { node.textContent = fn(); }); return node; }
  _tick() { for (const f of this._tickers) f(); }

  _sessions() {
    const out = [];
    for (const [mid, snap] of Object.entries(this._snaps)) {
      if (!snap || typeof snap !== "object") continue;
      const m = snap.machine || {};
      const offline = str(snap.status || m.status).toLowerCase() === "offline";
      const unconfigured = snap.configured === false;
      const mname = str(m.name || m.id || mid);
      const canCmd = !!(m.commands && m.commands.enabled === true) && !offline && !unconfigured;
      for (const s of arr(snap.sessions)) if (s && typeof s === "object") out.push({s, mid, mname, offline, unconfigured, canCmd});
    }
    return out;
  }

  /* ---- render ---- */
  _render() {
    const focus = this._captureFocus();
    this._tickers = [];
    this._headSlot.replaceChildren(this._renderHeader());
    this._noteSlot.replaceChildren(...(this._hubError ? [this._el("div", "muted", "Crew Hub integration not available")] : []));
    this._syncNew();
    this._syncPlans();
    this._syncSessions();
    this._restoreFocus(focus);
  }

  // Fallback for the rare rebuild of a focused control (session reordered, waiting prompt changed, machine changed).
  _captureFocus() {
    const a = this.shadowRoot.activeElement;
    if (!a || !a.dataset || !a.dataset.key) return null;
    const f = {key: a.dataset.key, top: a.scrollTop};
    try { if (typeof a.selectionStart === "number") { f.s = a.selectionStart; f.e = a.selectionEnd; f.d = a.selectionDirection; } } catch (e) { /* no selection */ }
    return f;
  }
  _restoreFocus(f) {
    if (!f) return;
    const cur = this.shadowRoot.activeElement;
    if (cur && cur.dataset && cur.dataset.key === f.key) return;
    const el = this._find("key", f.key)[0];
    if (!el) return;
    el.focus({preventScroll: true});
    try { if (f.s != null) el.setSelectionRange(f.s, f.e, f.d); } catch (e) { /* not a text field */ }
    el.scrollTop = f.top;
  }
  _find(attr, val) { return [...this.shadowRoot.querySelectorAll(`[data-${attr}]`)].filter(e => e.dataset[attr] === val); }

  _renderHeader() {
    const top = this._el("header", "top");
    const h1 = this._el("h1", null, "Crew ");
    h1.append(this._el("span", null, "Board"));
    let newest = null;
    for (const snap of Object.values(this._snaps)) {
      const u = snap && snap.machine && snap.machine.updatedAt;
      if (ms(u) != null && (newest == null || ms(u) > ms(newest))) newest = u;
    }
    const fresh = this._el("div", "fresh label");
    const dot = this._el("i", "dot" + (newest ? " live" : ""));
    const txt = this._el("span");
    if (newest) this._tickText(txt, () => "Updated " + dur(newest) + " ago");
    else txt.textContent = Object.keys(this._snaps).length ? "No update time reported" : "Waiting for snapshots…";
    fresh.append(dot, txt);
    top.append(this._el("span", "label", "Claude sessions · live"), h1, fresh);
    return top;
  }

  _key(x) { return x.mid + "|" + str(x.s.id || x.s.name); }
  _toggle(k) { if (this._open.has(k)) this._open.delete(k); else this._open.add(k); this._render(); }

  /* ---- active plans ---- */
  _plans() {
    const out = [], now = Date.now();
    for (const x of this._sessions()) {
      const p = x.s.plan;
      if (!p || typeof p !== "object") continue;
      const fin = p.finished === true, at = ms(p.updatedAt);
      if (fin && (at == null || now - at > FINISHED_PLAN_MS)) continue;
      const tasks = arr(p.tasks).filter(t => t && typeof t === "object");
      const total = Math.max(0, Number(p.total ?? tasks.length) || 0);
      const done = fin ? total : Math.min(Math.max(0, Number(p.done ?? tasks.filter(t => t.status === "done").length) || 0), total);
      out.push({key: this._key(x), title: str(p.title) || "Untitled plan", phase: fin ? "finished" : str(p.phase || "running"),
        done, total, fin, where: [x.s.name || x.s.project || "Session", x.mname].map(str).join(" · "), at: at ?? 0,
        tasks: p.source === "tasks"});
    }
    // Unfinished plans first, newest first; then the recently finished ones.
    return out.sort((a, b) => (a.fin - b.fin) || (b.at - a.at));
  }

  _syncPlans() {
    const plans = this._plans(), more = this._open.has("plans");
    const sig = JSON.stringify([plans.map(p => [p.key, p.title, p.phase, p.done, p.total, p.where, p.tasks]), more]);
    if (sig === this._planSig) return;
    this._planSig = sig;
    const box = this._el("section", "plans");
    box.setAttribute("aria-label", "Active plans");
    box.append(this._el("span", "label", "Active plans"));
    if (!plans.length) box.append(this._strip(null));
    (more ? plans : plans.slice(0, MAX_STRIPS)).forEach(p => box.append(this._strip(p)));
    if (plans.length > MAX_STRIPS) {
      const b = this._btn(more ? "Show fewer" : `+${plans.length - MAX_STRIPS} more`, "btn-o sm more", "plans|more", null, () => this._toggle("plans"));
      b.setAttribute("aria-expanded", String(more));
      box.append(b);
    }
    this._flowSlot.replaceChildren(box);
  }

  _strip(p) {
    const sec = this._el("div", "flow strip" + (p && p.fin ? " finished" : ""));
    sec.append(this._el("h3", null, p ? p.title : "No plan running"));
    const bar = this._el("div", "bar"), i = this._el("i");
    i.style.width = p && p.total ? (100 * p.done / p.total) + "%" : "0";
    bar.append(i);
    const meta = this._el("div", "flowmeta");
    meta.append(this._el("span", "phase", p ? p.phase : "idle"));
    if (p && p.total) meta.append(this._el("span", null, `${p.done} of ${p.total} accepted`));
    if (p) meta.append(this._el("span", "where", p.where));
    if (p && p.tasks) meta.append(this._el("span", "src", "from tasks"));
    sec.append(bar, meta);
    if (p) {
      sec.tabIndex = 0;
      sec.setAttribute("role", "button");
      sec.dataset.key = "plan|" + p.key;
      sec.dataset.plan = p.key;
      sec.addEventListener("click", () => this._goto(p.key));
      sec.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this._goto(p.key); } });
    }
    return sec;
  }

  // Scroll to a session's block; an idle one is opened first (idle section + its row).
  _goto(key) {
    const x = this._sessions().find(v => this._key(v) === key);
    if (!x) return;
    if (!ACTIVE.has(x.s.status)) { this._idleOpen = true; this._open.add("row|" + key); this._render(); }
    const a = this._arts.get(key);
    if (!a) return;
    let smooth = true;
    try { smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { /* old browser */ }
    a.el.scrollIntoView({behavior: smooth ? "smooth" : "auto", block: "start"});
  }

  /* ---- sessions ---- */
  _syncSessions() {
    const list = this._sessions().sort((a, b) =>
      ((ORDER[a.s.status] ?? 2) - (ORDER[b.s.status] ?? 2)) || cmpDesc(a.s.updatedAt, b.s.updatedAt));
    const active = [], idle = [], seen = new Set();
    for (const x of list) {
      x.key = this._key(x);
      if (seen.has(x.key)) continue;
      seen.add(x.key);
      (ACTIVE.has(x.s.status) ? active : idle).push(x);
    }
    const keepArts = new Set();
    const art = x => {
      let a = this._arts.get(x.key);
      if (!a) {
        a = {el: this._el("article"), top: this._el("div", "slot"), q: this._el("div", "slot"), mid: this._el("div", "slot"),
          ctl: this._el("div", "ctl"), bottom: this._el("div", "slot"), fin: this._el("div", "fin"), parts: {}, qSig: null, finSig: null};
        a.el.dataset.sess = x.key;
        a.el.append(a.top, a.q, a.mid, a.ctl, a.bottom, a.fin);
        this._arts.set(x.key, a);
      }
      keepArts.add(x.key);
      this._fillSession(a, x, x.key);
      return a.el;
    };
    const want = active.slice(0, 12).map(art);
    if (!want.length) {
      this._emptyEl.textContent = idle.length ? "No session is working right now."
        : "Your Claude Code sessions appear here, working ones first, with every agent inside them and what each is doing right now.";
      want.push(this._emptyEl);
    }
    this._place(this._sessBox, want);

    const rows = [], keepRows = new Set();
    for (const x of idle.slice(0, MAX_IDLE)) {
      const r = this._row(x.key);
      const exp = this._open.has("row|" + x.key);
      this._fillRow(r, x, exp);
      keepRows.add(x.key);
      this._place(r.el, exp ? [r.head, r.fb, art(x)] : [r.head, r.fb]);
      rows.push(r.el);
    }
    for (const [k, r] of this._rows) if (!keepRows.has(k)) { r.el.remove(); this._rows.delete(k); }
    for (const k of [...this._open]) if (k.startsWith("row|") && !seen.has(k.slice(4))) this._open.delete(k);
    this._place(this._idleList, rows);
    this._idleSum.textContent = `Idle sessions (${idle.length})`;
    this._idleDet.hidden = !idle.length;
    if (this._idleDet.open !== this._idleOpen) this._idleDet.open = this._idleOpen;
    for (const [k, a] of this._arts) if (!keepArts.has(k)) { a.el.remove(); this._arts.delete(k); }
  }

  // Make box's children exactly `want`, moving only what is out of place so a focused control isn't detached.
  _place(box, want) {
    for (const c of [...box.children]) if (!want.includes(c)) c.remove();
    want.forEach((el, i) => { if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null); });
  }

  _row(key) {
    let r = this._rows.get(key);
    if (r) return r;
    r = {el: this._el("div", "irow"), head: this._el("div", "ihead"), btn: this._el("button", "iopen"),
      nm: this._el("span", "nm"), im: this._el("span", "im"), is: this._el("span", "is"), xb: null, fb: this._fbNode("dis|" + key), cur: null};
    r.btn.type = "button";
    r.btn.dataset.key = "row|" + key;
    r.btn.append(r.nm, r.im, r.is);
    r.btn.addEventListener("click", () => this._toggle("row|" + key));
    r.head.append(r.btn);
    this._rows.set(key, r);
    return r;
  }

  _fillRow(r, x, exp) {
    const {s, mname, offline} = x, key = x.key, fk = "dis|" + key;
    const [cls] = SESS[s.status] || SESS.idle;
    r.cur = x;
    r.el.className = "irow " + cls;
    r.btn.setAttribute("aria-expanded", String(exp));
    const name = str(s.name || s.project || s.id || "Session");
    r.nm.textContent = name;
    const im = [this._el("span", null, mname)];
    if (s.status === "ended") im.push(this._el("span", null, "ended"));
    if (s.updatedAt) { const sp = this._el("span"); this._tickText(sp, () => "active " + agoMin(s.updatedAt)); im.push(sp); }
    if (offline) im.push(this._el("span", null, "computer offline"));
    r.im.replaceChildren(...im);
    r.is.textContent = str(s.summary);
    r.is.hidden = exp || !s.summary;
    const canDismiss = this._live() && !!s.id;
    if (canDismiss && !r.xb) {
      r.xb = this._btn("×", "btn-o x", fk, fk, () => {
        const c = r.cur;
        if (c) this._dismiss(fk, {machine: c.mid, session: str(c.s.id), session_dismissed: true});
      });
      r.head.append(r.xb);
    } else if (!canDismiss && r.xb) { r.xb.remove(); r.xb = null; }
    if (r.xb) { r.xb.setAttribute("aria-label", "Dismiss " + name); r.xb.title = "Dismiss"; }
  }

  _fillSession(a, x, key) {
    const {s, mname, offline, unconfigured} = x;
    const [cls, label] = SESS[s.status] || SESS.idle;
    a.el.className = "sess " + cls;
    const r = this._el("div", "row");
    r.append(this._el("h3", "name", s.name || s.project || s.id || "Session"), this._el("span", "chip c-" + cls, label));
    a.top.replaceChildren(r);
    if (s.summary) a.top.append(this._el("div", "summary", s.summary));
    this._syncQueue(a, s, key);
    const box = a.mid;
    box.replaceChildren();
    if (s.status === "waiting" && s.waiting) {
      const w = this._el("div", "waiting");
      w.append(this._el("b", null, "Waiting: "));
      const parts = [s.waiting.tool, s.waiting.text].map(str).filter(Boolean);
      w.append(parts.join(" — ") || str(s.waiting.kind) || "input");
      box.append(w);
    }
    const m = this._el("div", "meta");
    m.append(this._el("span", null, mname));
    if (s.localName && str(s.localName) !== str(s.name)) m.append(this._el("span", null, str(s.localName)));
    if (s.model) m.append(this._el("span", null, str(s.model).replace(/^claude-/, "")));
    if (typeof s.costUsd === "number" && isFinite(s.costUsd)) m.append(this._el("span", null, "$" + s.costUsd.toFixed(2)));
    const tk = tokText(s.tokens, true);
    if (tk) m.append(this._el("span", "tok", tk));
    if (s.updatedAt) { const sp = this._el("span"); this._tickText(sp, () => "active " + agoMin(s.updatedAt)); m.append(sp); }
    if (offline) m.append(this._el("span", null, "computer offline"));
    if (unconfigured) m.append(this._el("span", null, "unconfigured"));
    box.append(m);
    this._syncControls(a, x, key);
    const bottom = a.bottom;
    bottom.replaceChildren();
    const cx = s.ctx;
    if (cx && Number(cx.max) > 0) {
      const pct = Math.max(0, Math.min(100, Math.round(100 * (Number(cx.used) || 0) / Number(cx.max))));
      const c = this._el("div", "ctx" + (pct > 85 ? " hot" : "")), i = this._el("i");
      i.style.width = pct + "%"; c.append(i);
      bottom.append(this._el("span", "label", `Context ${pct}%`), c);
    }
    const tasks = new Map(arr(s.plan && s.plan.tasks).filter(t => t && t.agentId).map(t => [t.agentId, t]));
    // Agents with no title and no steps (a start with nothing to show yet) aren't rendered.
    const agents = arr(s.agents).filter(g => g && typeof g === "object"
      && (g.title || (tasks.get(g.id) || {}).title || Number(g.steps) > 0));
    const running = agents.filter(g => g.status === "running")
      .sort((p, q) => cmpDesc(p.current?.at || p.startedAt, q.current?.at || q.startedAt));
    if (running.length) {
      const ag = this._el("div", "agents");
      running.slice(0, 8).forEach(g => ag.append(this._renderAgent(g, tasks.get(g.id))));
      bottom.append(ag);
    }
    this._syncFinished(a, x, key, agents.filter(g => g.status !== "running"), tasks);
  }

  // Task queue under the summary: running/failed tasks + the next 3 queued; done ones behind an "N done" toggle.
  _syncQueue(a, s, key) {
    const p = s.plan && typeof s.plan === "object" ? s.plan : null;
    const items = p
      ? arr(p.tasks).filter(t => t && typeof t === "object").sort((u, v) => (u.order ?? 99) - (v.order ?? 99))
        .map(t => ({title: str(t.title || t.id), tier: str(t.tier), status: PLAN_TASK.includes(t.status) ? t.status : "queued"}))
      : arr(s.tasks).filter(t => t && typeof t === "object")
        .map(t => ({title: str(t.subject || t.id), tier: "", status: TASK[t.status] || "queued"}));
    const qk = "q|" + key, open = this._open.has(qk);
    const sig = items.length ? JSON.stringify([items, open]) : "";
    if (sig === a.qSig) return;
    a.qSig = sig;
    if (!sig) { a.q.replaceChildren(); return; }
    const li = it => {
      const lab = it.status === "running" ? "working" : it.status;
      const el = this._el("li", it.status === "done" ? "done" : "");
      const span = this._el("span", null, it.title);
      if (it.tier) span.append(" ", this._el("span", "tier", it.tier));
      el.append(this._el("b", "c-" + lab, lab), span);
      return el;
    };
    const queued = items.filter(i => i.status === "queued"), done = items.filter(i => i.status === "done");
    const now = [...items.filter(i => i.status === "running" || i.status === "failed"), ...queued.slice(0, 3)];
    const box = this._el("div", "qbox");
    if (now.length) {
      const ul = this._el("ul", "queue");
      now.forEach(it => ul.append(li(it)));
      if (queued.length > 3) ul.append(this._el("li", "more", `+${queued.length - 3} more queued`));
      box.append(ul);
    }
    if (done.length) {
      const b = this._btn(`${done.length} done`, "btn-o sm", key + "|qdone", null, () => this._toggle(qk));
      b.setAttribute("aria-expanded", String(open));
      const row = this._el("div", "btns");
      row.append(b);
      box.append(row);
      if (open) { const ul = this._el("ul", "queue"); done.forEach(it => ul.append(li(it))); box.append(ul); }
    }
    a.q.replaceChildren(box);
  }

  // Finished agents (done | failed | stopped) behind an "N finished" toggle, with "Clear finished" (crew_hub/dismiss).
  // Rebuilt only when they change, so taps on these buttons aren't lost to the once-a-second re-render.
  _syncFinished(a, x, key, fin, tasks) {
    const fk = key + "|clear", open = this._open.has("fin|" + key), live = this._live() && !!x.s.id;
    fin.sort((p, q) => cmpDesc(p.endedAt || p.current?.at || p.startedAt, q.endedAt || q.current?.at || q.startedAt));
    const sig = fin.length ? JSON.stringify([fin, fin.map(g => tasks.get(g.id) || null), open, live]) : "";
    if (sig === a.finSig) return;
    a.finSig = sig;
    if (!sig) { a.fin.replaceChildren(); return; }
    const btns = this._el("div", "btns");
    const t = this._btn(`${fin.length} finished`, "btn-o sm", key + "|fin", null, () => this._toggle("fin|" + key));
    t.setAttribute("aria-expanded", String(open));
    btns.append(t);
    if (live) {
      btns.append(this._btn("Clear finished", "btn-o sm", fk, fk, () => {
        const ids = fin.map(g => str(g.id)).filter(Boolean);
        if (ids.length) this._dismiss(fk, {machine: x.mid, session: str(x.s.id), agents: ids});
      }));
    }
    const kids = [btns, this._fbNode(fk)];
    if (open) {
      const ag = this._el("div", "agents");
      fin.slice(0, MAX_FINISHED).forEach(g => ag.append(this._renderAgent(g, tasks.get(g.id))));
      kids.push(ag);
    }
    a.fin.replaceChildren(...kids);
  }

  /* ---- commands (docs/COMMANDS.md) ---- */
  _live() {
    const c = this._hass && this._hass.connection;
    return !!(c && typeof c.sendMessagePromise === "function" && this._subToken && !this._hubError);
  }

  // Keep a control block while its signature is unchanged; rebuild it only when its inputs change.
  _part(a, name, sig, build) {
    if (!sig) { delete a.parts[name]; return; }
    if (a.parts[name] && a.parts[name].sig === sig) return;
    a.parts[name] = {sig, node: build()};
  }

  _syncControls(a, x, key) {
    const s = x.s, allowed = arr(s.controls).filter(v => typeof v === "string");
    const on = this._live() && x.canCmd;
    const has = act => on && allowed.includes(act);
    const w = s.waiting;
    const ans = has("answer") && s.status === "waiting" && w && typeof w === "object" && w.hubCanAnswer === true && w.requestId ? w : null;
    this._part(a, "ans", ans ? JSON.stringify([w.kind, w.requestId, w.tool, w.text, w.questions]) : "", () => this._buildAnswer(x, key, w));
    this._part(a, "msg", has("send") ? "send" : "", () => this._buildMsg(x, key));
    const bg = ["interrupt", "stop"].filter(has);
    this._part(a, "bg", bg.join(","), () => this._buildBg(x, key, bg));
    const nodes = ["ans", "msg", "bg"].map(n => a.parts[n] && a.parts[n].node).filter(Boolean);
    for (const c of [...a.ctl.children]) if (!nodes.includes(c)) c.remove();
    nodes.forEach((el, i) => { if (a.ctl.children[i] !== el) a.ctl.insertBefore(el, a.ctl.children[i] || null); });
    a.ctl.hidden = !nodes.length;
  }

  _input(tag, key, attrs) {
    const el = this._el(tag, "in");
    el.dataset.key = key;
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    el.value = str(this._drafts.get(key));
    el.addEventListener("input", () => { this._drafts.set(key, el.value); if (el.dataset.grow) this._grow(el); });
    return el;
  }
  _grow(el) {
    el.style.height = "auto";
    const cs = getComputedStyle(el), line = parseFloat(cs.lineHeight) || 22.5;
    const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) + parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
    el.style.height = Math.min(el.scrollHeight + parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth), line * 3 + pad) + "px";
  }
  _btn(text, cls, key, grp, onClick) {
    const b = this._el("button", cls || null, text);
    b.type = "button";
    if (key) b.dataset.key = key;
    if (grp) { b.dataset.grp = grp; b.disabled = this._busy.has(grp); }
    b.addEventListener("click", onClick);
    return b;
  }
  _fbNode(fk) {
    const f = this._el("div", "fb");
    f.dataset.fb = fk;
    f.setAttribute("role", "status");
    const cur = this._fb.get(fk);
    if (cur) { f.textContent = cur.text; if (cur.cls) f.classList.add(cur.cls); }
    return f;
  }
  _setFb(fk, text, cls) {
    const prev = this._fb.get(fk);
    if (prev && prev.timer) clearTimeout(prev.timer);
    const entry = {text, cls, timer: null};
    if (cls) entry.timer = setTimeout(() => { this._fb.delete(fk); this._paintFb(fk); }, 6000);
    this._fb.set(fk, entry);
    this._paintFb(fk);
  }
  _paintFb(fk) {
    const cur = this._fb.get(fk);
    for (const f of this._find("fb", fk)) {
      f.textContent = cur ? cur.text : "";
      f.classList.toggle("ok", !!cur && cur.cls === "ok");
      f.classList.toggle("err", !!cur && cur.cls === "err");
    }
  }
  _setBusy(fk, on) {
    if (on) this._busy.add(fk); else this._busy.delete(fk);
    for (const b of this._find("grp", fk)) b.disabled = on;
  }

  // Two-tap confirm: the first tap arms the button ("Confirm stop?", red) for 4 s, the second tap runs it.
  // guard() returns an error text when the action can't be sent yet; then the button isn't armed.
  _twoTap(text, armedText, cls, key, grp, run, guard) {
    const b = this._btn(text, cls, key, grp, () => {
      if (this._confirm.has(key)) { this._disarm(key); run(); return; }
      const err = guard && guard();
      if (err) { this._setFb(grp, err, "err"); return; }
      const t = setTimeout(() => this._disarm(key), 4000);
      this._confirm.set(key, t);
      this._paintArm(key);
    });
    b.dataset.confirm = key; b.dataset.label = text; b.dataset.armed = armedText;
    this._paintArm(key, b);
    return b;
  }
  _disarm(key) { clearTimeout(this._confirm.get(key)); this._confirm.delete(key); this._paintArm(key); }
  _paintArm(key, only) {
    const armed = this._confirm.has(key);
    for (const b of only ? [only] : this._find("confirm", key)) {
      b.textContent = armed ? b.dataset.armed : b.dataset.label;
      b.classList.toggle("armed", armed);
    }
  }

  async _send(fk, machine, action, args) {
    const conn = this._hass && this._hass.connection;
    if (!conn || typeof conn.sendMessagePromise !== "function") { this._setFb(fk, "Not connected to Home Assistant", "err"); return false; }
    this._setBusy(fk, true);
    this._setFb(fk, "Sending…", "");
    let ok = false;
    try {
      const r = await conn.sendMessagePromise({type: "crew_hub/command", machine, action, args});
      if (r && r.ok === true) { ok = true; this._setFb(fk, "Sent ✓", "ok"); }
      else this._setFb(fk, CrewHubCard.errText(r && r.error, r && r.detail), "err");
    } catch (e) {
      this._setFb(fk, CrewHubCard.errText(e && e.code, e && e.message), "err");
    } finally {
      this._setBusy(fk, false);
    }
    return ok;
  }
  // crew_hub/dismiss: on success the integration pushes the filtered machine, which removes the items.
  async _dismiss(fk, payload) {
    const conn = this._hass && this._hass.connection;
    if (!conn || typeof conn.sendMessagePromise !== "function") { this._setFb(fk, "Not connected to Home Assistant", "err"); return false; }
    this._setBusy(fk, true);
    let ok = false;
    try {
      const r = await conn.sendMessagePromise(Object.assign({type: "crew_hub/dismiss"}, payload));
      ok = !!(r && r.ok === true);
      if (!ok) this._setFb(fk, CrewHubCard.errText(r && r.error, r && r.detail), "err");
    } catch (e) {
      this._setFb(fk, CrewHubCard.errText(e && e.code, e && e.message), "err");
    } finally {
      this._setBusy(fk, false);
    }
    return ok;
  }
  static errText(code, detail) {
    const known = {
      not_configured: "This machine isn't set up in Crew Hub", commands_disabled: "Commands are off on this machine",
      offline: "Computer offline", not_allowed: "Not allowed for this session", unknown_session: "Session not found",
      unknown_action: "Unknown command", no_ack: "No answer from the machine", not_waiting: "No longer waiting",
      already_answered: "Already answered", unauthorized: "Admins only", publish_failed: "Couldn't publish to MQTT",
      not_loaded: "Crew Hub isn't loaded", unknown_machine: "Machine not known to Crew Hub", too_many: "Too many dismissed",
    };
    if (code === "invalid_args") return detail ? "Invalid: " + str(detail) : "Invalid input";
    return known[code] || str(detail || code || "Failed");
  }

  _buildMsg(x, key) {
    const fk = key + "|msg";
    const g = this._el("div", "grp");
    const ta = this._input("textarea", key + "|text", {rows: "1", maxlength: "4000", "aria-label": "Message to " + str(x.s.name || x.s.project || "session"), placeholder: "Message"});
    ta.dataset.grow = "1";
    requestAnimationFrame(() => this._grow(ta));
    const urgentKey = key + "|urgent";
    const send = this._btn("Send", null, fk, fk, async () => {
      const text = ta.value;
      if (!text.trim()) { this._setFb(fk, "Type a message first", "err"); return; }
      const urgent = !!this._drafts.get(urgentKey);
      if (await this._send(fk, x.mid, "send", {session: str(x.s.id), text, urgent})) {
        this._drafts.delete(key + "|text"); this._drafts.delete(urgentKey);
        for (const el of this._find("key", key + "|text")) { el.value = ""; this._grow(el); }
        for (const el of this._find("key", urgentKey)) el.checked = false;
      }
    });
    const line = this._el("div", "line");
    line.append(ta, send);
    const chk = this._el("label", "check");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.key = urgentKey; cb.checked = !!this._drafts.get(urgentKey);
    cb.addEventListener("change", () => this._drafts.set(urgentKey, cb.checked));
    chk.append(cb, this._el("span", null, "Urgent"));
    const opts = this._el("div", "btns");
    opts.append(chk, this._el("span", "hint", "interrupts the current reply"));
    g.append(line, opts, this._fbNode(fk));
    return g;
  }

  _buildAnswer(x, key, w) {
    const fk = key + "|ans", req = str(w.requestId), base = key + "|" + req;
    const g = this._el("div", "grp");
    const answer = args => this._send(fk, x.mid, "answer", Object.assign({session: str(x.s.id), requestId: req}, args));
    if (w.kind === "question" && arr(w.questions).length) {
      const qs = arr(w.questions).filter(q => q && typeof q === "object" && typeof q.question === "string");
      const direct = qs.length === 1 && !qs[0].multiSelect;
      qs.forEach((q, qi) => {
        const box = this._el("div", "q");
        box.append(this._el("div", null, q.question));
        const btns = this._el("div", "btns");
        const sk = base + "|q" + qi;
        const labels = arr(q.options).map(o => (o && typeof o === "object" ? o.label : o)).filter(l => typeof l === "string" && l);
        labels.forEach((lab, li) => {
          const b = this._btn(lab, "btn-o", sk + "|o" + li, fk, () => {
            if (direct) { answer({decision: "allow", answers: {[q.question]: lab}}); return; }
            let sel = arr(this._drafts.get(sk));
            sel = q.multiSelect ? (sel.includes(lab) ? sel.filter(v => v !== lab) : [...sel, lab]) : [lab];
            this._drafts.set(sk, sel);
            for (const [i, l] of labels.entries()) for (const el of this._find("key", sk + "|o" + i)) el.setAttribute("aria-pressed", String(sel.includes(l)));
          });
          if (!direct) b.setAttribute("aria-pressed", String(arr(this._drafts.get(sk)).includes(lab)));
          btns.append(b);
        });
        box.append(btns);
        g.append(box);
      });
      if (!direct) {
        g.append(this._btn("Answer", null, base + "|answer", fk, () => {
          const answers = {};
          for (const [qi, q] of qs.entries()) {
            const sel = arr(this._drafts.get(base + "|q" + qi));
            if (!sel.length) { this._setFb(fk, "Pick an answer for each question", "err"); return; }
            answers[q.question] = sel.join(", ");
          }
          answer({decision: "allow", answers});
        }));
      }
    } else {
      const note = this._input("input", base + "|note", {type: "text", maxlength: "500", placeholder: "Note (optional)", "aria-label": "Note with the answer"});
      const msg = () => { const v = note.value.trim(); return v ? {message: v} : {}; };
      const btns = this._el("div", "btns");
      btns.append(
        this._btn("Allow", null, base + "|allow", fk, () => answer(Object.assign({decision: "allow"}, msg()))),
        this._btn("Deny", "btn-o", base + "|deny", fk, () => answer(Object.assign({decision: "deny"}, msg()))));
      g.append(btns, note);
    }
    g.append(this._el("div", "hint", "or answer in claude.ai"), this._fbNode(fk));
    return g;
  }

  _buildBg(x, key, acts) {
    const fk = key + "|bg";
    const g = this._el("div", "grp");
    const btns = this._el("div", "btns");
    for (const act of acts) {
      const label = act === "stop" ? "Stop" : "Interrupt";
      btns.append(this._twoTap(label, `Confirm ${act}?`, "btn-o sm", key + "|" + act, fk,
        () => this._send(fk, x.mid, act, {session: str(x.s.id)})));
    }
    g.append(btns, this._fbNode(fk));
    return g;
  }

  /* ---- New session ---- */
  _startable() {
    const out = [];
    for (const [mid, snap] of Object.entries(this._snaps)) {
      if (!snap || typeof snap !== "object") continue;
      const m = snap.machine || {}, c = m.commands;
      if (!c || c.enabled !== true || snap.configured === false || str(snap.status || m.status).toLowerCase() === "offline") continue;
      out.push({
        mid, name: str(m.name || m.id || mid),
        folders: arr(c.folders).filter(f => f && typeof f.key === "string").map(f => ({key: f.key, label: str(f.label || f.key)})),
        models: arr(c.models).filter(v => typeof v === "string"),
        modes: arr(c.permissionModes).filter(v => typeof v === "string"),
      });
    }
    return out;
  }

  _syncNew() {
    const list = this._live() ? this._startable() : [];
    const sig = JSON.stringify(list);
    if (sig === this._newSig) return;
    this._newSig = sig;
    if (!list.length) { this._newSlot.replaceChildren(); return; }
    const fk = "new|start";
    const d = this._el("details", "diag");
    d.open = this._newOpen;
    d.addEventListener("toggle", () => { this._newOpen = d.open; });
    const sum = this._el("summary", "label", "New session");
    sum.dataset.key = "new|summary";
    const form = this._el("div", "form");
    const select = (key, label, options) => {
      const f = this._el("label", "field");
      const sel = this._el("select", "in");
      sel.dataset.key = key;
      for (const o of options) { const op = this._el("option", null, o.label); op.value = o.value; sel.append(op); }
      const want = this._drafts.get(key);
      sel.value = options.some(o => o.value === want) ? want : (options[0] ? options[0].value : "");
      this._drafts.set(key, sel.value);
      sel.addEventListener("change", () => { this._drafts.set(key, sel.value); if (key === "new|machine") { this._newSig = null; this._syncNew(); } });
      f.append(this._el("span", "label", label), sel);
      form.append(f);
      return sel;
    };
    const mSel = select("new|machine", "Machine", list.map(m => ({value: m.mid, label: m.name})));
    const mach = list.find(m => m.mid === mSel.value) || list[0];
    const fSel = select("new|folder", "Folder", mach.folders.map(f => ({value: f.key, label: f.label})));
    const modSel = select("new|model", "Model", mach.models.map(v => ({value: v, label: v.replace(/^claude-/, "")})));
    const pSel = select("new|mode", "Permission mode", mach.modes.map(v => ({value: v, label: v})));
    const field = (label, el) => { const f = this._el("label", "field wide"); f.append(this._el("span", "label", label), el); form.append(f); };
    const name = this._input("input", "new|name", {type: "text", maxlength: "60", autocomplete: "off"});
    const prompt = this._input("textarea", "new|prompt", {rows: "3", maxlength: "4000"});
    prompt.classList.add("tall");
    field("Name", name);
    field("Prompt", prompt);
    const start = this._twoTap("Start", "Confirm start?", null, "new|startbtn", fk, async () => {
      const args = {folder: fSel.value, model: modSel.value, permissionMode: pSel.value, name: name.value.trim(), prompt: prompt.value};
      if (await this._send(fk, mach.mid, "start", args)) {
        for (const k of ["new|name", "new|prompt"]) { this._drafts.delete(k); for (const el of this._find("key", k)) el.value = ""; }
      }
    }, () => (name.value.trim() && prompt.value.trim() && fSel.value && modSel.value && pSel.value ? null : "Name and prompt are needed"));
    const act = this._el("div", "btns wide");
    const fb = this._fbNode(fk);
    act.append(start, fb);
    form.append(act);
    d.append(sum, form);
    this._newSlot.replaceChildren(d);
  }

  _renderAgent(a, task) {
    const status = AGENT.includes(a.status) ? a.status : "done";
    const lab = status === "running" ? "working" : status;
    const c = this._el("div", "agent " + status);
    const rr = this._el("div", "row");
    rr.append(this._el("strong", "name", (task && task.title) || a.title || a.type || ("Agent " + str(a.id).slice(0, 7))), this._el("span", "chip c-" + lab, lab));
    c.append(rr);
    const mm = this._el("div", "meta");
    const tier = task && task.tier;
    if (tier) mm.append(this._el("span", null, tier));
    if (a.startedAt) {
      const sp = this._el("span", "elapsed");
      if (status === "running") this._tickText(sp, () => "running " + dur(a.startedAt));
      else sp.textContent = "took " + dur(a.startedAt, a.endedAt || a.current?.at || a.startedAt);
      mm.append(sp);
    }
    const steps = Number(a.steps) || 0;
    mm.append(this._el("span", null, steps + " step" + (steps === 1 ? "" : "s")));
    const tk = tokText(a.tokens, false);
    if (tk) mm.append(this._el("span", "tok", tk));
    if (status === "stopped" && a.stopReason) mm.append(this._el("span", null, str(a.stopReason)));
    c.append(mm);
    const cur = a.current && a.current.text ? a.current : null;
    if (status === "running" && cur) {
      const n = this._el("div", "now");
      n.append(this._el("i", "dot live"), this._el("span", null, cur.text));
      c.append(n);
    }
    const summary = a.summary || (task && task.summary);
    if (summary && status !== "running") c.append(this._el("div", "summary", summary));
    const tr = this._el("ul", "trail");
    arr(a.trail).filter(t => t && t.text && !(cur && status === "running" && t.text === cur.text && t.at === cur.at))
      .slice(-3).reverse().forEach(t => {
        const li = this._el("li");
        const tm = this._el("time", null, hm(t.at));
        if (t.at) tm.setAttribute("datetime", str(t.at));
        li.append(tm, this._el("span", null, t.text));
        tr.append(li);
      });
    if (tr.children.length) c.append(tr);
    return c;
  }
}

if (!customElements.get("crew-hub-card")) customElements.define("crew-hub-card", CrewHubCard);
window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === "crew-hub-card"))
  window.customCards.push({type: "crew-hub-card", name: "Crew Hub", description: "Live Claude Code sessions and agents"});
})();
