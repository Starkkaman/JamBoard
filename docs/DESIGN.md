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
  id (local session_id), ccr (session_… id for Remote Control sessions), name, project (cwd basename),
  status: working | waiting | idle | ended,
  waiting?: {kind: permission | question | input, tool, text, requestId, options?},
  summary (last assistant message, first line, ≤200 chars), model, effort,
  ctx {used, max}, costUsd (estimated), startedAt, updatedAt,
  tasks: [{id, subject, status pending|in_progress|completed}],          ← TaskCreate/TaskUpdate tools
  plan?: {title, phase, done, total, finished, updatedAt,
          tasks: [{id, title, tier, status queued|running|done|failed, order, agentId, summary}]},   ← plan file
  agents: [{id, type, title, model, status running|done|failed, startedAt, endedAt,
            steps, current {text, at}, trail: [{text, at}] (last 3)}]
}]
```

The card renders exactly the Crew Board v4 layout from this: the plan strip comes from `plan` (or `tasks` when there's no plan),
session blocks from `sessions`, and agent cards from `agents`.

### Verified vs. assumed

Verified on Claude Code 2.1.295 with a real headless test session (haiku, $0.011). Redacted payloads are in `docs/evidence/`.

| Fact | Status |
|---|---|
| All hooks get `session_id` (local uuid), `cwd`, `prompt_id`; tool hooks get `tool_name`, `tool_input`, `tool_use_id`, `permission_mode`, `effort.level` | ✅ verified |
| `SubagentStart` = `{agent_id, agent_type}` only, **no description** | ✅ verified |
| `PostToolUse` Agent → `tool_response {agentId, description, resolvedModel, isAsync}`, so title and model join on agentId | ✅ verified |
| Tool calls made inside a subagent carry `agent_id` + `agent_type`, and Bash `tool_input.description` is the plain-English step | ✅ verified |
| `SubagentStop {agent_id, last_assistant_message, background_tasks[]}` | ✅ verified |
| `TaskCreated / TaskCompleted {task_id, task_subject, task_description}`; PostToolUse TaskUpdate has `statusChange {from,to}` | ✅ verified (in_progress transition only via TaskUpdate) |
| `Stop {last_assistant_message, background_tasks}`, `SessionEnd {reason}`, `SessionStart {source}` | ✅ verified |
| Remote Control sessions run as `claude --print --session-id cse_X`; `cse_X` ↔ CCR `session_X` (same suffix) | ✅ verified (ps + list_sessions) |
| `~/.claude/sessions/<pid>.json` holds `sessionId`, `name`, `status busy/idle`, `bridgeSessionId`, `messagingSocketPath`; `claude agents --json` lists live sessions | ✅ verified |
| Each session has an inbox socket `/tmp/cc-socks/<pid>.sock` with a per-session peer token (`<pid>.*.key`, 0600, same uid); the CLI documents a `{"type":"auth"}` + `{"type":"user",…}` line protocol | ✅ seen in the CLI; ❌ **injection not tested** (blocked by auto mode, needs your OK) |
| Hooks fire inside Remote Control sessions the same as in the CLI | ⚠ assumed (same binary); first test in step 1 |
| Model + context from the transcript tail (assistant `usage`); cost estimated from a price table | ⚠ assumed. Hooks carry no cost or context; CCR's `cost_usd` isn't reachable without a model |
| `PermissionRequest` hook can hold the prompt and return allow/deny decided in HA | ⚠ to test |
| `PreToolUse` on `AskUserQuestion` can be answered from HA (deny with reason = your answer) | ⚠ to test |
| `Notification` payload for "waiting for input" | ⚠ to test |
| Interrupt a running turn locally (socket message type or signal) | ❓ unknown |
| Start a new session visible in claude.ai: `claude --bg --remote-control <name> --model … ` in a folder | ⚠ to test |
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
| `send_message(session, text)` | inject via the session's inbox socket (zero tokens) | no |
| `answer(session, requestId, choice/text)` | PermissionRequest / AskUserQuestion hook waits briefly for an HA answer; else send_message | no |
| `interrupt(session)` | socket/IPC if one exists; else SIGINT to the session pid (to test) | **yes** |
| `stop(session)` | `claude stop <id>` for --bg sessions; Remote Control: to test | **yes** |
| `start(machine, folderPreset, model, prompt)` | `claude --bg --remote-control` in an allowlisted folder, model from an allowlist | **yes** |

If socket injection turns out not to work, the fallback is a "relay" Claude session using the Claude Code Remote tools
(send_message, interrupt_session, create_session). It works, but every command costs one model turn, so I'd keep it as a last resort.

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

## Decisions I need from you

1. **Socket injection test:** may I inject a test message into a session's inbox socket (first my own session, then a throwaway
   `--bg` session)? Auto mode blocked it. It's the zero-token way to deliver messages; the alternative costs a model turn per command.
2. **Hosting:** GitHub `Starkkaman/crew-hub` (HACS needs GitHub; public is simplest, private means a manual copy into HA) or Gitea?
3. **Approve the HA batch in §6** (can wait until step 4).
4. **Do any hub machines run off the LAN?** If none do, plain LAN MQTT is enough. If some do, I recommend the Tailscale app.
