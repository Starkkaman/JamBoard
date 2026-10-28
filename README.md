# Crew Hub

Live progress of every Claude Code session and agent, shown in Home Assistant. Each machine runs the `crew-hub`
Claude Code plugin (hooks + `crew-hubd`), which publishes a snapshot over MQTT. The `crew_hub` integration mirrors
those snapshots in memory and pushes them to the `custom:crew-hub-card` Lovelace card over Home Assistant's own
websocket, so the card also works in the mobile app and through a reverse proxy. Design: `docs/DESIGN.md`.

## Install

Needs Home Assistant with the MQTT integration set up, on the same broker the machines publish to.
Tested with Home Assistant 2026.10.4.

1. Install the integration, either way:
   - **HACS:** HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/Starkkaman/JamBoard`,
     category *Integration*, then install **Crew Hub**.
   - **Manual:** copy `custom_components/crew_hub` into `/config/custom_components/crew_hub`.
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **Crew Hub**. Keep the MQTT prefix `crew` unless the machines'
   `~/.claude/crew-hub/config.json` uses another `prefix`.
4. Crew Hub → Configure → **Add a machine** for each machine: its `machineId` (for example `nas`) and a display name.
   Home Assistant makes the command secret and shows it **once** as a pairing code; see
   [Pairing MQTT machines](#pairing-mqtt-machines-and-rotating-secrets).
   Machines that publish but aren't added still show on the card, marked "unconfigured".
   Machines that leave the home network use the webhook transport instead (see
   [Roaming machines (webhook)](#roaming-machines-webhook)).
5. Add a dashboard (Settings → Dashboards → Add dashboard, admin-only is fine) with one card:

   ```yaml
   type: custom:crew-hub-card
   ```

   The integration serves the card at `/crew_hub/crew-hub-card.js` and registers the Lovelace resource for it
   automatically (storage-mode dashboards: one `module` resource, its `?v=` updated on every card change and removed
   when the integration is deleted), so don't add one by hand. With YAML-mode resources it loads the card on every
   frontend page instead. The card's feed (`crew_hub/subscribe`) is admin-only; other users see
   "Crew Hub integration not available".

Entities (device "Crew Hub"): `sensor.crew_hub_working` (sessions working on online machines),
`binary_sensor.crew_hub_needs_me` (a session is waiting; attribute `sessions` lists up to 5 names, not recorded) and
`binary_sensor.crew_hub_<machine>_online` per added machine.

## Pairing MQTT machines and rotating secrets

Since 0.3.1 Home Assistant keeps every machine's secrets (the command secret, and a webhook machine's webhook id) in a
private file, `.storage/crew_hub.secrets` (mode 0600), not in the config entry. The entry's options only say
`"has_secret": true`, so nothing that reads config entries (the config-entry REST/websocket API, ha-mcp, backups of
`core.config_entries`) sees a secret. An entry from 0.3.0 or earlier is moved over by itself on the first start
after the update; the machines keep their secrets and nothing has to be paired again. Diagnostics
(Download diagnostics) never include the secrets file, and redact secrets, webhook ids, signatures and URL paths.

**Pairing an MQTT machine**

1. On the machine, set up MQTT once: `crew-hub-pair --mqtt` asks for the broker host, port, user, password and machine
   id (leave the command secret empty). Already done on machines that report to Crew Hub over MQTT.
2. Crew Hub → Configure → **Add a machine**: the `machineId`, a display name, transport **MQTT**. The next step shows a
   **pairing code** (`crewhub1:…`) **once**. The machine is saved as soon as the code shows; submit the dialog when
   done.
3. On the machine, run `crew-hub-pair` in your own terminal (not through Claude) and paste the code when asked (hidden
   input). It replaces only `commandSecret` in `secrets.json` (the MQTT login and every other setting stay), prints
   `new command secret installed for <id>` and restarts crew-hubd. It refuses (and writes nothing) when the machine
   isn't on MQTT yet, uses the webhook transport, or has another machine id than the code.

Legacy path: under **Advanced** you can still type a secret yourself (at least 16 characters) instead of getting a
code; then put the same value into the machine's `secrets.json` `commandSecret` by hand.

**Rotating a secret** (lost code, leaked secret, or routine): Crew Hub → Configure → **Edit a machine** → tick
**Re-pair (new secret)** (webhook machines: **Re-pair**). A new code is shown once, and the old secret stops working
at once: until you run `crew-hub-pair` with the new code on the machine, its commands get no answer ("No answer from
the machine"; the machine drops commands with a signature it doesn't know). Removing a machine also deletes its
secrets; deleting the integration deletes the secrets file.

## Roaming machines (webhook)

A machine that isn't always on the home network (a laptop) can't reach the MQTT broker, which stays LAN-only. It talks
to Home Assistant over HTTPS instead, through the same public address you use for the app (your reverse proxy), at
`https://<public HA url>/api/webhook/<webhook id>`. The protocol is `docs/WEBHOOK.md`.

