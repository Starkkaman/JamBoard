# Crew Hub card in Callsheet's Jam look: design study

Status: proposal for Isaac, 2026-11-06. Nothing in `card/` or `custom_components/` has changed.
These files are in this folder:
- `mockup.html`: works offline. It renders `card/fixtures/snapshot-sample.json` with the clock frozen at 26 Oct 14:30. Every value is set with textContent only.
- `crewhub-jam-day-400.png`, `crewhub-jam-night-400.png` and `crewhub-jam-day-1280.png` (Idle, New session and one trail open).
- `callsheet-reference-400.png`: Callsheet d27188c in Jam, day, using its sample data.
- `fonts/`: Jam's two fonts and their OFL licences.

Mockup URL options: `?mode=auto|light|dark`, `&theme=light|dark` (the HA theme), `&open=idle,new,help,trail,more`, and `&shot=1` to hide the review bar.

## Sources
- **Look.** Callsheet's default look is **Jam**. In d27188c `app.js`, `DEFAULT_LOOK='jam'` and `ENABLED_LOOKS=['jam','easy']`; Bold and Clean were switched off on 2026-11-06. Only Jam is built here. There are no other looks and no look switcher.
- **Rules.** The jam-design HANDOFF.md (it wins over BIBLE.md), together with pass 6 (`mockups/pass6/callsheet.html` and `notes/pass6-callsheet.md`). Crew Hub is "a design of its own in the shared language". It is not a copy of Callsheet's screens.

## Token block (source and how it is adapted)
- **Copied verbatim from d27188c `app/static/app.css`:**
  - lines 593–668 (Jam day, night, auto and phone)
  - lines 2022–2023 (motion scale)
  - line 2037 (reduced motion)
  - lines 2608–2610 (panel base: `--pgap --ppad --prad --key-h`)
  - the @font-face rules on lines 9 and 27
- **Generator.** `gen.py` (in the session scratchpad) copies those lines and makes three mechanical rewrites so they work inside a shadow root:
  - `:root[data-theme=dark]` becomes `:host([data-theme=dark])`
  - `:root:not([data-theme=light])` becomes `:host(:not([data-theme=light]))`
  - `@media (max-width:700px)` becomes `@container crew (max-width:700px)`, with `.cq{container:crew/inline-size}` wrapped around `.app`. The card's width decides, not the window's.
- **One addition:** `@media (pointer:coarse){--key-h:44px}`, so touch targets stay 44 px on a wide tablet.
- **Component CSS uses tokens only.** Raw colours: none.
- **Two marks use the deeper token for AA non-text contrast:**
  - Red dots, the failed node and the offline edge use `--alert` (6.0 day, 7.4 night). `--alert-key` is only 2.8 on the day panel.
  - The context bar's warn fill uses `--warn-ink` (4.9). `--warn-line` is only 2.7 on the track.
- **Fonts in a real card.** Chrome ignores @font-face inside a shadow root. The card should inject one `<style data-crew-hub-fonts>` into `document.head`, with URLs resolved against the card's own URL (`/crew_hub/crew-hub-card.js`, so the fonts sit at `/crew_hub/fonts/…`). This replaces today's Google Fonts `FONT_HREF`/`loadFonts()`, which is an outside request.

## Modes (light and dark)
- `.app[data-look=jam][data-base=panel][data-mode=auto|light|dark]` works the same as in Callsheet.
- **Default:** `auto`. A card config option `mode: light|dark|auto` forces a mode.
- **Auto in Callsheet.** It follows the device through `prefers-color-scheme`. Callsheet never sets `:root[data-theme]`, and its sunset rule is for the Depth look only, so it does not apply to Jam.
- **Auto in the card.** The card sets the host's `data-theme` from `hass.themes.darkMode`, so auto follows Home Assistant's own day/night. HA set to Light on a dark device gives day. With no theme value, auto follows the device.
- **Checked:** auto plus a dark device gives night; `mode=dark` on a light device gives night; `theme=light` on a dark device gives day.

