# Crew Hub: design proposal (v1, 2026-10-26)

Status: **proposal, nothing built, no HA changes made.** Decisions needed are listed at the end.

## 1. Architecture

```
 EACH MACHINE (NAS container, Mac, Linux box)                          HOME ASSISTANT (192.168.5.166)
 ┌───────────────────────────────────────────────────────┐            ┌──────────────────────────────────────────┐
 │ Claude Code sessions (Remote Control, CLI, --bg)       │            │ Mosquitto (existing, :1883 LAN)           │
 │   │ hooks (plugin): SessionStart, UserPromptSubmit,    │            │   ▲                    │                  │
 │   │ Pre/PostToolUse, SubagentStart/Stop, TaskCreated/  │  MQTT QoS1 │   │                    ▼                  │
 │   │ Completed, PermissionRequest, Notification, Stop,  │ ─────────► │ crew_hub custom integration (Python)     │
 │   │ SessionEnd                                         │  crew/<m>/ │   - mirrors each machine's snapshot     │
 │   ▼  redact + append one line (≈30 ms, no network)     │  snapshot  │   - websocket API: crew_hub/subscribe   │
 │ ~/.claude/crew-hub/spool.jsonl  (0600, offline buffer) │  (retained)│   - admin-only services (allowlist)     │
 │   ▼                                                    │            │   - serves the card JS itself           │
 │ crew-hubd (one per machine, auto-started by the        │            │   ▼  HA websocket (works via your proxy) │
 │ SessionStart hook)                                     │            │ crew-hub-card (Lovelace, new "Crew"      │
 │   - folds events into a state model                    │ ◄───────── │ dashboard) ─ buttons ─► services          │
 │   - reads ~/.claude/sessions/*.json, transcript tails  │  crew/<m>/ │                                          │
 │   - publishes snapshot (debounced 1 s) + LWT status    │  cmd       │                                          │
 │   - executes HMAC-signed allowlisted commands          │  (HMAC,    └──────────────────────────────────────────┘
 │        ▼                                               │  never
 │   delivery into sessions (see §4)                      │  retained)
 └───────────────────────────────────────────────────────┘
```

Why this shape:
- **Zero tokens:** hooks are shell commands, so agents never spend a token on reporting, and the card needs no model calls.
- **Live within ~1–2 s:** hook → spool → MQTT → integration → websocket push. No polling anywhere in the browser.
- **Works away from home:** the card only talks to HA's own websocket, so it works through your reverse proxy and in the HA
  mobile app. MQTT never has to leave the LAN.
- **Custom integration rather than MQTT-discovered sensors.** Sensors would mean dozens of entities with large JSON attributes
  churning the recorder every second, and Lovelace cards can't call allowlisted commands safely on their own. A small
  integration holds the model in memory (nothing goes to the recorder), pushes diffs to the card, and owns the command allowlist.
  It ships with a few summary entities for automations and the Astrion remote: `sensor.crew_working`,
  `binary_sensor.crew_needs_me`, and per-machine `binary_sensor.crew_<m>_online`.
- **Offline buffering:** the spool file is the buffer. If MQTT is down, crew-hubd keeps folding state and publishes the
  latest snapshot when it reconnects. Because the snapshot is the state rather than a log, nothing has to be replayed in order.

Roaming machines (a Mac off the LAN), only if any exist; until then they buffer and catch up at home. Options, ranked with Mr Manager:
1. **Tailscale app in HA** (recommended if you roam): remote machines reach Mosquitto 1883 privately, with two-way MQTT and the
   same code path. The broker is never public. Needs Tailscale on each remote machine.
2. HTTPS through your reverse proxy to the integration: a per-machine token, snapshot POST and long-poll for commands. No new app, but it's a second transport to build.
3. Mosquitto websockets through the proxy: not before ACLs and per-user logins exist.

## 2. Data model

MQTT topics, one namespace per machine (matches nerveos/…, controlfreak/…):

