/* Crew Hub card: renders machine snapshots (docs/DESIGN.md §2) in Callsheet's Jam look (design/callsheet-harmony).
   No build step, no deps, no outside requests. Snapshot text is untrusted: DOM APIs + textContent only, never innerHTML.
   API: el.setSnapshots({<machineId>: snapshot}), setConfig(config), set hass(h), getCardSize().
   In Home Assistant the feed is the crew_hub integration's admin-only websocket subscription (crew_hub/subscribe):
   {"type":"full","machines":{id: {status, snapshot, receivedAt, configured}}} then {"type":"machine","id","data"} per change.
   Commands (docs/COMMANDS.md): with a live hass connection and machine.commands.enabled, each session gets controls for
   the actions in session.controls, sent as hass.connection.sendMessagePromise({type:"crew_hub/command", machine, action, args}).
   crew_hub/dismiss {machine, session, agents?, session_dismissed?} for "Clear finished" and the idle rows' × (hidden at once).
   The card re-renders on every snapshot (~1/s): panels, rows and controls are keyed and kept, controls are rebuilt only
   when their inputs change, and drafts/focus/caret/open state live in Maps on the card.
   0.5.1 no layout jumps: plans, working sessions and agents keep the order in which they first appeared; agent rows and
   plan rows have a fixed line count (ellipsis, full text in title); finished agents stay in place (dimmed) until
   crew-hubd prunes them or Clear finished dismisses them; when content above the viewport changes height the card scrolls
   its scroll container (HA's view or the page) by the difference.
   0.6.0 Jam look: tokens verbatim from Callsheet (see the CSS), fonts served next to this file (fonts/, injected once into
   document.head because Chrome ignores @font-face in a shadow root). Day/Night follows HA: the host's data-theme comes
   from hass.themes.darkMode; without hass it follows prefers-color-scheme. Waiting sessions come first (oldest prompt
   first), then working ones in first-seen order; the oldest prompt the hub can answer gets the one orange key (.go). */