## Fonts and sizes (Jam needs two files, 150 KB)
| Font file | Size | Weights | Used for |
|---|---|---|---|
| `jost.ttf` | 135 KB | variable 100–900 | words |
| `ibm-plex-mono-400.woff2` | 15 KB | 400, latin range | labels, times, numbers |

Sizes (from Jam tokens):
- Jost, three sizes:
  - headline `--fs-h2` 26 px (24 on phone), weight 500
  - session names, the brand and the question being asked `--fs-h3` 19 px
  - body, keys and rows `--fs-md` 15 px
- Plex Mono: `--fs-xs` 13 px for every label, time, count and meta line. Labels are uppercase with .14em tracking.
- No text is smaller than 12 px.

## Changes per component (old card, then this design)
- **Header.** "Claude sessions · live", "Crew Board" and a pulsing dot become:
  - the brand "Crew Hub"
  - a mono line "Updated 2 s ago", which turns into "▲ Updated 3 min ago" in warn ink after 90 s
  - two keys: "+ New" and "?". No pulse.
- **Headline (new).** One sentence with one idea: "2 need you, 3 working." The words "2 need you" jump to the oldest prompt, so the stable order never has to move. Other forms: "3 working. Nothing needs you." / "All quiet." / "No sessions yet."
- **Active plans.** Each row has:
  - the title and a mono "6/7"
  - the segmented track (see below)
  - the phase word, plus "from tasks" when the plan comes from tasks

  Finished plans read "Done 14:25" and stay for 10 min. More than 4 plans shows "+N more". Tapping a row jumps to its session.
- **Session panel.** One panel per session:
  - **Head:** the name, and the state as colour, shape and word together:
    - Working: muted word (all fine looks like nothing)
    - Needs you: ink pill, plus a 3 px ink edge on the panel
    - Offline: red dot, alert word and a 3 px alert edge
  - **Summary:** at most 2 lines.
  - **Meta line:** model, tokens and cost, then "71% context" over a 4 px bar. At 85% or more it becomes "▲ 91% context" in warn colour. The machine name shows only when the active sessions come from more than one machine.
- **The ask (Allow / Deny / answer).** An inset box shows:
  - the tool or header as a label, plus "waiting 3 min"
  - the request at 19 px
  - Allow, Deny and "Add a note". The note field opens only on that tap.

  Question options are neutral keys. A single choice sends on tap; multi-select gets an Answer key. After sending, the past form shows in place: "Allowed 14:30", "Answered 14:30 · Kitchen Pi". "Or answer in claude.ai" moved to "?".
- **Agent cards.** These become the agent thread (see below). A running agent shows its name, elapsed time, the current step, and "23 steps · 48k tok · careful". Tapping it shows the trail of 3 with times. Agents that ended in the last 30 min stay in place as one dim line ("Done 14:08 · …", "Failed 14:01 · …", "Stopped …"). Older ones sit behind "3 finished" (folded). "Clear finished" leaves "Cleared 14:30" in place.
- **Queue.** One row: "NEXT Battery report every 15 min +1". It opens the full task list (Done / Running / Queued / Failed plus tier). With nothing queued, the row reads "TASKS".
- **Message box.** Moved behind a quiet "Message" key. The box is a textarea and a neutral Send key. The flat Urgent tick box ("stops the current reply") shows only once there is text.
- **Interrupt / Stop.** Moved into "More", next to local name, effort, cache %, turns, start time and last report. Both are two-tap: the armed key turns dark ink and reads "Tap again to stop" for 4 s. They are never red.
- **Idle section.** A folded "IDLE 7" row (44 px). Each row has:
  - the name
  - a mono line ("nas · 1 h ago", "Done 13:51 · ● mac offline")
  - one summary line
  - an × (44 px) that leaves "Hidden 14:30 · Undo"

  Tapping a row shows Message and More.
