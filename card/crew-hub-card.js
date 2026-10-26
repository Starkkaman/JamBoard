/* Crew Hub card: renders machine snapshots (docs/DESIGN.md §2) in the Crew Board v4 look.
   No build step, no deps. Snapshot text is untrusted: DOM APIs + textContent only, never innerHTML with data.
   API: el.setSnapshots({<machineId>: snapshot}), setConfig(config), set hass(h), getCardSize().
   In Home Assistant the feed is the crew_hub integration's admin-only websocket subscription (crew_hub/subscribe):
   {"type":"full","machines":{id: {status, snapshot, receivedAt, configured}}} then {"type":"machine","id","data"} per change. */
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
.queue .tier{color:#5c5446;font-weight:400}

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
.agent .now{display:flex;gap:8px;align-items:baseline;min-width:0}
.agent .now span{min-width:0;overflow-wrap:anywhere}
.trail{margin:0;padding:0;list-style:none;display:grid;gap:2px;color:var(--dim);font-size:.85rem}
.trail li{display:flex;gap:8px;min-width:0}.trail li span{min-width:0;overflow-wrap:anywhere}
.trail time{font-variant-numeric:tabular-nums;flex:none}
.muted{color:var(--dim);font-size:.85rem}
.empty{color:var(--dim);background:var(--slab);padding:16px}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
`;

const SESS = {working: ["working", "working"], waiting: ["blocked", "needs you"], idle: ["idle", "idle"], ended: ["completed", "done"]};
const ORDER = {working: 0, waiting: 1, idle: 2, ended: 3};
const TASK = {pending: "queued", in_progress: "running", completed: "done"};

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
    this.shadowRoot.append(style, this._root);
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
    const n = Math.min(12, this._sessions().length);
    return 4 + n * 3;
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
      for (const s of arr(snap.sessions)) if (s && typeof s === "object") out.push({s, mname, offline, unconfigured});
    }
    return out;
  }

  /* ---- render ---- */
  _render() {
    this._tickers = [];
    const root = this._root;
    const parts = [this._renderHeader()];
    if (this._hubError) parts.push(this._el("div", "muted", "Crew Hub integration not available"));
    root.replaceChildren(...parts, this._renderFlow(), this._renderSessions());
  }

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

  _pickFlow() {
    const all = this._sessions();
    let best = null;
    for (const {s} of all) {
      const p = s.plan;
      if (p && typeof p === "object" && !p.finished && (!best || cmpDesc(p.updatedAt, best.plan.updatedAt) < 0)) best = {plan: p, s};
    }
    if (best) {
      const p = best.plan;
      const items = arr(p.tasks).slice().sort((a, b) => (a.order ?? 99) - (b.order ?? 99))
        .map(t => ({title: t.title || t.id, tier: t.tier, status: t.status || "queued"}));
      const total = p.total ?? items.length, done = p.done ?? items.filter(i => i.status === "done").length;
      return {title: p.title || "Untitled plan", phase: p.phase || "running", total, done, items, unit: "accepted"};
    }
    const withTasks = all.filter(x => arr(x.s.tasks).length).sort((a, b) => cmpDesc(a.s.updatedAt, b.s.updatedAt));
    if (withTasks.length) {
      const s = withTasks[0].s;
      const items = arr(s.tasks).map(t => ({title: t.subject || t.id, status: TASK[t.status] || "queued"}));
      return {title: `${s.name || s.project || "Session"}: tasks`, phase: (SESS[s.status] || SESS.idle)[1], total: items.length,
        done: items.filter(i => i.status === "done").length, items, unit: "done"};
    }
    return null;
  }

  _renderFlow() {
    const f = this._pickFlow();
    const sec = this._el("section", "flow");
    sec.setAttribute("aria-live", "polite");
    sec.append(this._el("span", "label", "Current plan"), this._el("h2", null, f ? f.title : "No plan running"));
    const bar = this._el("div", "bar"), i = this._el("i");
    const total = f ? Math.max(0, Number(f.total) || 0) : 0, done = f ? Math.min(Math.max(0, Number(f.done) || 0), total) : 0;
    i.style.width = total ? (100 * done / total) + "%" : "0";
    bar.append(i);
    const meta = this._el("div", "flowmeta");
    meta.append(this._el("span", "phase", f ? f.phase : "idle"),
      this._el("span", null, total ? `${done} of ${total} task${total === 1 ? "" : "s"} ${f.unit}` : ""));
    sec.append(bar, meta);
    const q = this._el("ul", "queue");
    for (const it of f ? f.items : []) {
      const st = it.status, lab = st === "running" ? "working" : st;
      const li = this._el("li", st === "done" ? "done" : "");
      const span = this._el("span", null, it.title);
      if (it.tier) span.append(" ", this._el("span", "tier", it.tier));
      li.append(this._el("b", "c-" + lab, lab), span);
      q.append(li);
    }
    sec.append(q);
    return sec;
  }

  _renderSessions() {
    const box = this._el("section", "sessions");
    box.setAttribute("aria-live", "polite");
    const list = this._sessions().sort((a, b) =>
      ((ORDER[a.s.status] ?? 2) - (ORDER[b.s.status] ?? 2)) || cmpDesc(a.s.updatedAt, b.s.updatedAt)).slice(0, 12);
    if (!list.length) {
      box.append(this._el("div", "empty", "Your Claude Code sessions appear here, working ones first, with every agent inside them and what each is doing right now."));
      return box;
    }
    for (const x of list) box.append(this._renderSession(x));
    return box;
  }

  _renderSession({s, mname, offline, unconfigured}) {
    const [cls, label] = SESS[s.status] || SESS.idle;
    const box = this._el("article", "sess " + cls);
    const r = this._el("div", "row");
    r.append(this._el("h3", "name", s.name || s.project || s.id || "Session"), this._el("span", "chip c-" + cls, label));
    box.append(r);
    if (s.summary) box.append(this._el("div", "summary", s.summary));
    if (s.status === "waiting" && s.waiting) {
      const w = this._el("div", "waiting");
      w.append(this._el("b", null, "Waiting: "));
      const parts = [s.waiting.tool, s.waiting.text].map(str).filter(Boolean);
      w.append(parts.join(" — ") || str(s.waiting.kind) || "input");
      box.append(w);
    }
    const m = this._el("div", "meta");
    m.append(this._el("span", null, mname));
    if (s.model) m.append(this._el("span", null, str(s.model).replace(/^claude-/, "")));
    if (typeof s.costUsd === "number" && isFinite(s.costUsd)) m.append(this._el("span", null, "$" + s.costUsd.toFixed(2)));
    if (s.updatedAt) { const sp = this._el("span"); this._tickText(sp, () => "active " + agoMin(s.updatedAt)); m.append(sp); }
    if (offline) m.append(this._el("span", null, "computer offline"));
    if (unconfigured) m.append(this._el("span", null, "unconfigured"));
    box.append(m);
    const cx = s.ctx;
    if (cx && Number(cx.max) > 0) {
      const pct = Math.max(0, Math.min(100, Math.round(100 * (Number(cx.used) || 0) / Number(cx.max))));
      const c = this._el("div", "ctx" + (pct > 85 ? " hot" : "")), i = this._el("i");
      i.style.width = pct + "%"; c.append(i);
      box.append(this._el("span", "label", `Context ${pct}%`), c);
    }
    const agents = arr(s.agents).filter(a => a && typeof a === "object");
    if (agents.length) {
      const tiers = new Map(arr(s.plan && s.plan.tasks).filter(t => t && t.agentId).map(t => [t.agentId, t]));
      const ag = this._el("div", "agents");
      agents.slice().sort((a, b) => ((a.status === "running" ? 0 : 1) - (b.status === "running" ? 0 : 1))
        || cmpDesc(a.current?.at || a.endedAt || a.startedAt, b.current?.at || b.endedAt || b.startedAt))
        .slice(0, 8).forEach(a => ag.append(this._renderAgent(a, tiers.get(a.id))));
      box.append(ag);
    }
    return box;
  }

  _renderAgent(a, task) {
    const status = ["running", "done", "failed"].includes(a.status) ? a.status : "done";
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