(() => {
// This script's own URL, for the fonts next to it: classic <script> (currentScript), module (stack trace, since
// import.meta can't appear in a file that also loads as a classic script), or a script tag / the integration's path.
const SCRIPT_URL = (() => {
  try { const cs = document.currentScript; if (cs && cs.src) return cs.src; } catch (e) { /* no currentScript */ }
  try {
    for (const line of String(new Error().stack || "").split("\n")) {
      const m = /([a-z][a-z0-9+.-]*:\/\/[^\s()]+?)(?::\d+){1,2}\)?\s*$/i.exec(line.trim());
      if (m) return m[1];
    }
  } catch (e) { /* no stack */ }
  const s = [...document.querySelectorAll("script[src]")].find(e => /crew-hub-card\.js/.test(e.src));
  return s ? s.src : new URL("/crew_hub/crew-hub-card.js", location.href).href;
})();
const fontUrl = f => new URL("fonts/" + f, SCRIPT_URL).href.replace(/["\\\n]/g, encodeURIComponent);
// Callsheet d27188c app.css lines 9 and 27, with the URLs resolved against this script.
function loadFonts() {
  if (document.head.querySelector("style[data-crew-hub-fonts]")) return;
  const st = document.createElement("style");
  st.dataset.crewHubFonts = "1";
  st.textContent = `@font-face{font-family:"Jost";src:url("${fontUrl("jost.ttf")}") format("truetype");font-weight:100 900;font-display:swap}
@font-face{font-family:"IBM Plex Mono";src:url("${fontUrl("ibm-plex-mono-400.woff2")}") format("woff2");font-weight:400;unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD;font-display:swap}`;
  document.head.append(st);
}

const CSS = `
/* ===== TOKENS. Verbatim from Callsheet d27188c app/static/app.css lines 593-668 (Jam), 2022-2023 (motion scale),
   2037 (reduced motion), 2608-2610 (panel base). Only three mechanical rewrites for the shadow root:
   ":root[data-theme=dark]" -> ":host([data-theme=dark])", ":root:not([data-theme=light])" -> ":host(:not([data-theme=light]))",
   "@media (max-width:700px)" -> "@container crew (max-width:700px)". ===== */
/* ================= 0.11.0: JAM (the default; jam-design HANDOFF.md "calm instruments", mockups/pass6/callsheet.html) =================
   Day and Night are pass 6's :root values: ground --bg, panel --surface, press --surface-2, hair --rule, ink, dim --muted,
   orange --accent (the one thing to do now). Text-bearing reds use the deeper alert ink (--alert); the red alert key is
   --alert-key with --on-alert-key. Extra tokens for the panel structure: --ink2 --inset --bubble --kline --walnut
   --alert-key --on-alert-key --warn --warn-ink --warn-line. Area colours are calm marks (blue, teal, violet, walnut), never
   orange or a state colour. Structure: data-base=panel (the end of this file). */
.app[data-look=jam]{
  color-scheme:light;
  --font-display:"Jost","Helvetica Neue",Arial,sans-serif;
  --font-body:"Jost","Helvetica Neue",Arial,sans-serif;
  --display-case:none;--display-weight:500;--display-lh:1.12;--display-track:-.01em;
  --label-case:uppercase;--label-track:.14em;--label-weight:400;
  --radius:10px;--tag-radius:999px;--chip-radius:10px;--phone-radius:0px;
  --rule-w:1px;--rule-mid:1px;--rule-heavy:1px;--frame-w:1px;--gap-tight:8px;
  --base-size:16px;--lh:1.45;--focus-w:2px;
  --cut:none;--cut-bar:none;--cut-torn:none;
  --fs-xs:13px;--fs-sm:14px;--fs-md:15px;--fs-lg:17px;--fs-time:17px;
  --fs-h3:19px;--fs-h2:26px;--fs-h1:40px;--fs-stat:30px;--fs-hero:30px;--bezel-w:1px;
  --font-label:"IBM Plex Mono",ui-monospace,monospace;--font-mono:"IBM Plex Mono",ui-monospace,monospace;
  --font-stat:"IBM Plex Mono",ui-monospace,monospace;--stat-weight:400;--btn-radius:10px;--ease:cubic-bezier(.16,1,.3,1);
  /* colour: Day */
  --bg:#ece7dd;--surface:#f7f4ee;--surface-2:#e8e2d5;--text:#19202e;--muted:#5b606d;--rule:#e0d9cb;
  --accent:#ec7c22;--on-accent:#1a1206;--alert:#b3261b;--ok:#3f8a1f;
  --area-1:#4a6396;--on-area-1:#f7f4ee;--area-2:#2c6d7d;--on-area-2:#f7f4ee;
  --area-3:#6f52a3;--on-area-3:#f7f4ee;--area-4:#6b4428;--on-area-4:#f7f4ee;
  --sel:#19202e;--on-sel:#f7f4ee;
  --masthead:#ece7dd;--on-masthead:#19202e;--eyebrow:#5b606d;
  --box:#f7f4ee;--on-box:#19202e;--callbox:#f7f4ee;--on-callbox:#19202e;
  --panel:#f7f4ee;--on-panel:#19202e;--panel-head:transparent;--on-panel-head:#19202e;--panel-mark:#6b4428;
  --track:#e0d9cb;--fill:#19202e;--field:#efeae0;--field-line:#8a8477;
  --phone:#ece7dd;--bezel:#e0d9cb;
  --tabbar:#ece7dd;--on-tabbar:#5b606d;--tab-on:#ece7dd;--on-tab-on:#19202e;
  --focus:#19202e;
  --ink2:#343b4a;--inset:#efeae0;--bubble:#e6dfd0;--kline:#8a8477;--walnut:#6b4428;
  --alert-key:#ff5a48;--on-alert-key:#1c0804;--warn:#f2c14e;--warn-ink:#7a5300;--warn-line:#a87a12;
}
.app[data-look=jam][data-mode=dark],
:host([data-theme=dark]) .app[data-look=jam][data-mode=auto]{
  color-scheme:dark;
  --bg:#10141d;--surface:#161c28;--surface-2:#202838;--text:#ece5d3;--muted:#9f9a8b;--rule:#232b3a;
  --accent:#f08a2c;--on-accent:#1a1206;--alert:#ff8a7a;--ok:#7bd651;
  --area-1:#8098c8;--on-area-1:#10141d;--area-2:#5ea3b3;--on-area-2:#10141d;
  --area-3:#a58bd3;--on-area-3:#10141d;--area-4:#c09a74;--on-area-4:#10141d;
  --sel:#ece5d3;--on-sel:#161c28;
  --masthead:#10141d;--on-masthead:#ece5d3;--eyebrow:#9f9a8b;
  --box:#161c28;--on-box:#ece5d3;--callbox:#161c28;--on-callbox:#ece5d3;
  --panel:#161c28;--on-panel:#ece5d3;--panel-head:transparent;--on-panel-head:#ece5d3;--panel-mark:#c09a74;
  --track:#232b3a;--fill:#ece5d3;--field:#10141d;--field-line:#636e87;
  --phone:#10141d;--bezel:#232b3a;
  --tabbar:#10141d;--on-tabbar:#9f9a8b;--tab-on:#10141d;--on-tab-on:#ece5d3;
  --focus:#ece5d3;
  --ink2:#d2cbb9;--inset:#1b2230;--bubble:#232c3e;--kline:#636e87;--walnut:#6b4428;
  --alert-key:#ff5a48;--on-alert-key:#1c0804;--warn:#f2c14e;--warn-ink:#f2c14e;--warn-line:#f2c14e;
}
@media (prefers-color-scheme: dark){
:host(:not([data-theme=light])) .app[data-look=jam][data-mode=auto]{
  color-scheme:dark;
  --bg:#10141d;--surface:#161c28;--surface-2:#202838;--text:#ece5d3;--muted:#9f9a8b;--rule:#232b3a;
  --accent:#f08a2c;--on-accent:#1a1206;--alert:#ff8a7a;--ok:#7bd651;
  --area-1:#8098c8;--on-area-1:#10141d;--area-2:#5ea3b3;--on-area-2:#10141d;
  --area-3:#a58bd3;--on-area-3:#10141d;--area-4:#c09a74;--on-area-4:#10141d;
  --sel:#ece5d3;--on-sel:#161c28;
  --masthead:#10141d;--on-masthead:#ece5d3;--eyebrow:#9f9a8b;
  --box:#161c28;--on-box:#ece5d3;--callbox:#161c28;--on-callbox:#ece5d3;
  --panel:#161c28;--on-panel:#ece5d3;--panel-head:transparent;--on-panel-head:#ece5d3;--panel-mark:#c09a74;
  --track:#232b3a;--fill:#ece5d3;--field:#10141d;--field-line:#636e87;
  --phone:#10141d;--bezel:#232b3a;
  --tabbar:#10141d;--on-tabbar:#9f9a8b;--tab-on:#10141d;--on-tab-on:#ece5d3;
  --focus:#ece5d3;
  --ink2:#d2cbb9;--inset:#1b2230;--bubble:#232c3e;--kline:#636e87;--walnut:#6b4428;
  --alert-key:#ff5a48;--on-alert-key:#1c0804;--warn:#f2c14e;--warn-ink:#f2c14e;--warn-line:#f2c14e;
}
}
@container crew (max-width:700px){
.app[data-look=jam]{--fs-h1:30px;--fs-stat:26px;--fs-h2:24px}
}
.app{--dur-1:150ms;--dur-2:250ms;--dur-3:350ms;background:var(--scene,var(--bg));letter-spacing:var(--body-track,normal);
  font-stretch:var(--body-stretch,normal);font-feature-settings:var(--font-features,normal)}
@media (prefers-reduced-motion:reduce){.app *,.app *::before,.app *::after{transition-duration:0s!important;animation-duration:0s!important;animation-delay:0s!important}}
.app[data-look=jam]{--dur-1:.15s;--dur-2:.25s}
.app[data-base=panel]{--pgap:20px;--ppad:22px 26px 24px;--prad:16px;--key-h:38px}
@container crew (max-width:700px){.app[data-base=panel]{--pgap:12px;--ppad:18px 18px 20px;--prad:14px;--key-h:44px}}
/* ===== Crew Hub components. Tokens only, no raw colours. ===== */
:host{display:block}
*,*::before,*::after{box-sizing:border-box}
.cq{container:crew / inline-size}
.app{font-family:var(--font-body);font-size:var(--fs-md);line-height:var(--lh);color:var(--text);-webkit-font-smoothing:antialiased}
:host([data-fill]) .app{min-height:100vh}
@media (pointer:coarse){.app[data-base=panel]{--key-h:44px}}
button,input,select,textarea{font:inherit;color:inherit;margin:0}
button{cursor:pointer}
:focus-visible{outline:var(--focus-w) solid var(--focus);outline-offset:2px}
[hidden]{display:none!important}
p,h2,h3,ul,ol,dl{margin:0}
.wrap{max-width:1344px;margin:0 auto;padding:20px 28px 40px}
@container crew (max-width:700px){.wrap{padding:12px 16px 32px}}

/* masthead + headline */
.mast{display:flex;align-items:center;gap:8px;margin:0 0 8px}
.bt{flex:1;min-width:0;display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 12px}
.brand{font:var(--display-weight) var(--fs-h3)/1.1 var(--font-display);letter-spacing:var(--display-track)}
.fresh{font:400 var(--fs-xs)/1.3 var(--font-mono);color:var(--muted)}
.fresh.stale{color:var(--warn-ink)}
.sp{flex:1}
.hb{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:var(--key-h);min-width:var(--key-h);padding:0 12px;
  border:0;border-radius:var(--radius);background:none;box-shadow:inset 0 0 0 1px var(--rule);font:500 var(--fs-md)/1 var(--font-body)}
.hb:hover{background:var(--inset)}
.hb[aria-expanded=true]{background:var(--inset);box-shadow:inset 0 0 0 1.5px var(--text)}
.hb svg{width:14px;height:14px;stroke:currentColor;stroke-width:1.8;fill:none}
.head{font:var(--display-weight) var(--fs-h2)/var(--display-lh) var(--font-display);letter-spacing:var(--display-track);margin:4px 0 20px;text-wrap:balance}
.jump{display:inline-block;font:inherit;letter-spacing:inherit;color:inherit;background:none;border:0;padding:9px 0;margin:-9px 0;
  text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:5px}

/* layout: phone = one column (new, plans, sessions, idle); wide = sessions left, the rest right */
.grid{display:flex;flex-direction:column;gap:var(--pgap)}
.side{display:contents}
.sessions{display:flex;flex-direction:column;gap:var(--pgap);order:2;min-width:0}
.newp,.help{order:0}.plans{order:1}.idle{order:3}
@container crew (min-width:960px){
  .grid{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(320px,1fr);align-items:start}
  .side{display:flex;flex-direction:column;gap:var(--pgap);min-width:0;grid-column:2;grid-row:1}
  .sessions{grid-column:1;grid-row:1}
}

/* panel structure (Callsheet data-base=panel) */
.panel{background:var(--surface);border:1px solid var(--rule);border-radius:var(--prad);padding:var(--ppad);min-width:0;scroll-margin-top:12px;
  transition:outline-color var(--dur-3) var(--ease)}
.panel.flash,.irow.flash{outline:var(--focus-w) solid var(--focus);outline-offset:3px}
.lab{font:var(--label-weight) var(--fs-xs)/1.3 var(--font-label);letter-spacing:var(--label-track);text-transform:var(--label-case);color:var(--muted)}
.mono{font:400 var(--fs-xs)/1.4 var(--font-mono);color:var(--muted)}
.ph{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 10px;min-height:24px}
.chev{flex:none;width:8px;height:8px;margin:0 4px 3px 4px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;
  transform:rotate(45deg);transition:transform var(--dur-1) var(--ease)}
[aria-expanded=true]>.chev{transform:translateY(3px) rotate(225deg)}

/* keys (pass 6 / Callsheet panel keys) */
.k{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:var(--key-h);min-width:var(--key-h);padding:0 16px;border:0;
  border-radius:var(--radius);background:var(--surface);color:var(--text);box-shadow:inset 0 0 0 1px var(--kline);
  font:500 var(--fs-md)/1 var(--font-body);transition:background-color var(--dur-1) var(--ease),color var(--dur-1) var(--ease)}
.k:hover{background:var(--surface-2)}
.k.go{background:var(--accent);color:var(--on-accent);box-shadow:none}
.k.dark{background:var(--text);color:var(--surface);box-shadow:none}
.k.on{background:var(--inset);box-shadow:inset 0 0 0 1.5px var(--text)}
.k.q{background:none;box-shadow:none;padding:0 8px;color:var(--ink2);font-weight:400;text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:4px}
.k.q:hover{color:var(--text)}
.k:disabled{opacity:.45;cursor:default}
.keys{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.fb{font:400 var(--fs-xs)/1.4 var(--font-mono);color:var(--ink2)}
.fb.err{color:var(--alert)}

/* plans: signature 1, the segmented track (one segment per task) */
.pl{list-style:none;padding:0;display:grid;gap:2px}
.plan{display:grid;gap:7px;width:calc(100% + 20px);margin:0 -10px;padding:9px 10px;text-align:left;background:none;border:0;border-radius:10px}
.plan:hover{background:var(--inset)}
.pt{display:flex;align-items:baseline;justify-content:space-between;gap:12px;min-width:0}
.pn{font:500 var(--fs-md)/1.3 var(--font-body);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.pc{flex:none;font:400 var(--fs-xs)/1 var(--font-mono);color:var(--ink2)}
.plan.fin .pn{color:var(--ink2);font-weight:400}
.pm{font:400 var(--fs-xs)/1.2 var(--font-mono);color:var(--muted)}
.pm .al{color:var(--alert)}
.seg{display:flex;gap:3px;height:8px}
.seg i{flex:1;border-radius:2px;box-shadow:inset 0 0 0 1px var(--kline)}
.seg i.done{background:var(--fill);box-shadow:none}
.seg i.running{background:var(--ok);box-shadow:none}
.seg i.failed{background:var(--alert);box-shadow:none}
.seg.bar{background:var(--track);border-radius:2px}.seg.bar i{flex:none;box-shadow:none}

/* session panel */
.sess{display:grid;gap:14px}
.sess.cue-you{box-shadow:inset 3px 0 0 var(--text)}
.sess.cue-al{box-shadow:inset 3px 0 0 var(--alert)}
.sh{display:flex;align-items:baseline;justify-content:space-between;gap:6px 12px;flex-wrap:wrap}
.sn{font:var(--display-weight) var(--fs-h3)/1.2 var(--font-display);letter-spacing:var(--display-track);min-width:0;overflow-wrap:anywhere}
.st{flex:none;font:400 var(--fs-xs)/1 var(--font-mono);color:var(--muted)}
.st.you{background:var(--text);color:var(--surface);padding:6px 10px;border-radius:999px;letter-spacing:.04em}
.st.off{color:var(--alert)}
.st.off::before,.im .off::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--alert);margin-right:6px}
.sum{color:var(--ink2);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.metaw{display:grid;gap:6px}
.meta{display:flex;flex-wrap:wrap;justify-content:space-between;gap:2px 12px;font:400 var(--fs-xs)/1.4 var(--font-mono);color:var(--muted)}
.meta .warn{color:var(--warn-ink)}
.ctx{height:4px;border-radius:2px;background:var(--track);overflow:hidden}
.ctx i{display:block;height:100%;background:var(--fill)}
.ctx.warn i{background:var(--warn-ink)}

/* the ask (waiting prompt) */
.ask{background:var(--inset);border-radius:12px;padding:14px 16px;display:grid;gap:10px}
.at{display:flex;align-items:baseline;gap:4px 12px;flex-wrap:wrap}
.aq{font:400 var(--fs-h3)/1.3 var(--font-body)}
.note{width:100%}

/* agents: signature 2, the thread (a hairline spine; nodes carry state) */
.thread{position:relative;display:grid;gap:12px;padding-left:24px}
.thread::before{content:"";position:absolute;left:5px;top:10px;bottom:10px;width:1px;background:var(--kline)}
.ag{position:relative;min-width:0}
.node{position:absolute;left:-24px;top:6px;width:11px;height:11px;border-radius:50%;background:var(--surface);box-shadow:inset 0 0 0 1.5px var(--kline)}
.ag.run .node{background:var(--ok);box-shadow:0 0 0 3px var(--surface)}
.ag.done .node{background:var(--muted);box-shadow:0 0 0 3px var(--surface);transform:scale(.7)}
.ag.failed .node{background:var(--alert);border-radius:2px;box-shadow:0 0 0 3px var(--surface);transform:rotate(45deg) scale(.8)}
.agb{display:grid;gap:2px;width:100%;padding:0;background:none;border:0;text-align:left;border-radius:6px}
.agt{display:flex;justify-content:space-between;align-items:baseline;gap:12px;min-width:0}
.agn{font:500 var(--fs-md)/1.3 var(--font-body);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.age{flex:none;font:400 var(--fs-xs)/1 var(--font-mono);color:var(--ink2)}
.now{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:opacity var(--dur-2) var(--ease)}
.agm{font:400 var(--fs-xs)/1.4 var(--font-mono);color:var(--muted)}
.trail{list-style:none;padding:8px 0 0;display:grid;gap:4px}
.trail li{display:flex;gap:10px;color:var(--ink2);min-width:0}
.trail .tm{flex:none;font:400 var(--fs-xs)/1.6 var(--font-mono);color:var(--muted)}
.agl{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.agl .tw{font:400 var(--fs-xs)/1 var(--font-mono);margin-right:8px}
.ag.failed .agl .tw{color:var(--alert)}
.tf{display:flex;flex-wrap:wrap;gap:0 4px;margin:4px 0 -8px 16px}

/* queue + actions + drawers */
.next{display:flex;align-items:center;gap:10px;width:100%;min-height:var(--key-h);padding:0;background:none;border:0;border-top:1px solid var(--rule);text-align:left}
.next .nt{min-width:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tasks{list-style:none;padding:4px 0 0;display:grid;gap:6px}
.tasks li{display:grid;grid-template-columns:76px minmax(0,1fr);gap:10px;align-items:baseline}
.tasks .tw{font:400 var(--fs-xs)/1 var(--font-mono);color:var(--muted)}
.tasks li.running .tw{color:var(--text)}
.tasks li.failed .tw{color:var(--alert)}
.tasks li.done .tt{color:var(--muted)}
.tier{font:400 var(--fs-xs)/1 var(--font-mono);color:var(--muted)}
.acts{display:flex;flex-wrap:wrap;gap:4px;margin:-6px 0 -8px -8px}
.actw{display:grid;gap:12px}
.comp,.more{display:grid;gap:10px}
.more{background:var(--inset);border-radius:12px;padding:14px 16px}
.crow{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.kv{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 16px;font:400 var(--fs-xs)/1.4 var(--font-mono)}
.kv dt{color:var(--muted)}.kv dd{margin:0;color:var(--ink2);overflow-wrap:anywhere}

/* fields + flat tick box */
input[type=text],select,textarea{width:100%;min-height:var(--key-h);padding:0 12px;background:var(--field);color:var(--text);
  border:1px solid var(--field-line);border-radius:var(--radius);font:400 var(--fs-md)/1.3 var(--font-body)}
textarea{padding:9px 12px;resize:vertical;display:block}
.tick{display:inline-flex;align-items:center;gap:10px;min-height:var(--key-h);cursor:pointer}
.tick input{appearance:none;-webkit-appearance:none;flex:none;width:20px;height:20px;border-radius:5px;background:var(--field);border:1.5px solid var(--field-line)}
.tick input:checked{background:var(--text);border-color:var(--text);box-shadow:inset 0 0 0 3px var(--field)}
.form{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}
.f{display:grid;gap:6px;min-width:0}.f.wide{grid-column:1/-1}

/* idle */
.fold{display:flex;align-items:center;gap:10px;width:100%;min-height:var(--key-h);padding:0;background:none;border:0;text-align:left}
.fold .cnt{font:400 var(--fs-xs)/1 var(--font-mono);color:var(--ink2)}
.fold .chev{margin-left:auto}
.irows{list-style:none;padding:6px 0 0}
.irow{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;border-top:1px solid var(--rule);border-radius:2px}
.ib{display:grid;gap:2px;min-width:0;min-height:var(--key-h);padding:10px 0;background:none;border:0;text-align:left}
.in{font:500 var(--fs-md)/1.3 var(--font-body)}
.im{font:400 var(--fs-xs)/1.4 var(--font-mono);color:var(--muted)}
.im .off{color:var(--alert)}
.is{color:var(--ink2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.irow.open .is{white-space:normal}
.x{width:var(--key-h);height:var(--key-h);margin:4px -10px 0 0;border:0;border-radius:var(--radius);background:none;color:var(--muted);font:400 22px/1 var(--font-body)}
.x:hover{background:var(--inset);color:var(--text)}
.idet{grid-column:1/-1;padding:0 0 14px}
.gone{grid-column:1/-1;display:flex;align-items:center;gap:8px;min-height:var(--key-h)}
.help p{color:var(--ink2);margin:0 0 6px}
/* ===== Real-card additions (0.6.0). Tokens only, no raw colours. ===== */
.notice{margin:0 0 12px;color:var(--alert)}
.plus{position:relative;flex:none;width:12px;height:12px}
.plus::before,.plus::after{content:"";position:absolute;background:currentColor;border-radius:1px}
.plus::before{left:5px;top:0;width:2px;height:12px}.plus::after{top:5px;left:0;width:12px;height:2px}
.empty{color:var(--ink2)}
.sh{flex-wrap:nowrap}.sn{flex:1}
.stw{flex:none;min-width:96px;display:flex;justify-content:flex-end}
.sum{min-height:calc(2em * var(--lh))}
.meta{flex-wrap:nowrap}
.meta .ml{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.meta .mc{flex:none}
.plan.none{cursor:default}.plan.none:hover{background:none}
.pm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:1.2em}
.agb[role=button]{cursor:pointer}
.agb .now{min-height:calc(1em * var(--lh))}
.agm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:1.4em}
.sess{grid-template-columns:minmax(0,1fr)}.sess>*{min-width:0}
.ag.held{opacity:.6}.ag.held .now{color:var(--muted)}
.ag.stopped .node{box-shadow:inset 0 0 0 1.5px var(--muted),0 0 0 3px var(--surface)}
.trail li{min-height:1.6em}.trail li>span:last-child{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.agl .why{color:var(--muted)}
.fin{display:grid;gap:10px}.fin:empty{display:none}
.tf{margin:0 0 -8px 16px}
.fb:empty{display:none}
.irow>.fb{grid-column:1/-1;padding:0 0 10px}
.k.on{font-weight:500}
`;

const ACTIVE = new Set(["working", "waiting"]);  // panels; everything else goes into the Idle section
const AGENT = ["running", "done", "failed", "stopped"];
const PLAN_TASK = ["queued", "running", "done", "failed"];
const WORD = {queued: "Queued", running: "Running", done: "Done", failed: "Failed", stopped: "Stopped"};
const TASK = {pending: "queued", in_progress: "running", completed: "done"};
const PERM = {default: "Ask first", acceptEdits: "Accept edits", auto: "Auto", plan: "Plan only"};
const FINISHED_PLAN_MS = 10 * 60 * 1000;  // finished plans stay in Plans this long
const STALE_MS = 90 * 1000;                // "▲ Updated …" after this long without a report
const MAX_PLANS = 4, MAX_ACTIVE = 12, MAX_IDLE = 40, MAX_FINISHED = 20, MAX_SEG = 24;
const MAX_AGENT_ROWS = 12;  // agent rows in a session (running + finished held in place; crew-hubd keeps 12 agents)
const TRAIL_LINES = 3;

// Times arrive as epoch ms (crew-hubd snapshots) or ISO strings (fixtures).
const ms = v => { const t = typeof v === "number" ? v : Date.parse(v); return Number.isFinite(t) ? t : null; };
// "12 s", "3 min", "2 h", "3 d"
function ago(d) {
  const s = Math.max(0, Math.floor(d / 1000)); if (s < 60) return s + " s";
  const m = Math.floor(s / 60); if (m < 60) return m + " min";
  const h = Math.floor(m / 60); return h < 48 ? h + " h" : Math.floor(h / 24) + " d";
}
const since = v => { const t = ms(v); return t == null ? "" : ago(Date.now() - t); };
function hm(v) { const t = ms(v); if (t == null) return ""; try { return new Date(t).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false}); } catch { return ""; } }
const nowHm = () => hm(Date.now());
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
// "312k tok"; "" without token data.
function tokText(t) {
  if (!t || typeof t !== "object") return "";
  const sum = ["in", "out", "cacheRead", "cacheWrite"].reduce((a, k) => a + (Number(t[k]) || 0), 0);
  return sum > 0 ? fmtTok(sum) + " tok" : "";
}
// "claude-opus-5-5" -> "Opus 5.5"
function modelName(m) {
  const r = /^claude-([a-z]+)-(\d+)(?:-(\d+))?/.exec(str(m));
  return r ? r[1].charAt(0).toUpperCase() + r[1].slice(1) + " " + r[2] + (r[3] ? "." + r[3] : "") : str(m).replace(/^claude-/, "");
}
const tierOf = type => { const r = /-(light|medium|heavy|careful)$/.exec(str(type)); return r ? r[1] : ""; };

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
    // UI state that must survive re-renders.
    this._arts = new Map();    // "mid|sid" -> session panel parts
    this._rows = new Map();    // "mid|sid" -> idle row parts
    this._drafts = new Map();  // data-key -> typed value / checkbox state / selected labels
    this._fb = new Map();      // feedback key -> {text, cls, timer}
    this._confirm = new Map(); // two-tap key -> timer
    this._busy = new Set();    // feedback keys with a command in flight
    this._open = new Set();    // open toggles: "plans", "row|k", "q|k", "fin|k", "msg|k", "more|k", "tr|k|agent", "note|k|req"
    this._hidden = new Set();  // idle sessions hidden with × (crew_hub/dismiss in flight or done)
    this._newOpen = false; this._helpOpen = false; this._idleOpen = false;
    this._newSig = null; this._headSig = null;
    // 0.5.1 stable order: first-seen sequence numbers (sessions per group, plans); new items get the next number.
    this._n = 0;
    this._seq = new Map();     // session key -> {grp: "w"|"a"|"i", n}
    this._planSeq = new Map(); // plan key -> n
    this._strips = new Map();  // plan key -> plan row parts (patched in place)
    this._noAnchorUntil = 0;   // no scroll compensation while a jump's smooth scroll runs
    this._onScreenRows = new Set();
    this._moved = new Set();   // panels that changed group in this render: not used as scroll anchors
    this._goKey = null;        // session whose prompt gets the orange key
    this._firstWait = null;    // session the headline's "N need you" jumps to
    this._ids = 0;

    const E = (t, c, x) => this._el(t, c, x);
    const cq = E("div", "cq"), app = E("div", "app");
    app.dataset.look = "jam"; app.dataset.base = "panel"; app.dataset.mode = "auto";
    this._root = E("div", "wrap");
    // masthead
    this._fresh = E("span", "fresh");
    const bt = E("div", "bt");
    bt.append(E("h2", "brand", "Crew Hub"), this._fresh);
    this._newBtn = this._btn("", "hb", "hdr|new", null, () => {
      this._newOpen = !this._newOpen; this._render();
      if (this._newOpen) { const f = this._find("key", "new|name")[0]; if (f) f.focus(); }
    });
    this._newBtn.append(this._plus(), document.createTextNode("New"));
    this._helpBtn = this._btn("?", "hb", "hdr|help", null, () => { this._helpOpen = !this._helpOpen; this._render(); });
    this._helpBtn.setAttribute("aria-label", "Help");
    const mast = E("header", "mast");
    mast.append(bt, this._newBtn, this._helpBtn);
    this._head = E("p", "head");
    this._note = E("p", "mono notice", "Crew Hub integration not available");
    // grid: sessions left, the rest right (one column on a phone: New, Plans, sessions, Idle)
    const grid = E("div", "grid"), side = E("div", "side");
    this._sessBox = E("div", "sessions");
    this._sessBox.setAttribute("aria-live", "polite");
    this._emptyEl = E("div", "panel empty");
    this._help = E("section", "panel help");
    const hh = E("div", "ph"); hh.append(E("h3", "lab", "Help"));
    this._help.append(hh,
      E("p", null, "Needs you: a session waits for your answer. Answer here or in claude.ai."),
      E("p", null, "Urgent messages stop the current reply first."),
      E("p", null, "Interrupt and Stop work on background sessions. Both ask for a second tap."),
      E("p", null, "▲ Updated means no report from your machines for a while."));
    this._newP = E("section", "panel newp");
    this._plansP = E("section", "panel plans");
    this._plansP.setAttribute("aria-label", "Plans");
    const ph = E("div", "ph");
    this._moreBtn = this._btn("", "k q", "plans|more", null, () => this._toggle("plans"));
    ph.append(E("h3", "lab", "Plans"), this._moreBtn);
    this._pl = E("ul", "pl");
    this._noPlan = E("li");
    const np = E("div", "plan none");
    np.append(E("span", "pm", "No plan running"));
    this._noPlan.append(np);
    this._plansP.append(ph, this._pl);
    this._idleP = E("section", "panel idle");
    this._fold = this._btn("", "fold", "idle|fold", null, () => { this._idleOpen = !this._idleOpen; this._render(); });
    this._idleCnt = E("span", "cnt");
    this._fold.append(E("span", "lab", "Idle"), this._idleCnt, this._chev());
    this._irows = E("ul", "irows");
    this._idleP.append(this._fold, this._irows);
    side.append(this._help, this._newP, this._plansP, this._idleP);
    grid.append(this._sessBox, side);
    this._root.append(mast, this._head, this._note, grid);
    app.append(this._root);
    cq.append(app);
    this.shadowRoot.append(style, cq);
  }

  connectedCallback() {
    loadFonts();
    this._theme();
    this._render();
    if (!this._timer) this._timer = setInterval(() => this._tick(), 1000);
    if (this._hass && !this._subToken) this._subscribe();
  }
  disconnectedCallback() { clearInterval(this._timer); this._timer = null; this._unsubscribe(); }

  /* ---- public API ---- */
  setSnapshots(obj) { this._snaps = obj && typeof obj === "object" ? obj : {}; this._render(); }
  setConfig(config) { this._config = Object.assign({}, config || {}); if (this.isConnected) this._render(); }
  set hass(h) {
    const prev = this._hass;
    this._hass = h;
    this._theme();
    if (!h || !h.connection) return;
    if (prev && prev.connection !== h.connection) this._unsubscribe();
    // hass is set on every state change: subscribe once per connection, and don't retry a refused one here.
    if (this.isConnected && !this._subToken && !(this._hubError && this._errConn === h.connection)) this._subscribe();
  }
  get hass() { return this._hass; }
  getCardSize() {
    const n = Math.min(MAX_ACTIVE, this._sessions().filter(x => ACTIVE.has(x.s.status)).length);
    return 6 + n * 3;
  }

  // Day/Night follows Home Assistant (hass.themes.darkMode); without it the device decides (prefers-color-scheme).
  _theme() {
    const t = this._hass && this._hass.themes;
    const want = t && typeof t.darkMode === "boolean" ? (t.darkMode ? "dark" : "light") : null;
    if (want) { if (this.getAttribute("data-theme") !== want) this.setAttribute("data-theme", want); }
    else if (this.hasAttribute("data-theme")) this.removeAttribute("data-theme");
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
  _chev() { const c = this._el("span", "chev"); c.setAttribute("aria-hidden", "true"); return c; }
  _plus() { const c = this._el("span", "plus"); c.setAttribute("aria-hidden", "true"); return c; }
  _tickText(node, fn) { const f = () => this._txt(node, fn()); f(); this._tickers.push(f); return node; }
  _tick() { const an = this._anchor(); for (const f of this._tickers) f(); this._reanchor(an); }
  // Patch text only when it changed (a rewrite with the same text still replaces the text node).
  _txt(node, t, title) {
    t = str(t);
    if (node.textContent !== t) node.textContent = t;
    if (title !== undefined) { if (title) { if (node.title !== t) node.title = t; } else if (node.hasAttribute("title")) node.removeAttribute("title"); }
    return node;
  }
  _cls(node, c) { if (node.className !== c) node.className = c; }
  _attr(node, k, v) { if (v == null) { if (node.hasAttribute(k)) node.removeAttribute(k); } else if (node.getAttribute(k) !== v) node.setAttribute(k, v); }
  // Make box's children the given spans, reusing them by key: items are [k, className, text | fn (ticking)].
  _spans(box, items) {
    const old = new Map([...box.children].map(c => [c.dataset.k, c]));
    const nodes = items.map(([k, cls, text]) => {
      let el = old.get(k);
      if (!el) { el = this._el("span"); el.dataset.k = k; }
      this._cls(el, cls || "");
      if (typeof text === "function") this._tickText(el, text); else this._txt(el, text);
      return el;
    });
    this._place(box, nodes);
  }
  // The same, with " · " between the items.
  _joined(box, items) {
    const out = [];
    items.forEach((it, i) => { if (i) out.push(["s|" + it[0], "sep", " · "]); out.push(it); });
    this._spans(box, out);
  }
  // Make box's children exactly `want`, moving only what is out of place so a focused control isn't detached.
  _place(box, want) {
    for (const c of [...box.children]) if (!want.includes(c)) c.remove();
    want.forEach((el, i) => { if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null); });
  }
  _key(x) { return x.mid + "|" + str(x.s.id || x.s.name); }
  _toggle(k) { if (this._open.has(k)) this._open.delete(k); else this._open.add(k); this._render(); }
  _find(attr, val) { return [...this.shadowRoot.querySelectorAll(`[data-${attr}]`)].filter(e => e.dataset[attr] === val); }
  _expanded(btn, open) { this._attr(btn, "aria-expanded", String(!!open)); }

  /* ---- scroll anchoring ----
     HA renders the card inside its own scroll container (Safari has no CSS scroll anchoring at all), so the card keeps
     the first element on screen where it was: measure it before a change, scroll by its movement after. */
  _scroller() {
    let n = this;
    while (n) {
      n = n.assignedSlot || n.parentNode || n.host;
      if (!n || n.nodeType === 9 || n === document.documentElement || n === document.body) return null;
      if (n.nodeType !== 1) continue;
      const oy = getComputedStyle(n).overflowY;
      if ((oy === "auto" || oy === "scroll" || oy === "overlay") && n.scrollHeight > n.clientHeight) return n;
    }
    return null;
  }
  _anchor() {
    if (!this.isConnected || Date.now() < this._noAnchorUntil) return null;
    const sc = this._scroller(), se = document.scrollingElement || document.documentElement;
    if ((sc ? sc.scrollTop : se.scrollTop) <= 0) return null;  // at the top the page grows downwards, like a browser does
    const top = sc ? sc.getBoundingClientRect().top : 0, h = sc ? sc.clientHeight : window.innerHeight;
    const picked = [];
    for (const el of this.shadowRoot.querySelectorAll("[data-anchor]")) {
      const r = el.getBoundingClientRect();
      if (!r.height || r.bottom <= top + 1 || r.top >= top + h) continue;
      picked.push({el, y: r.top});
      if (picked.length >= 16) break;
    }
    return picked.length ? {sc, se, picked} : null;
  }
  // Agent rows on screen before this render changes anything (rows above may shrink during it).
  _rowsOnScreen() {
    const out = new Set();
    if (!this.isConnected) return out;
    const sc = this._scroller();
    const top = sc ? sc.getBoundingClientRect().top : 0, h = sc ? sc.clientHeight : window.innerHeight;
    for (const el of this.shadowRoot.querySelectorAll(".thread.main > .ag")) {
      const r = el.getBoundingClientRect();
      if (r.height > 0 && r.bottom > top && r.top < top + h) out.add(el);
    }
    return out;
  }
  _reanchor(an) {
    if (!an) return;
    // Prefer an element whose panel kept its group; if every one on screen moved, keep that panel where it was.
    const moved = [...this._moved];
    for (const skipMoved of [true, false]) for (const p of an.picked) {
      if (!p.el.isConnected || (skipMoved && moved.some(m => m.contains(p.el)))) continue;
      const r = p.el.getBoundingClientRect();
      if (!r.height) continue;
      const d = Math.round(r.top - p.y);
      if (d) (an.sc || an.se).scrollTop += d;
      return;
    }
  }

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
    const an = this._anchor();
    this._onScreenRows = this._rowsOnScreen();
    this._moved = new Set();
    const focus = this._captureFocus();
    this._tickers = [];
    this._syncNew();
    this._syncPlans();
    this._syncSessions();
    this._renderHeader();
    this._note.hidden = !this._hubError;
    this._help.hidden = !this._helpOpen;
    this._expanded(this._helpBtn, this._helpOpen);
    this._restoreFocus(focus);
    this._reanchor(an);
  }

  // Fallback for the rare rebuild of a focused control (prompt changed, machine changed, a panel moved).
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
    if (!el || el.offsetParent === null) return;
    el.focus({preventScroll: true});
    try { if (f.s != null) el.setSelectionRange(f.s, f.e, f.d); } catch (e) { /* not a text field */ }
    el.scrollTop = f.top;
  }

  // Masthead freshness ("Updated 2 s ago", "▲ Updated 3 min ago" after 90 s) and the one-sentence headline.
  _renderHeader() {
    let newest = null;
    for (const snap of Object.values(this._snaps)) {
      const u = ms(snap && snap.machine && snap.machine.updatedAt);
      if (u != null && (newest == null || u > newest)) newest = u;
    }
    const fr = this._fresh;
    if (newest != null) {
      const f = () => {
        const d = Date.now() - newest, stale = d > STALE_MS;
        this._txt(fr, (stale ? "▲ " : "") + "Updated " + ago(d) + " ago");
        this._cls(fr, "fresh" + (stale ? " stale" : ""));
      };
      f(); this._tickers.push(f);
    } else {
      this._txt(fr, Object.keys(this._snaps).length ? "No update time reported" : "Waiting for snapshots…");
      this._cls(fr, "fresh");
    }
    const {wt, wk, total} = this._counts;
    const sig = JSON.stringify([wt, wk, total > 0]);
    if (sig !== this._headSig) {
      this._headSig = sig;
      if (wt) {
        const j = this._btn(wt + (wt === 1 ? " needs you" : " need you"), "jump", "head|jump", null, () => { if (this._firstWait) this._goto(this._firstWait); });
        this._head.replaceChildren(j, document.createTextNode(wk ? ", " + wk + " working." : "."));
      } else this._head.replaceChildren(document.createTextNode(wk ? wk + " working. Nothing needs you." : total ? "All quiet." : "No sessions yet."));
    }
  }

  /* ---- plans ---- */
  _tasksOf(s) {
    const p = s.plan && typeof s.plan === "object" ? s.plan : null;
    if (p && arr(p.tasks).length)
      return arr(p.tasks).filter(t => t && typeof t === "object").sort((u, v) => (u.order ?? 99) - (v.order ?? 99))
        .map(t => ({title: str(t.title || t.id), tier: str(t.tier), status: PLAN_TASK.includes(t.status) ? t.status : "queued"}));
    return arr(s.tasks).filter(t => t && typeof t === "object").map(t => {
      const m = /^\[(\w+)\]\s*(.*)$/.exec(str(t.subject));
      return {title: m ? m[2] : str(t.subject || t.id), tier: m ? m[1] : "", status: TASK[t.status] || "queued"};
    });
  }
  _plans() {
    const out = [], now = Date.now();
    for (const x of this._sessions()) {
      const p = x.s.plan;
      if (!p || typeof p !== "object") continue;
      const fin = p.finished === true, at = ms(p.updatedAt);
      if (fin && (at == null || now - at > FINISHED_PLAN_MS)) continue;
      const tasks = arr(p.tasks).filter(t => t && typeof t === "object").sort((u, v) => (u.order ?? 99) - (v.order ?? 99))
        .map(t => (PLAN_TASK.includes(t.status) ? t.status : "queued"));
      const total = Math.max(0, Number(p.total ?? tasks.length) || 0);
      const done = fin ? total : Math.min(Math.max(0, Number(p.done ?? tasks.filter(t => t === "done").length) || 0), total);
      const sk = this._key(x), name = str(x.s.name || x.s.project);
      const title = str(p.title) || name || "Untitled plan";
      out.push({key: sk + "|" + title, sk, title, phase: fin ? "" : str(p.phase || "running"), done, total, fin, at: at ?? 0,
        segs: tasks, failed: tasks.filter(t => t === "failed").length, src: p.source === "tasks",
        where: name && title.indexOf(name) !== 0 ? name : ""});
    }
    // First sight: unfinished plans first, newest first, then the recently finished ones. After that every plan keeps
    // its place (finishing doesn't move it) and a new plan goes to the end.
    out.sort((a, b) => (a.fin - b.fin) || (b.at - a.at));
    const keys = new Set(out.map(p => p.key));
    for (const k of [...this._planSeq.keys()]) if (!keys.has(k)) this._planSeq.delete(k);
    for (const p of out) if (!this._planSeq.has(p.key)) this._planSeq.set(p.key, this._n++);
    return out.sort((a, b) => this._planSeq.get(a.key) - this._planSeq.get(b.key));
  }

  // Plan rows are kept per plan and patched in place; only which ones are shown (and +N more) changes.
  _syncPlans() {
    const plans = this._plans(), more = this._open.has("plans");
    const shown = more ? plans : plans.slice(0, MAX_PLANS);
    const keep = new Set(plans.map(p => p.key));
    for (const k of [...this._strips.keys()]) if (!keep.has(k)) this._strips.delete(k);
    const want = plans.length ? [] : [this._noPlan];
    for (const p of shown) {
      let st = this._strips.get(p.key);
      if (!st) { st = this._planRow(); this._strips.set(p.key, st); }
      this._fillPlan(st, p);
      want.push(st.li);
    }
    this._place(this._pl, want);
    this._moreBtn.hidden = plans.length <= MAX_PLANS;
    this._txt(this._moreBtn, more ? "Show fewer" : `+${plans.length - MAX_PLANS} more`);
    this._expanded(this._moreBtn, more);
  }
  _planRow() {
    const E = (t, c, x) => this._el(t, c, x);
    const st = {li: E("li"), b: E("button", "plan"), pn: E("span", "pn"), pc: E("span", "pc"), seg: E("span", "seg"), pm: E("span", "pm"), sk: null};
    st.li.dataset.anchor = "1";
    st.b.type = "button";
    const pt = E("span", "pt");
    pt.append(st.pn, st.pc);
    st.seg.setAttribute("aria-hidden", "true");
    st.b.append(pt, st.seg, st.pm);
    st.b.addEventListener("click", () => this._goto(st.sk));
    st.li.append(st.b);
    return st;
  }
  _fillPlan(st, p) {
    st.sk = p.sk;
    st.b.dataset.key = "plan|" + p.sk;
    st.b.dataset.plan = p.sk;
    this._cls(st.b, "plan" + (p.fin ? " fin" : ""));
    this._txt(st.pn, p.title, true);
    this._txt(st.pc, p.fin ? "Done " + hm(p.at) : p.total ? `${p.done}/${p.total}` : "");
    // Signature 1, the segmented track: one segment per task; above MAX_SEG (or without tasks) a plain bar.
    const bar = !(p.segs.length && p.segs.length <= MAX_SEG);
    this._cls(st.seg, "seg" + (bar ? " bar" : ""));
    const want = bar ? 1 : p.segs.length;
    while (st.seg.children.length > want) st.seg.lastChild.remove();
    while (st.seg.children.length < want) st.seg.append(this._el("i"));
    if (bar) {
      const i = st.seg.firstChild, w = (p.total ? Math.round(100 * p.done / p.total) : 0) + "%";
      this._cls(i, "done");
      if (i.style.width !== w) i.style.width = w;
    } else p.segs.forEach((s, n) => { const i = st.seg.children[n]; this._cls(i, s); if (i.style.width) i.style.width = ""; });
    const m = [];
    if (p.phase) m.push(["ph", "phase", p.phase]);
    if (p.src) m.push(["src", "src", "from tasks"]);  // plans derived from the built-in task list
    if (p.where) m.push(["wh", "where", p.where]);
    if (p.failed) m.push(["al", "al", `▲ ${p.failed} failed`]);
    this._joined(st.pm, m);
  }

  // Scroll to a session; an idle one is opened first (Idle section + its row). The target flashes briefly.
  _goto(key) {
    const x = this._sessions().find(v => this._key(v) === key);
    if (!x) return;
    let el = null;
    if (ACTIVE.has(x.s.status) && this._arts.get(key) && this._arts.get(key).el.isConnected) el = this._arts.get(key).el;
    else {
      this._idleOpen = true; this._open.add("row|" + key); this._render();
      const r = this._rows.get(key);
      el = r && r.li.isConnected ? r.li : null;
    }
    if (!el) return;
    this._noAnchorUntil = Date.now() + 1500;  // a scroll correction would cancel the smooth scroll
    let smooth = true;
    try { smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { /* old browser */ }
    el.scrollIntoView({behavior: smooth ? "smooth" : "auto", block: "start"});
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1200);
  }

  /* ---- sessions ---- */
  _canAnswer(x) {
    const s = x.s, w = s.waiting;
    return this._live() && x.canCmd && s.status === "waiting" && arr(s.controls).includes("answer")
      && !!w && typeof w === "object" && w.hubCanAnswer === true && !!w.requestId;
  }
  _waitAt(x) { const w = x.s.waiting; return ms(w && typeof w === "object" ? w.since : null) ?? ms(x.s.updatedAt) ?? 0; }

  _syncSessions() {
    const list = this._sessions().sort((a, b) => cmpDesc(a.s.updatedAt, b.s.updatedAt));
    const waiting = [], working = [], idle = [], seen = new Set();
    for (const x of list) {
      x.key = this._key(x);
      if (seen.has(x.key)) continue;
      seen.add(x.key);
      x.grp = x.s.status === "waiting" ? "w" : x.s.status === "working" ? "a" : "i";
      if (x.grp === "i" && this._hidden.has(x.key)) continue;
      (x.grp === "w" ? waiting : x.grp === "a" ? working : idle).push(x);
    }
    // A hidden session leaves the set once the integration has filtered it out, or when it becomes active again.
    for (const k of [...this._hidden]) if (!seen.has(k) || waiting.some(x => x.key === k) || working.some(x => x.key === k)) this._hidden.delete(k);
    // Order: waiting sessions first, oldest prompt first; then working ones; each keeps the place where it first appeared
    // in its group (newest activity first at first sight), and a session entering a group goes to the group's end.
    for (const k of [...this._seq.keys()]) if (!seen.has(k)) this._seq.delete(k);
    for (const x of [...waiting, ...working, ...idle]) {
      const o = this._seq.get(x.key);
      if (!o || o.grp !== x.grp) {
        if (o && this._arts.get(x.key)) this._moved.add(this._arts.get(x.key).el);
        this._seq.set(x.key, {grp: x.grp, n: this._n++});
      }
    }
    const bySeq = (u, v) => this._seq.get(u.key).n - this._seq.get(v.key).n;
    waiting.sort((u, v) => (this._waitAt(u) - this._waitAt(v)) || bySeq(u, v));
    working.sort(bySeq); idle.sort(bySeq);
    const active = [...waiting, ...working];
    this._counts = {wt: waiting.length, wk: working.length, total: seen.size};
    this._firstWait = waiting.length ? waiting[0].key : null;
    const asks = waiting.filter(x => this._canAnswer(x));
    this._goKey = asks.length ? asks[0].key : null;  // waiting is already sorted oldest prompt first
    this._multi = new Set(active.map(x => x.mid)).size > 1;
    this._idleMulti = new Set(idle.map(x => x.mid)).size > 1;

    const keepArts = new Set();
    const art = (x, idleMode) => {
      const a = this._art(x);
      keepArts.add(x.key);
      this._fillSession(a, x, idleMode);
      return a;
    };
    const want = active.slice(0, MAX_ACTIVE).map(x => art(x, false).el);
    if (!seen.size) {
      this._txt(this._emptyEl, "Your Claude Code sessions appear here, the ones that need you first, with every agent inside them and what each is doing right now.");
      want.push(this._emptyEl);
    }
    this._place(this._sessBox, want);

    const rows = [], keepRows = new Set();
    for (const x of idle.slice(0, MAX_IDLE)) {
      const r = this._row(x.key);
      const exp = this._open.has("row|" + x.key);
      this._fillRow(r, x, exp);
      keepRows.add(x.key);
      if (exp) { const a = art(x, true); this._place(r.det, [a.actw]); }
      this._place(r.li, [r.b, r.xb, r.fb, exp ? r.det : null].filter(Boolean));
      rows.push(r.li);
    }
    for (const [k, r] of this._rows) if (!keepRows.has(k)) { r.li.remove(); this._rows.delete(k); }
    for (const k of [...this._open]) if (/^(row|q|fin|msg|more|tr|note)\|/.test(k) && !seen.has(k.split("|").slice(1, 3).join("|"))) this._open.delete(k);
    this._place(this._irows, rows);
    this._txt(this._idleCnt, String(idle.length));
    this._idleP.hidden = !idle.length;
    this._irows.hidden = !this._idleOpen;
    this._expanded(this._fold, this._idleOpen);
    for (const [k, a] of this._arts) if (!keepArts.has(k)) { a.el.remove(); a.actw.remove(); this._arts.delete(k); }
  }

  _art(x) {
    let a = this._arts.get(x.key);
    if (a) return a;
    const E = (t, c, v) => this._el(t, c, v), key = x.key;
    a = {el: E("article", "panel sess"), sh: E("div", "sh"), nm: E("h3", "sn"), stw: E("span", "stw"), st: E("span", "st"),
      sum: E("p", "sum"), metaw: E("div", "metaw"), meta: E("div", "meta"), ml: E("span", "ml"), mc: E("span", "mc"),
      ctx: E("div", "ctx"), ctxI: E("i"), agents: E("div", "agents"), thread: E("div", "thread main"), fin: E("div", "fin"),
      q: E("div", "qw"), actw: E("div", "actw"), acts: E("div", "acts"), more: E("div", "more"), kv: E("dl", "kv"),
      parts: {}, qSig: null, finSig: null, kvSig: null, kvLast: null,
      rows: new Map(), aseq: new Map(), an: 0, shown: new Set(), cleared: new Set()};
    a.el.dataset.sess = key;
    a.el.tabIndex = -1;
    a.stw.append(a.st);
    a.sh.append(a.nm, a.stw);
    a.meta.append(a.ml, a.mc);
    a.ctx.append(a.ctxI);
    a.ctx.setAttribute("aria-hidden", "true");
    a.metaw.append(a.meta, a.ctx);
    a.agents.append(a.thread, a.fin);
    a.msgBtn = this._btn("Message", "k q", key + "|msgbtn", null, () => {
      this._toggle("msg|" + key);
      if (this._open.has("msg|" + key)) { const t = this._find("key", key + "|text")[0]; if (t) t.focus(); }
    });
    a.moreBtn = this._btn("More", "k q", key + "|morebtn", null, () => this._toggle("more|" + key));
    a.acts.append(a.msgBtn, a.moreBtn);
    a.more.append(a.kv);
    for (const n of [a.sh, a.sum, a.metaw, a.q, a.actw]) n.dataset.anchor = "1";
    this._arts.set(key, a);
    return a;
  }

  _row(key) {
    let r = this._rows.get(key);
    if (r) return r;
    const E = (t, c) => this._el(t, c);
    r = {li: E("li", "irow"), b: E("button", "ib"), nm: E("span", "in"), im: E("span", "im"), is: E("span", "is"),
      xb: null, fb: this._fbNode("dis|" + key), det: E("div", "idet"), cur: null};
    r.li.dataset.anchor = "1";
    r.li.dataset.row = key;
    r.li.tabIndex = -1;
    r.b.type = "button";
    r.b.dataset.key = "row|" + key;
    r.b.append(r.nm, r.im, r.is);
    r.b.addEventListener("click", () => this._toggle("row|" + key));
    this._rows.set(key, r);
    return r;
  }

  _fillRow(r, x, exp) {
    const {s, mname, offline} = x, key = x.key, fk = "dis|" + key;
    r.cur = x;
    this._cls(r.li, "irow" + (exp ? " open" : ""));
    this._expanded(r.b, exp);
    const name = str(s.name || s.localName || s.project || s.id || "Session");
    this._txt(r.nm, name);
    const im = [];
    if (this._idleMulti && !offline) im.push(["m", "", mname]);
    if (s.status === "ended") im.push(["end", "", "Done " + hm(s.updatedAt)]);
    else if (ms(s.updatedAt) != null) im.push(["act", "", () => since(s.updatedAt) + " ago"]);
    if (x.unconfigured && !offline) im.push(["unc", "", "unconfigured"]);
    if (offline) im.push(["off", "off", mname + " offline"]);
    this._joined(r.im, im);
    this._txt(r.is, s.summary, true);
    r.is.hidden = !s.summary;
    // × hides the session at once (crew_hub/dismiss); the integration then filters it out for every device.
    const canDismiss = this._live() && !!s.id;
    if (canDismiss && !r.xb) {
      r.xb = this._btn("×", "x", fk, fk, async () => {
        const c = r.cur;
        if (!c) return;
        this._hidden.add(key);
        this._render();
        if (!await this._dismiss(fk, {machine: c.mid, session: str(c.s.id), session_dismissed: true})) {
          this._hidden.delete(key);
          this._render();
          this._paintFb(fk);
        }
      });
    } else if (!canDismiss && r.xb) { r.xb.remove(); r.xb = null; }
    if (r.xb) { r.xb.setAttribute("aria-label", "Hide " + name); r.xb.title = "Hide"; }
  }

  // Patched in place: persistent nodes get new text; only parts whose inputs changed are rebuilt.
  // idleMode: the session sits in the Idle section, where only its actions (Message, More) show.
  _fillSession(a, x, idleMode) {
    const {s, mname, offline, unconfigured} = x, key = x.key;
    const waiting = s.status === "waiting";
    this._cls(a.el, "panel sess" + (offline ? " cue-al" : waiting ? " cue-you" : ""));
    this._txt(a.nm, s.name || s.localName || s.project || s.id || "Session");
    const [stc, stt] = offline ? ["st off", "Offline"] : waiting ? ["st you", "Needs you"] : s.status === "working" ? ["st", "Working"]
      : s.status === "ended" ? ["st", "Done " + hm(s.updatedAt)] : ["st", "Idle"];
    this._cls(a.st, stc);
    this._txt(a.st, stt);
    this._txt(a.sum, s.summary, true);
    a.sum.hidden = !s.summary;
    // Meta line: model, tokens, cost (machine only when the panels come from more than one machine), then context.
    const bits = [];
    if (this._multi) bits.push(mname);
    if (s.model) bits.push(modelName(s.model));
    const tk = tokText(s.tokens);
    if (tk) bits.push(tk);
    if (typeof s.costUsd === "number" && isFinite(s.costUsd)) bits.push("$" + s.costUsd.toFixed(2));
    if (unconfigured) bits.push("unconfigured");
    this._txt(a.ml, bits.join(" · "), true);
    const cx = s.ctx, hasCtx = !!(cx && Number(cx.max) > 0);
    a.ctx.hidden = a.mc.hidden = !hasCtx;
    if (hasCtx) {
      const pct = Math.max(0, Math.min(100, Math.round(100 * (Number(cx.used) || 0) / Number(cx.max)))), warn = pct >= 85;
      this._cls(a.mc, "mc" + (warn ? " warn" : ""));
      this._txt(a.mc, (warn ? "▲ " : "") + pct + "% context");
      this._cls(a.ctx, "ctx" + (warn ? " warn" : ""));
      if (a.ctxI.style.width !== pct + "%") a.ctxI.style.width = pct + "%";
    }
    // The ask: rebuilt only when the prompt (or whether the hub can answer it) changes.
    const w = s.waiting, can = this._canAnswer(x);
    const ask = waiting && w && typeof w === "object" ? w : null;
    this._part(a, "ask", ask ? JSON.stringify([w.kind, w.requestId, w.tool, w.text, w.questions, can]) : "", () => this._buildAsk(x, key, w, can));
    const askNode = a.parts.ask && a.parts.ask.node;
    if (askNode) {
      if (askNode._go) askNode._go.classList.toggle("go", key === this._goKey);
      const t0 = ms(w.since);
      this._tickText(askNode._since, () => (t0 == null ? "" : "waiting " + ago(Date.now() - t0)));
      if (askNode._note) askNode._note.hidden = !this._open.has("note|" + askNode._base) && !this._drafts.get(askNode._base + "|note");
      if (askNode._noteBtn) askNode._noteBtn.hidden = !askNode._note.hidden;
    }
    if (!idleMode) this._syncAgents(a, x, key);
    this._syncQueue(a, s, key);
    this._syncActions(a, x, key);
    this._place(a.el, [a.sh, a.sum, a.metaw, askNode, a.agents, a.q, idleMode ? null : a.actw].filter(Boolean));
  }

  // Signature 2, the thread: agent rows keep the order in which they first appeared. A running agent gets a row; when it
  // finishes the row stays where it is (dimmed, same height) until crew-hubd prunes the agent or Clear finished dismisses
  // it. Agents already finished when first seen go behind the "N finished" toggle.
  _syncAgents(a, x, key) {
    const s = x.s;
    const tasks = new Map(arr(s.plan && s.plan.tasks).filter(t => t && t.agentId).map(t => [t.agentId, t]));
    // Agents with no title and no steps (a start with nothing to show yet) aren't rendered.
    const agents = arr(s.agents).filter(g => g && typeof g === "object"
      && (g.title || (tasks.get(g.id) || {}).title || Number(g.steps) > 0));
    const aid = g => str(g.id) || "t:" + str(g.title);
    const ids = new Set(agents.map(aid));
    // A row whose agent is gone (pruned by crew-hubd) stays as it was while it is on screen, and goes once it has been
    // scrolled away (above the viewport the scroll correction hides that). Clear finished removes its rows at once.
    const ghosts = [];
    for (const k of [...a.aseq.keys()]) {
      if (ids.has(k)) continue;
      const r = a.rows.get(k);
      if (r && a.shown.has(k) && !a.cleared.has(k) && this._onScreenRows.has(r.el)) { ghosts.push(k); continue; }
      a.aseq.delete(k); a.shown.delete(k); a.cleared.delete(k);
    }
    for (const g of agents) {
      const k = aid(g);
      if (!a.aseq.has(k)) a.aseq.set(k, a.an++);
      if (g.status === "running") a.shown.add(k);
    }
    const bySeq = (p, q) => a.aseq.get(aid(p)) - a.aseq.get(aid(q));
    let main = agents.filter(g => a.shown.has(aid(g))).sort(bySeq);
    let extra = main.length - MAX_AGENT_ROWS;
    for (const g of main) {  // too many rows: finished ones leave first, oldest first
      if (extra <= 0) break;
      if (g.status !== "running") { a.shown.delete(aid(g)); extra--; }
    }
    main = main.filter(g => a.shown.has(aid(g))).slice(0, MAX_AGENT_ROWS);
    const keep = new Set(ghosts);
    for (const g of main) {
      const k = aid(g);
      let r = a.rows.get(k);
      if (!r) { r = this._agentNode(); a.rows.set(k, r); }
      this._fillAgent(r, g, tasks.get(g.id), g.status !== "running", "tr|" + key + "|" + k);
      keep.add(k);
    }
    const nodes = [...keep].sort((p, q) => a.aseq.get(p) - a.aseq.get(q)).map(k => a.rows.get(k).el);
    for (const k of [...a.rows.keys()]) if (!keep.has(k)) a.rows.delete(k);
    this._place(a.thread, nodes);
    a.thread.hidden = !nodes.length;
    const shownIds = new Set(main.map(aid));
    const finished = agents.filter(g => g.status !== "running");
    this._syncFinished(a, x, key, finished.filter(g => !shownIds.has(aid(g))).sort(bySeq), finished, tasks);
    a.agents.hidden = !nodes.length && !a.fin.firstChild;
  }

  // Finished agents without a row of their own (done | failed | stopped) behind an "N finished" toggle, and
  // "Clear finished" (crew_hub/dismiss) for every finished agent, including the dimmed rows held in place.
  // Rebuilt only when they change, so taps on these keys aren't lost to the once-a-second re-render.
  _syncFinished(a, x, key, fin, all, tasks) {
    const fk = key + "|clear", open = this._open.has("fin|" + key), live = this._live() && !!x.s.id;
    const ids = all.map(g => str(g.id)).filter(Boolean);
    const fbOn = this._fb.has(fk); // "Cleared HH:MM" stays in place after the finished agents go
    const sig = fin.length || (live && ids.length) ? JSON.stringify([fin, fin.map(g => tasks.get(g.id) || null), open, live, ids])
      : fbOn ? "fb" : "";
    if (sig === a.finSig) return;
    a.finSig = sig;
    if (!sig) { a.fin.replaceChildren(); return; }
    if (sig === "fb") { a.fin.replaceChildren(this._fbNode(fk)); return; }
    const tf = this._el("div", "tf keys");
    tf.dataset.anchor = "1";
    if (fin.length) {
      const t = this._btn(`${fin.length} finished`, "k q", key + "|fin", null, () => this._toggle("fin|" + key));
      t.append(this._chev());
      this._expanded(t, open);
      tf.append(t);
    }
    if (live && ids.length) {
      tf.append(this._btn("Clear finished", "k q", fk, fk, () => {
        if (!ids.length) return;
        for (const id of ids) a.cleared.add(id);
        this._dismiss(fk, {machine: x.mid, session: str(x.s.id), agents: ids}, "Cleared " + nowHm());
      }));
    }
    const kids = [];
    if (open && fin.length) {
      const th = this._el("div", "thread old");
      fin.slice(0, MAX_FINISHED).forEach(g => th.append(this._endLine(g, tasks.get(g.id))));
      kids.push(th);
    }
    kids.push(tf, this._fbNode(fk));
    a.fin.replaceChildren(...kids);
  }

  // Queue: one row ("NEXT <first queued task> +N", or "TASKS") that opens the full task list.
  _syncQueue(a, s, key) {
    const items = this._tasksOf(s);
    const qk = "q|" + key, open = this._open.has(qk);
    const sig = items.length ? JSON.stringify([items, open]) : "";
    if (sig === a.qSig) return;
    a.qSig = sig;
    a.q.hidden = !sig;
    if (!sig) { a.q.replaceChildren(); return; }
    const queued = items.filter(t => t.status === "queued");
    const b = this._btn("", "next", key + "|next", null, () => this._toggle(qk));
    if (queued.length) {
      const nt = this._el("span", "nt", queued[0].title);
      nt.title = queued[0].title;
      b.append(this._el("span", "lab", "Next"), nt);
      if (queued.length > 1) b.append(this._el("span", "mono", "+" + (queued.length - 1)));
    } else b.append(this._el("span", "lab", "Tasks"), this._el("span", "nt"));
    b.append(this._chev());
    this._expanded(b, open);
    const kids = [b];
    if (open) {
      const ol = this._el("ol", "tasks");
      for (const t of items) {
        const li = this._el("li", t.status), tt = this._el("span", "tt", t.title);
        if (t.tier) tt.append(this._el("span", "tier", "  " + t.tier));
        li.append(this._el("span", "tw", WORD[t.status]), tt);
        ol.append(li);
      }
      kids.push(ol);
    }
    a.q.replaceChildren(...kids);
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

  // Message (composer behind a quiet key) and More (details, Interrupt / Stop).
  _syncActions(a, x, key) {
    const s = x.s, allowed = arr(s.controls).filter(v => typeof v === "string");
    const on = this._live() && x.canCmd;
    const has = act => on && allowed.includes(act);
    const msgOpen = this._open.has("msg|" + key), moreOpen = this._open.has("more|" + key);
    this._part(a, "msg", has("send") ? "send" : "", () => this._buildMsg(x, key));
    a.msgBtn.hidden = !a.parts.msg;
    this._expanded(a.msgBtn, msgOpen && !!a.parts.msg);
    this._expanded(a.moreBtn, moreOpen);
    // More: details said nowhere else.
    const kv = [["Machine", x.mname + (x.offline ? " (offline)" : x.unconfigured ? " (unconfigured)" : "")]];
    if (s.localName) kv.push(["Local name", str(s.localName)]);
    if (s.effort) kv.push(["Effort", str(s.effort)]);
    const tk = s.tokens && typeof s.tokens === "object" ? s.tokens : null;
    if (tk && tk.cacheHit != null && Number.isFinite(Number(tk.cacheHit))) kv.push(["Cached", Math.round(Number(tk.cacheHit) * 100) + "%"]);
    if (tk && Number(tk.turns) > 0) kv.push(["Turns", String(Number(tk.turns))]);
    if (ms(s.startedAt) != null) kv.push(["Started", hm(s.startedAt)]);
    const last = ms(s.updatedAt);
    if (last != null) kv.push(["Last report", null]);
    const kvSig = JSON.stringify(kv);
    if (kvSig !== a.kvSig) {
      a.kvSig = kvSig; a.kvLast = null;
      const kids = [];
      for (const [k, v] of kv) {
        const dd = this._el("dd", null, v);
        if (v == null) a.kvLast = dd;
        kids.push(this._el("dt", null, k), dd);
      }
      a.kv.replaceChildren(...kids);
    }
    if (a.kvLast) this._tickText(a.kvLast, () => since(last) + " ago");
    const bg = ["interrupt", "stop"].filter(has);
    this._part(a, "bg", bg.join(","), () => this._buildBg(x, key, bg));
    this._place(a.more, [a.kv, a.parts.bg && a.parts.bg.node].filter(Boolean));
    a.more.hidden = !moreOpen;
    const comp = a.parts.msg && a.parts.msg.node;
    if (comp) comp.hidden = !msgOpen;
    this._place(a.actw, [a.acts, comp, a.more].filter(Boolean));
  }

  _input(tag, key, attrs) {
    const el = this._el(tag);
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
    const f = this._el("p", "fb");
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
    for (const b of this._find("grp", fk)) { b.disabled = on; if (b._upd) b._upd(); }
  }

  // Two-tap confirm: the first tap arms the key (dark ink, "Tap again to stop") for 4 s, the second tap runs it.
  // guard() returns an error text when the action can't be sent yet; then the key isn't armed.
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
      b.classList.toggle("dark", armed);
    }
  }

  async _send(fk, machine, action, args, okText) {
    const conn = this._hass && this._hass.connection;
    if (!conn || typeof conn.sendMessagePromise !== "function") { this._setFb(fk, "Not connected to Home Assistant", "err"); return false; }
    this._setBusy(fk, true);
    this._setFb(fk, "Sending…", "");
    let ok = false;
    try {
      const r = await conn.sendMessagePromise({type: "crew_hub/command", machine, action, args});
      if (r && r.ok === true) { ok = true; this._setFb(fk, okText ? okText() : "Sent " + nowHm(), "ok"); }
      else this._setFb(fk, CrewHubCard.errText(r && r.error, r && r.detail), "err");
    } catch (e) {
      this._setFb(fk, CrewHubCard.errText(e && e.code, e && e.message), "err");
    } finally {
      this._setBusy(fk, false);
    }
    return ok;
  }
  // crew_hub/dismiss: on success the integration pushes the filtered machine, which removes the items.
  async _dismiss(fk, payload, okText) {
    const conn = this._hass && this._hass.connection;
    if (!conn || typeof conn.sendMessagePromise !== "function") { this._setFb(fk, "Not connected to Home Assistant", "err"); return false; }
    this._setBusy(fk, true);
    let ok = false;
    try {
      const r = await conn.sendMessagePromise(Object.assign({type: "crew_hub/dismiss"}, payload));
      ok = !!(r && r.ok === true);
      if (!ok) this._setFb(fk, CrewHubCard.errText(r && r.error, r && r.detail), "err");
      else if (okText) this._setFb(fk, okText, "ok");
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
    const fk = key + "|msg", name = str(x.s.name || x.s.project || "this session");
    const c = this._el("div", "comp");
    const ta = this._input("textarea", key + "|text", {rows: "1", maxlength: "4000", "aria-label": "Message to " + name, placeholder: "Message " + name});
    ta.dataset.grow = "1";
    requestAnimationFrame(() => this._grow(ta));
    const urgentKey = key + "|urgent";
    const urg = this._el("label", "tick");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.key = urgentKey; cb.checked = !!this._drafts.get(urgentKey);
    cb.addEventListener("change", () => this._drafts.set(urgentKey, cb.checked));
    urg.append(cb, this._el("span", null, "Urgent"), this._el("span", "mono", "stops the current reply"));
    const send = this._btn("Send", "k", fk, fk, async () => {
      const text = ta.value;
      if (!text.trim()) return;
      const urgent = !!this._drafts.get(urgentKey);
      if (await this._send(fk, x.mid, "send", {session: str(x.s.id), text, urgent}, () => (urgent ? "Sent urgent " : "Sent ") + nowHm())) {
        this._drafts.delete(key + "|text"); this._drafts.delete(urgentKey);
        for (const el of this._find("key", key + "|text")) { el.value = ""; this._grow(el); }
        for (const el of this._find("key", urgentKey)) el.checked = false;
      }
      upd();
    });
    // Send needs text; the Urgent tick box shows only once there is some.
    const upd = () => { const has = !!ta.value.trim(); urg.hidden = !has; send.disabled = this._busy.has(fk) || !has; };
    send._upd = upd;
    ta.addEventListener("input", upd);
    upd();
    const row = this._el("div", "crow");
    row.append(urg, this._el("span", "sp"), send);
    c.append(ta, row, this._fbNode(fk));
    return c;
  }

  // The ask: label (tool or header) + "waiting 3 min", the request at 19 px, then Allow / Deny / Add a note, or the
  // question's options (a single choice sends on tap; otherwise an Answer key). The oldest answerable prompt's
  // Allow / Answer key gets .go (orange) in _fillSession.
  _buildAsk(x, key, w, can) {
    const fk = key + "|ans", req = str(w.requestId), base = key + "|" + req;
    const box = this._el("div", "ask");
    box.dataset.anchor = "1";
    box._base = base;
    const answer = (args, okText) => this._send(fk, x.mid, "answer", Object.assign({session: str(x.s.id), requestId: req}, args), okText);
    const qs = w.kind === "question" ? arr(w.questions).filter(q => q && typeof q === "object" && typeof q.question === "string") : [];
    box._since = this._el("span", "mono");
    const at = this._el("div", "at");
    at.append(this._el("span", "lab", qs.length ? str(qs[0].header || "Question") : str(w.tool || "Permission")), box._since);
    box.append(at);
    if (qs.length) {
      const direct = qs.length === 1 && !qs[0].multiSelect;
      qs.forEach((q, qi) => {
        if (qi) box.append(this._el("span", "lab", str(q.header || "Question")));
        box.append(this._el("p", "aq", q.question));
        if (!can) return;
        const keys = this._el("div", "keys");
        const sk = base + "|q" + qi;
        const labels = arr(q.options).map(o => (o && typeof o === "object" ? o.label : o)).filter(l => typeof l === "string" && l);
        labels.forEach((lab, li) => {
          const b = this._btn(lab, "k", sk + "|o" + li, fk, () => {
            if (direct) { answer({decision: "allow", answers: {[q.question]: lab}}, () => "Answered " + nowHm() + " · " + lab); return; }
            let sel = arr(this._drafts.get(sk));
            sel = q.multiSelect ? (sel.includes(lab) ? sel.filter(v => v !== lab) : [...sel, lab]) : [lab];
            this._drafts.set(sk, sel);
            for (const [i, l] of labels.entries()) for (const el of this._find("key", sk + "|o" + i)) {
              el.setAttribute("aria-pressed", String(sel.includes(l))); el.classList.toggle("on", sel.includes(l));
            }
          });
          if (!direct) { const on = arr(this._drafts.get(sk)).includes(lab); b.setAttribute("aria-pressed", String(on)); b.classList.toggle("on", on); }
          keys.append(b);
        });
        box.append(keys);
      });
      if (can && !direct) {
        box._go = this._btn("Answer", "k", base + "|answer", fk, () => {
          const answers = {};
          for (const [qi, q] of qs.entries()) {
            const sel = arr(this._drafts.get(base + "|q" + qi));
            if (!sel.length) { this._setFb(fk, "Pick an answer for each question", "err"); return; }
            answers[q.question] = sel.join(", ");
          }
          answer({decision: "allow", answers}, () => "Answered " + nowHm());
        });
        const keys = this._el("div", "keys");
        keys.append(box._go);
        box.append(keys);
      }
    } else {
      box.append(this._el("p", "aq", str(w.text || w.kind) || "Waiting for your answer"));
      if (can) {
        const note = this._input("input", base + "|note", {type: "text", maxlength: "500", placeholder: "Note for Claude (optional)", "aria-label": "Note for Claude"});
        note.className = "note";
        const msg = () => { const v = note.value.trim(); return v ? {message: v} : {}; };
        const ok = word => () => word + " " + nowHm() + (note.value.trim() ? " · with your note" : "");
        box._go = this._btn("Allow", "k", base + "|allow", fk, () => answer(Object.assign({decision: "allow"}, msg()), ok("Allowed")));
        box._note = note;
        box._noteBtn = this._btn("Add a note", "k q", base + "|addnote", null, () => {
          this._open.add("note|" + base); this._render(); note.focus();
        });
        const keys = this._el("div", "keys");
        keys.append(box._go, this._btn("Deny", "k", base + "|deny", fk, () => answer(Object.assign({decision: "deny"}, msg()), ok("Denied"))), box._noteBtn);
        box.append(keys, note);
      }
    }
    if (!can) box.append(this._el("p", "mono", "Answer in claude.ai"));
    box.append(this._fbNode(fk));
    return box;
  }

  _buildBg(x, key, acts) {
    const fk = key + "|bg";
    const g = this._el("div", "grp");
    const keys = this._el("div", "keys");
    for (const act of acts) {
      const label = act === "stop" ? "Stop" : "Interrupt";
      keys.append(this._twoTap(label, "Tap again to " + act, "k", key + "|" + act, fk,
        () => this._send(fk, x.mid, act, {session: str(x.s.id)}, () => (act === "stop" ? "Stop sent " : "Interrupted ") + nowHm())));
    }
    g.append(keys, this._fbNode(fk));
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
    this._newBtn.hidden = !list.length;
    this._expanded(this._newBtn, this._newOpen && !!list.length);
    this._newP.hidden = !list.length || !this._newOpen;
    // Machines that can't start a session are listed greyed out.
    const others = list.length ? Object.entries(this._snaps).filter(([mid]) => !list.some(m => m.mid === mid)).map(([mid, snap]) => {
      const m = (snap && snap.machine) || {}, off = str((snap && snap.status) || m.status).toLowerCase() === "offline";
      return {mid, name: str(m.name || m.id || mid) + (off ? " (offline)" : " (commands off)")};
    }) : [];
    const sig = JSON.stringify([list, others]);
    if (sig === this._newSig) return;
    this._newSig = sig;
    if (!list.length) { this._newP.replaceChildren(); return; }
    const fk = "new|start";
    const form = this._el("div", "form");
    const field = (label, el, wide) => {
      const id = "chn-" + (++this._ids), l = this._el("label", "lab", label), f = this._el("div", "f" + (wide ? " wide" : ""));
      el.id = id; l.htmlFor = id;
      f.append(l, el);
      form.append(f);
      return el;
    };
    const select = (key, label, options) => {
      const sel = this._el("select");
      sel.dataset.key = key;
      for (const o of options) { const op = this._el("option", null, o.label); op.value = o.value; if (o.off) op.disabled = true; sel.append(op); }
      const want = this._drafts.get(key), ok = options.filter(o => !o.off);
      sel.value = ok.some(o => o.value === want) ? want : (ok[0] ? ok[0].value : "");
      this._drafts.set(key, sel.value);
      sel.addEventListener("change", () => { this._drafts.set(key, sel.value); if (key === "new|machine") { this._newSig = null; this._syncNew(); } });
      return field(label, sel);
    };
    const mSel = select("new|machine", "Machine", [...list.map(m => ({value: m.mid, label: m.name})), ...others.map(m => ({value: m.mid, label: m.name, off: true}))]);
    const mach = list.find(m => m.mid === mSel.value) || list[0];
    const fSel = select("new|folder", "Folder", mach.folders.map(f => ({value: f.key, label: f.label})));
    const modSel = select("new|model", "Model", mach.models.map(v => ({value: v, label: modelName(v)})));
    const pSel = select("new|mode", "Permissions", mach.modes.map(v => ({value: v, label: PERM[v] || v})));
    const name = field("Name", this._input("input", "new|name", {type: "text", maxlength: "60", autocomplete: "off", placeholder: "What to call it"}), true);
    const prompt = field("Prompt", this._input("textarea", "new|prompt", {rows: "3", maxlength: "4000", placeholder: "What should Claude do?"}), true);
    const start = this._twoTap("Start", "Tap again to start", "k", "new|startbtn", fk, async () => {
      const args = {folder: fSel.value, model: modSel.value, permissionMode: pSel.value, name: name.value.trim(), prompt: prompt.value};
      if (await this._send(fk, mach.mid, "start", args, () => "Started " + nowHm() + " · " + args.name)) {
        for (const k of ["new|name", "new|prompt"]) { this._drafts.delete(k); for (const el of this._find("key", k)) el.value = ""; }
      }
    }, () => (name.value.trim() && prompt.value.trim() && fSel.value && modSel.value && pSel.value ? null : "Name and prompt are needed"));
    const ph = this._el("div", "ph");
    ph.append(this._el("h3", "lab", "New session"));
    const row = this._el("div", "crow");
    row.append(this._fbNode(fk), this._el("span", "sp"), start);
    this._newP.replaceChildren(ph, form, row);
  }

  // Agent row with a fixed line count: name + elapsed (or "Done 14:08"), the current step (or the summary), and
  // "23 steps · 48k tok · careful". Built once per agent, then patched. A tap opens the last TRAIL_LINES steps with times.
  _agentNode() {
    const E = (t, c) => this._el(t, c);
    const r = {el: E("div", "ag"), node: E("span", "node"), b: E("div", "agb"), nm: E("span", "agn"), age: E("span", "age"),
      now: E("span", "now"), agm: E("span", "agm"), trail: E("ol", "trail"), tr: [], okey: null, hasTrail: false};
    r.el.dataset.anchor = "1";
    const agt = E("span", "agt");
    agt.append(r.nm, r.age);
    r.b.append(agt, r.now, r.agm);
    for (let i = 0; i < TRAIL_LINES; i++) {
      const li = E("li"), tm = E("span", "tm"), sp = E("span");
      li.append(tm, sp);
      r.trail.append(li);
      r.tr.push({tm, sp});
    }
    const flip = () => { if (r.hasTrail && r.okey) this._toggle(r.okey); };
    r.b.addEventListener("click", flip);
    r.b.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } });
    r.el.append(r.node, r.b, r.trail);
    return r;
  }
  _fillAgent(r, a, task, held, okey) {
    const status = AGENT.includes(a.status) ? a.status : "done";
    const running = status === "running";
    r.okey = okey;
    this._cls(r.el, "ag " + (running ? "run" : status) + (held ? " held" : ""));
    this._txt(r.nm, (task && task.title) || a.title || a.type || ("Agent " + str(a.id).slice(0, 7)), true);
    if (running) { const t0 = ms(a.startedAt); this._tickText(r.age, () => (t0 == null ? "" : ago(Date.now() - t0))); }
    else this._txt(r.age, WORD[status] + (hm(a.endedAt) ? " " + hm(a.endedAt) : ""));
    const cur = a.current && a.current.text ? a.current : null;
    this._txt(r.now, running ? (cur ? cur.text : "") : str(a.summary || (task && task.summary)), true);
    const steps = Number(a.steps) || 0, m = [steps + " step" + (steps === 1 ? "" : "s")];
    const tk = tokText(a.tokens), tier = (task && task.tier) || tierOf(a.type);
    if (tk) m.push(tk);
    if (tier) m.push(str(tier));
    if (status === "stopped" && a.stopReason) m.push(str(a.stopReason));
    this._txt(r.agm, m.join(" · "), true);
    const trail = arr(a.trail).filter(t => t && t.text && !(cur && running && t.text === cur.text && t.at === cur.at))
      .slice(-TRAIL_LINES).reverse();
    r.hasTrail = trail.length > 0;
    const open = r.hasTrail && this._open.has(okey);
    this._attr(r.b, "role", r.hasTrail ? "button" : null);
    this._attr(r.b, "tabindex", r.hasTrail ? "0" : null);
    this._attr(r.b, "aria-expanded", r.hasTrail ? String(open) : null);
    this._attr(r.b, "data-key", r.hasTrail ? okey : null);
    r.trail.hidden = !open;
    r.tr.forEach((p, i) => {
      const t = trail[i];
      this._txt(p.tm, t ? hm(t.at) : "");
      this._txt(p.sp, t ? t.text : "", true);
    });
  }
  // One dim line for an agent behind "N finished": "Done 14:08 · title" (+ why it stopped).
  _endLine(a, task) {
    const k = a.status === "failed" ? "failed" : a.status === "stopped" ? "stopped" : "done";
    const el = this._el("div", "ag " + k), p = this._el("p", "agl"), t = (task && task.title) || a.title || a.type || "Agent";
    el.append(this._el("span", "node"));
    p.append(this._el("span", "tw", WORD[k] + (hm(a.endedAt) ? " " + hm(a.endedAt) : "")), document.createTextNode(str(t)));
    if (k === "stopped" && a.stopReason) p.append(this._el("span", "why", " · " + str(a.stopReason)));
    p.title = p.textContent;
    el.append(p);
    return el;
  }
}

if (!customElements.get("crew-hub-card")) customElements.define("crew-hub-card", CrewHubCard);
window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === "crew-hub-card"))
  window.customCards.push({type: "crew-hub-card", name: "Crew Hub", description: "Live Claude Code sessions and agents"});
})();