**Pairing**

1. Crew Hub → Configure → **Add a machine**: the `machineId`, a display name, transport **Webhook**, and the public
   Home Assistant URL (`https://…`, no path, for example `https://hass.example.com`). Home Assistant generates the
   webhook id and the secret (a secret typed under **Advanced** is ignored for webhook machines).
2. The next step shows a **pairing code** (`crewhub1:…`) **once**. On the machine, run `crew-hub-pair` in your own
   terminal (not through Claude) and paste the code when asked; the input is hidden. It saves the settings, sends a
   test request and restarts crew-hubd. The machine is saved in Home Assistant as soon as the code shows, so the test
   request works while the dialog is open; submit the dialog when done.
3. The code contains the webhook address and the secret: don't paste it into a chat or a file. Lost it, or think it
   leaked? Crew Hub → Configure → **Edit a machine** → tick **Re-pair**: a new code is shown once, and the old webhook
   and secret stop working at once. Changing the public URL only reaches the machine through a re-pair.
   Removing the machine also removes its webhook.

**How it behaves**

- Every request is signed with the machine's secret (HMAC-SHA256) and checked by Home Assistant: the machine id, a
  timestamp within 2 minutes, a nonce that wasn't used in the last 10 minutes, and the signature (constant-time).
  Anything else gets an empty `401`; a body over 256 KB gets `413`. Home Assistant answers `200` for webhook ids it
  doesn't know, as it does for every webhook, so the URL alone gives nothing away.
- Snapshots feed the same card, entities and `crew_hub/subscribe` feed as MQTT machines. The machine counts as
  online while a valid request arrived in the last 90 s (its poll loop keeps that fresh), then offline, and its
  sessions stop counting.
- Commands work the same way as for MQTT machines (same signature, same controls, same 15 s wait for the answer), but
  instead of an MQTT publish they wait in Home Assistant until the machine polls: up to 20 per machine, dropped after
  60 s if nobody picks them up, each one handed out at most once. A full queue refuses new commands with
  `queue_full`. The machine holds one poll open for up to 25 s at a time.
- MQTT messages under a webhook machine's id (`<prefix>/<id>/…`) are ignored, so nobody on the LAN can pose as it.
- Reverse proxy: it must pass `POST /api/webhook/…` through, allow request bodies up to 256 KB and keep a request open
  for at least 30 s (the held poll). The webhook id is in the URL, so keep proxy access logs private.
- Existing MQTT machines are unchanged; a machine added before 0.3.0 is an MQTT machine.

**On the machine (plugin 0.3.0+)**

- Needs `python3` 3.9 or newer, standard library only (the macOS system Python is fine). In Claude Code,
  `/crew-hub-setup` walks you through it; Claude never sees the code.