| Topic | Retain | Content |
|---|---|---|
| `crew/<m>/status` | yes (LWT) | `online` / `offline` |
| `crew/<m>/snapshot` | yes | full machine state below, ≤ ~40 KB, at most 1/s |
| `crew/<m>/cmd` | **never** | `{id, ts, action, args, hmac}`; the daemon ignores retained or stale (>60 s) or replayed ids |
| `crew/<m>/ack` | no | `{id, ok, error?}` |

Snapshot (field names are final unless testing says otherwise):

```
machine: {id, name, version, updatedAt}
sessions: [{
  id (local session_id), ccr (session_… id for Remote Control sessions), name (human, see below), localName
  (registry name, e.g. project-aa), project (cwd basename),
  status: working | waiting | idle | ended,
  waiting?: {kind: permission | question | input, tool, text, requestId?, since?, questions?, hubCanAnswer?},
  summary (last assistant message, first line, ≤200 chars), model, effort,
  ctx {used, max}, costUsd (estimated), startedAt, updatedAt,
  tokens?: {in, out, cacheRead, cacheWrite, cacheHit, turns},                  ← 0.5.0, session transcript (main thread)
  tasks: [{id, subject, status pending|in_progress|completed}],          ← TaskCreate/TaskUpdate tools
  plan?: {title, phase, done, total, finished, updatedAt, source "file"|"tasks",
          tasks: [{id, title, tier, status queued|running|done|failed, order, agentId, summary}]},   ← plan file, else task list
  agents: [{id, type, title, model, status running|done|failed|stopped, startedAt, endedAt,
            stopReason ("session restarted" | "stopped by session" | "inactive" | null), steps, current {text, at}, trail: [{text, at}] (last 3),
            tokens?: {in, out, cacheRead, cacheWrite}}]                         ← 0.5.0, agent transcript
}]
```

0.5.0 plans from the task list (state.py `derive_plan`, docs/CREW-FLOW.md): without a plan file (missing or invalid),
`plan` is derived from `tasks` once there are ≥ 2 tasks or any subject has a tier prefix (TodoWrite lists never count).
Subject prefixes `[heavy|careful|medium|light] ` and `Project: ` (either order) give the tier and the project; the project
is stripped from titles only when all tasks share it, and then the title is `<Project>: <n> tasks` (else the session name,
computed without the plan). Agent ↔ task join: `#<taskId>` at the end of the agent title (Agent `description`), else an
exact case-insensitive title match (tier prefix stripped; with or without the project prefix; a subject clipped with "…"
matches as a prefix), preferring a task that is not completed and not held by a running agent; the newest joined agent is
`agentId`. Task status: completed → done; a joined agent running → running; newest joined agent failed/stopped → failed;
in_progress → running; else queued. Phase: all completed → `finished` (finished true); a joined agent running →
`delegate`; some joined → `review`; else `plan`. done = completed count, total = task count. TaskUpdate also renames
(`subject`, clipped like TaskCreated) and deletes (statusChange to deleted, or input status deleted). A sync Agent call's
description arrives only at its PostToolUse (when it ends), so only background agents join while running.

0.5.0 tokens (state.py `read_usage`): per transcript a reader state (inode, offset, totals, last 16 message ids) reads only
complete new lines, ≤ 4 MB per file and ≤ 16 MB per enrich pass (newest sessions first); a replaced or shrunk file
starts over. Usage lines repeat per content block with the same `message.id`, so a repeated id adds only what grew.
Session: main-thread (`isSidechain` false) usage of `transcript_path`; `turns` = distinct assistant messages; `cacheHit`
= cacheRead / (in + cacheRead + cacheWrite), 3 decimals. Agent: `<transcript_path minus .jsonl>/subagents/agent-<id>.jsonl`
(verified on disk, 2.1.3xx) or SubagentStop `agent_transcript_path` (spool `atp`, local only like `tp`), every 15 s while
running and until fully read after it ends. Paths and reader states are `_` keys, never published.