- **New session.** Opens from "+ New". Fields: Machine (offline machines are greyed out), Folder, Model, Permissions (Ask first / Accept edits / Auto / Plan only), Name and Prompt. Start is neutral and two-tap, and its guard message is "Name and prompt are needed".
- **Layout.**
  - Phone: one column in this order: New, Plans, sessions, Idle.
  - Card width of 960 px or more: sessions on the left (1.7 fr), and New, Plans and Idle on the right.
  - Width is capped at 1344 px.

## Two signature shapes of Crew Hub's own
1. **The segmented track.** One segment per plan task:
   - done = ink
   - running = `--ok`
   - queued = kline outline
   - failed = alert

   The plan reads at a glance without words. Above 24 tasks it becomes a plain bar.
2. **The thread.** A hairline spine down each session's agents, with time going down:
   - running = flat `--ok` live dot (the only live dot on screen)
   - done = small dim dot
   - failed = alert diamond
   - stopped = hollow ring

## Density cuts (what moved behind a tap)
- These moved behind a tap:
  - agent trails
  - older finished agents
  - the full task list
  - the message box
  - Interrupt and Stop
  - local name, effort, cache %, turns and start time (into More)
  - the note on Allow and Deny
  - idle rows
  - the New session form
  - help text
- The machine name is hidden when there is only one machine.
- Each value is said once. For example, the plan count lives only in Plans, and cost only in the meta line.
- **Result on the same fixture:** a phone view 2,776 px tall with 318 words, against the old card's 4,988 px screenshot (`card/screenshots/card-v050-390.png`). With everything opened it is 4,376 px.

## Where Crew Hub's real data disagrees with the jam-design drafts
- **One orange per screen.** Real data often has several open prompts. The rule I chose: only the oldest answerable prompt gets the orange key, and the rest stay neutral. A single-choice question gets no orange at all, because its options are the answer.
- **The drafts assume a few calm panels.** Crew Hub has 5 active sessions plus 7 idle, and each one has controls. That is why Message and More are quiet links, not keys.
- **"Say it once" and Crew Hub's numbers.** Tokens, cost, context, steps and cache are all numbers. Only model, tokens, cost and context stay on screen.
- **Moving urgent items up.** The drafts move urgent things to the top, but 0.5.1 promised a stable order with no jumps. The headline link and the ink edge do that job instead.
- **One live dot.** The drafts imagine one live dot; here there is one per running agent (3). They are flat, with no pulse, and nothing loops.
- **Red in the data.** Red appears for failed agents and for an offline machine with an ended session (mac). Both are true alerts.

## Verification (pw-node, file://, Chromium)
- **Requests:** 3 (`mockup.html` and the 2 fonts), all file://; 0 outside requests. Console errors and warnings: 0.
- **Overflow:** none at 400 px (scrollWidth 400). No text under 12 px.
- **Touch targets:** every phone control is at least 44 px.
- **Orange:** exactly 1 element (Allow) in every run.
- **Contrast:** 20 token pairs checked in day and night, 0 below AA. Mono meta 5.7 / 6.1, text on orange 6.6 / 7.4, live dot 3.9 / 9.4, key edge 3.4 / 3.3.

## Open questions for Isaac
1. **Mode control.** Should mode come from the card config only (`mode: auto` by default, following HA), or should there be a small Day / Night / Auto control in "?"?
2. **Name.** Should it say "Crew Hub" or "Crew Board"?
3. **Interrupt.** Is it fine inside More, or should it stay visible on working background sessions?
4. **Orange.** Is orange on the oldest prompt only right? Should questions ever get orange?
5. **Stale warning.** The "▲ Updated" warning starts at 90 s. Is that the right threshold?
6. **Undo for hiding a session.** "Hidden · Undo" needs an undismiss command (`crew_hub/dismiss` has no undo today). Should we add one, or drop Undo?
7. **Font size.** Shrink `jost.ttf` (135 KB) to a latin woff2 subset of about 40 KB? Callsheet ships the ttf as it is.
8. **Easy read later.** Easy read could be added with the same `data-look` switch, using Callsheet's `easy` tokens. Do you want that later?