- The tool is `<plugin root>/bin/crew-hub-pair` (in Claude Code's plugin folder, or `plugins/crew-hub/bin/` in this repo).
  `--name "MacBook"` sets the display name (default: the hostname). It writes `~/.claude/crew-hub/config.json`
  (`"transport": "webhook"`, `"webhook": {"url": …}`, `machineId`, `machineName`, and a `commands` block with
  `enabled: true` and no folder presets) and `secrets.json` (`webhookId`, `commandSecret`), both 0600 in a 0700
  folder, keeping every other key. Then it sends one signed `hello` and prints `paired as <id> with <host>` or the
  error (`HTTP 401`, `unknown webhook`: make a new code), and restarts crew-hubd (or starts it). The settings stay
  saved when the hello fails; crew-hubd keeps retrying.
- LAN machines can use the same tool for MQTT: `crew-hub-pair --mqtt` asks for the broker host, port, user, password,
  machine id and command secret (hidden input) and writes the two files with `"transport": "mqtt"`. A pairing code
  for an MQTT machine (`crew-hub-pair`, then paste) installs only a new `commandSecret` (plugin 0.3.1+, see
  [Pairing MQTT machines](#pairing-mqtt-machines-and-rotating-secrets)).
- crew-hubd then posts `hello` at start, the snapshot on every change (at most once a second) and at least every 60 s,
  and command acks; one poll is always open (35 s socket timeout). Commands from a poll go through the same checks as
  over MQTT (signature, 60 s window, replay, allowlists). On errors it backs off up to 60 s and keeps only the latest
  snapshot. TLS uses the system's normal certificate checks (never disabled), so the reverse proxy needs a certificate
  the machine trusts. `crew-hubd.log` names the host only, never the webhook id, path, secret or signatures:
  look for `webhook connected to <host> as machine <id>`.
- Folder presets for **New session** work as for MQTT machines (`commands.folders` in config.json, see
  [Machine side](#machine-side-crew-hubd)); restart crew-hubd after editing.

## Session names, agents and plans (plugin 0.4.0)

- **Session names** are human: the name you gave the session or Claude's auto title; else its newest earlier such
  name (`formerNames` in `~/.claude/sessions/*.json`); else the plan title before the first `:` (`NerveOS: …` →
  `NerveOS`); else the folder name with the local name, e.g. `crew-hub (crew-hub-3f)`, or just `project-aa` in
  `~/project`. The snapshot keeps the local name in `session.localName`.
- **Agents** are `running`, `done`, `failed` or `stopped`. `stopped` (with `endedAt` and `stopReason`) means the session
  restarted while the agent ran (`"session restarted"`: a SessionStart from startup, resume or clear (not compaction),
  or the session moved to a new process), the session stopped it with TaskStop (`"stopped by session"`), or the agent
  made no step for 10 minutes while its session was idle or ended (`"inactive"`). A restarted or inactive agent that
  makes a step again is running again; any agent that starts again (SubagentStart) is running again. Finished agents leave the card 30 minutes after they end, at most 6 per session stay
  (newest first), and untitled agents that never made a step are dropped once finished. Running agents are never
  dropped by count. Plan task statuses always come from the plan file.
- **Plan file rule:** after a successful pairing, `crew-hub-pair` adds the plan-file rule to the user-level
  `~/.claude/CLAUDE.md`, between `<!-- crew-hub:plan-file -->` and `<!-- /crew-hub:plan-file -->` (created if missing,
  the block replaced if present, everything else kept). `crew-hub-pair --install-plan-rule` does only that (no prompts,
  no secrets). The text is `plugins/crew-hub/templates/plan-file-rule.md`: every savvy-flow plan (and any multi-step
  plan with agents) is also written as a whole file to `~/.claude/crew-hub/plans/<local session_id>.json`.

## Plans from the task list and token insight (plugin 0.5.0)

- **Plans without a plan file** (Crew Flow, `docs/CREW-FLOW.md`): when a session has no plan file, crew-hubd builds
  `session.plan` from Claude Code's built-in task list (TaskCreate/TaskUpdate), at zero token cost. It does so only once
  the list has at least 2 tasks, or any task has a tier prefix. Subjects may start with `[heavy|careful|medium|light] `
  (the tier; any case) and `Project: ` (either order); both are stripped from the task title, the project only when every
  task has the same one. Statuses: pending → queued, in_progress → running, completed → done, deleted tasks disappear.
  An agent joins a task by an explicit `#<taskId>` at the end of its Agent `description`, else by an exact
  case-insensitive title match (tier prefix and `#id` stripped, with or without the project prefix). A joined running
  agent makes its task running; a joined agent that ended failed or stopped makes it failed unless the orchestrator
  completes it. Phase: `plan` (no joined agent yet), `delegate` (a joined agent runs), `review` (joined agents all
  finished, tasks still open), `finished` (all completed). Title: `<Project>: <n> tasks`, else the session name.
  `plan.source` is `"file"` or `"tasks"`; the plan file always wins. Sync (foreground) Agent calls report their
  description only when they finish, so they join then; background agents (the default) join at once.
- **Tokens**: `session.tokens {in, out, cacheRead, cacheWrite, cacheHit, turns}` sums the main thread's assistant usage
  in the session transcript (subagents not included; `turns` = API calls; `cacheHit` = cacheRead / (in + cacheRead +
  cacheWrite)). `agent.tokens {in, out, cacheRead, cacheWrite}` sums the agent's own transcript
  (`<session>/subagents/agent-<id>.jsonl`): every 15 s while it runs, once more when it ends (from the SubagentStop
  `agent_transcript_path`, kept local like the session transcript path). Transcripts are read incrementally from a
  remembered offset, at most 4 MB per file and 16 MB per 3 s pass, so a first look at a large history takes a few passes.

## Crew Flow (plugin crew-flow 0.1.0)

An orchestrator skill in the same marketplace, successor to savvy-flow: the session plans, owns design, delegates to
tiered workers and reviews their diffs. Rules come from `docs/research/orchestration-efficiency.md`.

- **Install**: `/plugin install crew-flow@jamboard` (needs the `jamboard` marketplace from the Install section; for a
  one-off try, `claude --plugin-dir plugins/crew-flow`). Crew Hub (`crew-hub@jamboard`) must be installed on the same
  machine for the dashboard part.
- **Use**: `/crew-flow <task>`, best with the session at high effort (`claude --effort high`, set at the start).
  Workers: `crew-heavy` (Opus xhigh, insight), `crew-careful` (high, thoroughness), `crew-medium` (medium, standard
  work), `crew-light` (low, mechanical). No Fable tier. Workers start lean: no CLAUDE.md (`omitClaudeMd`), an
  explicit tool list (Bash, Read, Edit, Write, ToolSearch, Monitor, TaskStop; no Skill, no MCP), auto-compact at 250K,
  and a short set of ground rules in place of CLAUDE.md, so every brief carries the project rules that matter.
  To give a tier an MCP server, add `mcp__<server>` to its `tools:` line.
- **On the Crew dashboard**: no plan file and no extra tokens. The orchestrator creates one built-in task per step
  (`[tier] Project: title`), launches each worker with the description `Project: title #<taskId>`, and marks tasks
  in progress / completed. crew-hubd turns that into `session.plan` with `source: "tasks"`: tiers from the prefix,
  agents joined to their tasks by `#<taskId>`, phase plan → delegate → review → finished, title `<Project>: <n> tasks`.

## The card (integration 0.5.0)

- **Active plans**: one paper strip per unfinished plan on any machine and session (newest first), plus plans that
  finished in the last 10 minutes (chip "finished", bar full). Each strip shows the title, the bar, the phase,
  "N of M accepted" and the session and machine; tapping it scrolls to that session (opening it if it is idle). More
  than 4 plans: 4 strips and a "+N more" toggle. No plans: one "No plan running" strip.
- **Sessions**: working and waiting sessions get full blocks. Each block lists its task queue under the summary
  (running tasks and the next 3 queued; done tasks behind an "N done" toggle), its running agents, and its finished
  agents (done, failed, stopped) behind an "N finished" toggle with **Clear finished**. Stopped agents show a dim
  "stopped" chip and the stop reason; the session's local name shows in the meta row when it differs from its name.
- **Tokens** (0.5.0): the session meta row shows e.g. "312k tok · 94% cached", each agent "48k tok" (all four counts
  summed). A plan derived from the task list looks like a file plan (tiers in the queue, task titles on joined agents)
  and its strip carries a small dim "from tasks" tag.
- **Idle sessions (N)**: idle and ended sessions, collapsed by default, one row each (name, machine, last activity,
  one line of summary). Tap a row to open the full block with its message box; **×** dismisses the session.
- **Dismiss is shared**: Clear finished and × call the admin-only websocket command `crew_hub/dismiss`
  `{machine, session, agents?: [ids], session_dismissed?: bool}`. Home Assistant keeps the dismissed ids in the private
  Store `.storage/crew_hub.dismissed` and leaves them out of what every device gets (`crew_hub/subscribe`,
  `crew_hub/get`); commands and diagnostics still see the full snapshot. A dismissed session is hidden only while it is
  idle or ended: when it works or waits again it comes back for good. Ids missing from the machine's snapshots for
  24 hours are forgotten.

## Commands

Admin users can act on sessions from the card. Each command is signed with the machine's command secret
(HMAC-SHA256, docs/COMMANDS.md), published once on `<prefix>/<machine>/cmd` (QoS 1, never retained; webhook
machines pick it up with their next poll instead), and the card shows
"Sending…", then "Sent ✓" or the machine's error (no answer within 15 s shows "No answer from the machine").
Controls appear only when the machine reports `commands.enabled`, is online and added in Crew Hub, and only for the
actions that session allows (`session.controls`):

- **Message box + Send:** sends text (≤ 4000 characters) to the session. **Urgent** delivers it at once and interrupts
  the current reply; otherwise it's read after the current turn.
- **Allow / Deny (+ note):** answers a waiting permission prompt. **Question options:** answer an AskUserQuestion
  (one tap for a single question; toggle + **Answer** for multi-select or several questions). The prompt in claude.ai
  or the terminal still works too; whichever answers first wins.
- **Interrupt / Stop** (background sessions only): tap twice within 4 s. Interrupt stops the current turn; Stop ends
  the session.
- **New session** (collapsed, at the top): machine, folder preset, model, permission mode, name and prompt; **Start**
  needs two taps. Only the machine's own folder presets, models and modes are offered.

The same commands exist as services `crew_hub.send_message`, `crew_hub.answer`, `crew_hub.interrupt`, `crew_hub.stop`
and `crew_hub.start_session` (plus the admin-only websocket command `crew_hub/command`). They are **admin only**, and
calls without a user (automations, scripts run by a trigger) are refused on purpose: a command can run work on a
machine, so a person must send it. Home Assistant logs only the action, machine, command id and outcome, never the
secret, the signature or the text.

### Machine side (crew-hubd)

Commands are off until both of these are set on the machine (then restart crew-hubd):

- `~/.claude/crew-hub/secrets.json` (mode 0600): `"commandSecret"` next to `mqttPassword`, written by `crew-hub-pair`
  from the pairing code (or, legacy, the same secret you typed into Crew Hub). Without it crew-hubd drops every command.
- `~/.claude/crew-hub/config.json` (no secrets in this file):

  ```json
  {
    "machineId": "nas",
    "mqtt": {"host": "192.0.2.10", "port": 1883, "username": "crew-nas"},
    "commands": {
      "enabled": true,
      "folders": {
        "projects": {"path": "/home/me/projects", "label": "Projects"}
      },
      "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
      "permissionModes": ["default", "acceptEdits", "auto", "plan"],
      "holdSeconds": 55
    }
  }
  ```

  `models`, `permissionModes` and `holdSeconds` are optional (the values above are the defaults). Each folder preset
  must be an absolute path that Claude Code already trusts **exactly** (`projects["<path>"].hasTrustDialogAccepted`
  in `~/.claude.json`, which crew-hubd only reads); start in that folder once interactively to trust it. Home
  Assistant only ever sees the preset keys and labels, never the paths.

What crew-hubd does with a command: it checks the HMAC (constant time), drops unsigned or badly signed messages
without an answer, and acks every other refusal (`retained`, `stale` (more than 60 s off), `replay`, `disabled`,
`unknown_action`, `bad_args:<field>`, `not_live`, `not_bg`, `not_waiting`, `already_answered`, `unknown_folder`,
`folder_missing`, `folder_untrusted`, `model_not_allowed`, `mode_not_allowed`, `start_failed`, …) on
`<prefix>/<machine>/ack`. Its log (`crew-hubd.log`) gets the action, command id and outcome only.

- **send:** the session's local inbox socket (zero tokens). The text arrives as `[From Isaac via Crew Hub] …`, and
  Claude Code tells the model it came from another session, so a message can't approve a pending prompt.
- **answer:** the `PermissionRequest` hook (`crew-hook`) holds each permission prompt and question for up to
  `holdSeconds` (0 = report only, never answer) while the normal prompt stays open in the terminal and claude.ai; the
  first answer wins. The hold doesn't delay those prompts.
- **interrupt / stop:** background (`--bg`) sessions only: `claude attach` + one Esc, and `claude stop`.
- **start:** `claude --bg --remote-control <name> -n <name> --permission-mode <mode> --model <model> -- <prompt>` in
  the preset folder; the ack carries the background id (and the session id once it registers).

## Development

- Card source: `card/crew-hub-card.js`. After editing, run `scripts/sync-card.sh` (copies it into
  `custom_components/crew_hub/www/`; a test fails if the copies differ). Previews: `card/preview.html`,
  `card/preview-hass.html` (fake hass feed + fake command answers). Browser check of the controls:
  `pw-node card/verify-controls.js` (writes `card/screenshots/card-v050-390.png` and `card-v050-1280.png`).
- Stdlib tests: `python3 -m unittest discover -s tests`.
- Home Assistant tests: `/home/node/.local/opt/ha-test-venv/bin/python -m pytest tests/ha`
  (venv with `pytest-homeassistant-custom-component`, Python 3.14).