0.4.1 waiting rules (state.py): `waiting` means a prompt is open **right now**. The live registry entry
(`sessions/<pid>.json`) is authoritative: `waiting` shows waiting (text from the crew-hook request, else the
Notification, else `waitingFor`); `busy`/`idle`/`shell` clear it within one enrich pass (3 s, the allowance for registry
write lag), except a request crew-hook is still holding whose registry status hasn't changed since the request. A
registry status that changed after the request means the prompt was answered or denied elsewhere (a deny in claude.ai or
the terminal fires no PostToolUse, PermissionDenied or Stop). Without a live registry entry (other machines on older
versions, print sessions) any later progress event of the session or its agents ends it (PreToolUse, PostToolUse,
PostToolUseFailure, PermissionDenied, UserPromptSubmit, SubagentStart/Stop, TaskCreated/Completed, Stop, SessionEnd),
except a request crew-hook still holds; "unanswered" or a hold that ran out drops a request, requests expire after
10 min and a request-less Notification after 60 s. "unanswered" requests stay only while the registry says waiting.
`hubCanAnswer` is true only while crew-hook holds the request. `idle_prompt` ("waiting for your input") is idle, not
waiting.

0.4.0 rules (state.py): `name` = registry name when `nameSource` is user/auto, else the newest non-derived
`formerNames` name, else the plan title before ":", else the cwd basename + " (" + localName + ")" (just localName when
the cwd is `project`). Running agents become `stopped` on SessionStart (not source "compact") or when the registry entry moves to a new
process ("session restarted"), on PostToolUse TaskStop/KillShell/KillBash naming their id ("stopped by session"; the
spool keeps only `stopTask`, verified: TaskStop's task_id is the agentId, 2.1.310), or after 10 min without a step while
the session is idle/ended ("inactive"); a step revives restarted/inactive ones, SubagentStart revives any. Finished agents go 30 min after `endedAt`, at most 6 per session; untitled zero-step ones go once finished.

The card renders exactly the Crew Board v4 layout from this: the plan strip comes from `plan` (or `tasks` when there's no plan),
session blocks from `sessions`, and agent cards from `agents`.

### Verified vs. assumed

Hook rows were verified on Claude Code 2.1.295 with a real headless test session (haiku, $0.011). Bridge rows (inbox,
permission/question answers, interrupt, stop, start) were verified on **2.1.310** with throwaway `--bg --remote-control`
sessions (haiku, ≈ $0.06). Details and exact JSON are in `docs/evidence/bridge-tests.md`; redacted hook payloads are in `docs/evidence/`.

| Fact | Status |
|---|---|
| All hooks get `session_id` (local uuid), `cwd`, `prompt_id`; tool hooks get `tool_name`, `tool_input`, `tool_use_id`, `permission_mode`, `effort.level` | ✅ verified |
| `SubagentStart` = `{agent_id, agent_type}` only, **no description** | ✅ verified |
| `PostToolUse` Agent → `tool_response {agentId, description, resolvedModel, isAsync}`, so title and model join on agentId | ✅ verified |
| Tool calls made inside a subagent carry `agent_id` + `agent_type`, and Bash `tool_input.description` is the plain-English step | ✅ verified |
| `SubagentStop {agent_id, last_assistant_message, background_tasks[], agent_transcript_path}` | ✅ verified (path in the 2.1.295 sample; agent transcripts at `<session>/subagents/agent-<id>.jsonl`, seen on disk 2026-10-27) |
| `TaskCreated / TaskCompleted {task_id, task_subject, task_description}`; PostToolUse TaskUpdate has `statusChange {from,to}` | ✅ verified (in_progress transition only via TaskUpdate) |
| `Stop {last_assistant_message, background_tasks}`, `SessionEnd {reason}`, `SessionStart {source}` | ✅ verified |
| Remote Control sessions run as `claude --print --session-id cse_X`; `cse_X` ↔ CCR `session_X` (same suffix) | ✅ verified (ps + list_sessions) |
| `~/.claude/sessions/<pid>.json` holds `sessionId`, `name`, `status`, `bridgeSessionId`, `messagingSocketPath`, `procStart`; `claude agents --json` lists live sessions | ✅ verified. Status values: `busy`, `idle`, `shell` (a background shell task is running) and `waiting`. `waiting` comes with `waitingFor: "permission prompt"` or `"input needed"`. Stale files for dead or reused pids exist, so check `procStart` against `/proc/<pid>/stat` field 22. |
| Inbox socket injection (`/tmp/cc-socks/<pid>.sock`; `{"type":"auth","token"}` then `{"type":"user","priority","from","message"}`) | ✅ verified, also after a respawn. The token is in `~/.claude/sessions/<pid>.<sha256(messagingSocketPath)>.key`. The model sees the text wrapped as a message from "another Claude session", not as the user's own words, and it can't approve a pending prompt. |
| Priority `now` vs `next` while a session is busy | ✅ verified. `next` is queued and delivered at the next tool boundary inside the same turn, or as the next turn. `now` cuts model streaming at once, but a running tool finishes first. A cut turn sends no `Stop` hook. |
| Hooks fire inside Remote Control sessions the same as in the CLI | ✅ for `--bg --remote-control` sessions (2.1.310). ⚠ still assumed for `claude remote-control` children (`--print --sdk-url`). |
| Model and context from the transcript tail | ✅ verified. Context = `input + cache_creation_input + cache_read_input` of the last main-chain assistant `usage`; dedupe entries by `message.id`. **Max context** only appears in the statusLine payload (`context_window.context_window_size`) and in CCR metadata. |
| Cost | ✅ verified, with limits. Live cost comes only from the statusLine payload (`cost.total_cost_usd`; statusLine runs in `--bg` sessions). Transcript `cost-state` entries are written only at exit or respawn and leave out aborted turns. Otherwise it's an estimate from usage. |
| `PermissionRequest` hook can hold the prompt and return allow/deny decided in HA | ✅ verified (8 s hold). It races the TUI and claude.ai prompt, and the first answer wins. Set the hook `timeout` explicitly. The payload has no `tool_use_id`. |
| `AskUserQuestion` answered from HA | ✅ verified, and it works in `--bg` sessions. The clean way is PermissionRequest (or PreToolUse) `allow` with `updatedInput.answers {questionText: label}`. Deny-with-reason also works, but the model sees it as an error. |
| `Notification` payloads | ✅ `permission_prompt` ("Claude needs your permission") fires 6 s after a permission or question dialog opens. ❌ `idle_prompt` never fired in bg sessions, so use `Stop` plus the sessions-json status instead. |
| Interrupt a running turn locally | ✅ for `--bg` sessions: `claude attach <id>` in a pty, then one ESC, kills the running tool. Soft version: an inbox `now` message. SIGINT/SIGTERM **exit** the session; SIGTERM also gets respawned. ❓ Remote Control children: no local interrupt; it needs claude.ai or CCR `interrupt_session`. |
| Start a new session visible in claude.ai: `claude --bg --remote-control <ccr title> -n <local name> --permission-mode <m> --model …` | ✅ verified (from `/home/node/project`, the only trusted folder) |
| Stop a `--bg` session | ✅ `claude stop <id>` exits in under 1 s, even mid-turn, with SessionEnd `reason:"other"` and no respawn. `claude rm <id>` needs the on-demand daemon running. |
| TodoWrite (older tool) | not used by 2.1.295 by default (TaskCreate/TaskUpdate replaced it); handled anyway |

## 3. Plugin shape and new-machine setup

Repo `crew-hub` (marketplace + plugin + HA integration in one repo):

```
.claude-plugin/marketplace.json
plugins/crew-hub/
  .claude-plugin/plugin.json
  hooks/hooks.json            → all events above → bin/crew-hook (matcher "*")
  bin/crew-hook               Python 3 stdlib: redact, append to spool, ensure crew-hubd is running
  bin/crew-hubd               Python 3 stdlib: state folder, tiny built-in MQTT 3.1.1 client, command executor
  skills/crew-hub-setup/      /crew-hub-setup: writes ~/.claude/crew-hub/config.json, tests the broker, prints the HA step
custom_components/crew_hub/   HA integration (+ www/crew-hub-card.js), HACS-installable
docs/
```

No dependencies (Python 3.8+ stdlib only; macOS and Linux both have it). New machine:
1. `/plugin marketplace add <repo>` then `/plugin install crew-hub`.
2. `/crew-hub-setup` asks for the machine id, broker host, MQTT user/password and the command secret, and stores them in
   `~/.claude/crew-hub/config.json` (0600, never in git). It checks the broker, and from then on every session on that machine reports.
3. In HA, add the machine in the Crew Hub integration's options (machine id + the same command secret).

Jamfin / savvy-flow plans (agreed with the Jamfin session): the orchestrator rewrites
`~/.claude/crew-hub/plans/<local session_id>.json` = `{title, phase, done, total, finished, updatedAt, ccrSessionId?, tasks[…]}`
with Write/Edit. A PostToolUse hook on Write|Edit for that path picks it up. A missing file means no plan.
This adds no tool calls to its normal flow.

## 4. Interaction (HA → session)

Allowlisted actions only. There is no shell, no free-form command, and no path outside the configured project roots.

| Action | Mechanism (best → fallback) | Confirm in card |
|---|---|---|
| `send_message(session, text, urgent?)` | Inbox socket (zero tokens, ✅ verified). Look up by sessionId and send priority `next`. With `urgent`, send `now`: it cuts model output at once, but a running tool finishes first. The model is told it's a message from another session, so a pending prompt can't be approved this way. | no |
| `answer(session, requestId, choice/text)` | A crew `PermissionRequest` hook (matcher `*`, with an explicit `timeout` in seconds of at least the hold time) publishes the request and waits on a local answer file. It returns `decision.behavior` allow/deny (+ `message`). For AskUserQuestion it returns allow with `updatedInput.answers {questionText: label}`. It races the TUI and claude.ai prompt: whoever answers first wins, and the other side is cancelled. ✅ verified. `requestId` = our own id. The payload has no `tool_use_id`, so pair it with the preceding PreToolUse. | no |
| `interrupt(session)` | `--bg`: run `claude attach <id>` in a pty, send **one** ESC, close the client (✅ verified, kills a running tool). Fallback: a `now` message. Never SIGINT/SIGTERM: they end the session. Remote Control children: no local interrupt, so use the relay (CCR `interrupt_session`) or a `now` message (untested there). | **yes** |
| `stop(session)` | `--bg`: `claude stop <id>` (✅ verified; `claude --bg --resume <sessionId>` with no flags resumes it). Remote Control children: not offered (signals would kill the bridged process); archive from claude.ai instead. | **yes** |
| `start(machine, folderPreset, model, prompt)` | `claude --bg --remote-control "<title>" -n "<name>" --permission-mode <mode> --model <m> "<prompt>"` in an allowlisted, trusted folder (✅ verified). It returns the bg id; the CCR id appears in `sessions/<pid>.json` `bridgeSessionId` shortly after start (it was there by the first idle, ~4 s). | **yes** |
| `rename(session, name)` (optional) | Inbox control frame `{"type":"control","action":"rename","name"}` (✅ verified, local name only) | no |

Status for the card comes from `sessions/<pid>.json` with no hooks needed: `busy` / `idle` / `waiting` (+ `waitingFor`) / `shell`.
A turn cut by an interrupt or a `now` message sends no `Stop`, so a new `UserPromptSubmit` or the status going `idle` ends it.

The "relay" Claude session (Claude Code Remote tools send_message, interrupt_session, create_session) is now needed only to
interrupt Remote Control children. Every command through it costs one model turn.

## 5. Security model

- **HA is the only front door.** The card calls `crew_hub.*` services, which check the caller is an HA **admin** user. Your HA
  login (including through the reverse proxy and the mobile app) is the authentication. HA gets no new unauthenticated endpoints.
- **Commands are signed:** HMAC-SHA256 with a per-machine secret over `{id, ts, action, args}`, a 60 s window, a replay cache
  and no retain. Today every Mosquitto user can write every topic (no ACL), so the HMAC is what stops anything else on the LAN
  from commanding a machine. It doesn't depend on broker changes.
- **The daemon is the allowlist:** fixed action set, folder presets from local config, model allowlist, message length cap. It
  refuses everything else and acks the error.
- **Redaction at the source:** hooks publish only names, descriptions (Bash `description`, Agent `description`, task subjects),
  statuses, timings and token counts. Never tool inputs/outputs, commands, file contents, prompts or agent briefs. The spool is 0600.
- **Secrets** (MQTT password, command secrets, webhook ids, ha-mcp path) live only in `~/.claude/crew-hub/config.json` and HA's
  config entry. They never go in git or chat.
- **Broker exposure (no ACL today):** a crew MQTT login can publish to *every* topic, including NerveOS power presses,
  controlfreak/…, showkiosk/… and homeassistant/# discovery. The HMAC protects only crew commands. This is acceptable on the LAN
  (the existing logins are equally unrestricted), but **the ACL step must come before any crew credential goes onto a roaming machine.**
- **Be aware:** sending a message to a session in auto mode is effectively "run work on that machine". It sits behind HA admin
  plus HMAC, and the session's own permission mode still applies.

## 6. HA changes Mr Manager would make (one batch, needs your OK)

No broker restart, no change to Astrion/RosCard, and existing MQTT clients are untouched.
1. **MQTT login per machine (your hands):** in Settings → People → Users, create `crew-nas` (later `crew-mac`, …), with admin
   off and "Can only log in from local network" on. ha-mcp can't create users. Mosquitto's auth_api should accept such users
   with no app restart. **Step 1's first acceptance test is a real publish with that login** before anything depends on it.
2. **Install the `crew_hub` integration:** HACS custom repository (needs the GitHub repo), or you copy it into
   `/config/custom_components` with File editor or SSH, since Mr Manager has no /config file access. Mr Manager then restarts HA once.
3. **Add the integration** via Settings → Devices & services: MQTT prefix `crew`, machine `nas` with its command secret
   (typed into the config flow and stored in HA's .storage).
4. **New dashboard "Crew"** (sidebar, admin-only, not Astrion) with one `custom:crew-hub-card`. The integration serves the
   card from a static path and registers it as a Lovelace resource with a version query. A tiny loader loads the real bundle
   only on the Crew dashboard; `add_extra_js_url` is only a fallback, because it loads on every page. Summary entities are
   excluded from the recorder.
5. Optional: automation "`binary_sensor.crew_needs_me` → on for 5 s → `notify.mobile_app_pixel_9a`" (Isaac only).

**Batch B, the roaming Mac (Isaac: "my MacBook jumps on and off the home network all the time"). Required before the Mac gets a login:**
6. Tailscale app in HA (Mac joins the tailnet and reaches Mosquitto privately).
7. Mosquitto ACLs: turn on customize, add an acl file with full-access rules for every existing client and `crew-*` users
   limited to `crew/<own machine>/#`, then restart Mosquitto (all MQTT devices drop for a few seconds; do it at a quiet time).

Original note on the ACLs (needs a Mosquitto restart): Mosquitto's acl_file is default-deny for **all** users,
so it would need rules for Control Freak, NerveOS, the kiosks, mac2mqtt and the HA login too. HMAC covers commands meanwhile.

## 7. Build steps (each verified before the next)

1. Hook + spool + crew-hubd on the NAS, publishing snapshots to MQTT (read-only). Check them with a subscriber.
2. Verify the ⚠ rows above with a cheap test session (Remote Control hooks, transcript usage, PermissionRequest/AskUserQuestion,
   socket injection if you allow it, interrupt, `--bg --remote-control`).
3. Card as a standalone HTML page fed by a recorded snapshot, to match the Crew Board look pixel for pixel.
4. HA integration + card (after your OK on §6), read-only.
5. Commands, one at a time, with confirms.
6. Plugin packaging, marketplace repo, `/crew-hub-setup`; then the Mac.
7. Roaming machines, if any (Tailscale preferred).

## Decisions (2026-10-26)

1. Socket injection: approved by Isaac and verified (docs/evidence/bridge-tests.md).
2. Hosting: GitHub Starkkaman/JamBoard (HACS custom repository).
3. The MacBook roams: Tailscale app + broker ACLs (batch B) come before it gets a login.
4. HA batch A: approved and applied 2026-10-26 (HACS install, restart, integration, "Crew" dashboard). Machines + command
   secrets come with step 5.
5. Not rotating the crew-nas password for now (Isaac). It lives only in ~/.claude/crew-hub/secrets.json.
