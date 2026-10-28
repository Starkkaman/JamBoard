# Crew Hub

Home Assistant hub for live progress of every Claude Code session/agent, with commands back to sessions, shipped as a
Claude Code plugin. Brief: PROMPT.md. Design + verified/assumed table: docs/DESIGN.md. Evidence: docs/evidence/.

## Status
- 2026-10-26: design v1 (docs/DESIGN.md). Repo pushed to GitHub Starkkaman/JamBoard (deploy key ~/.ssh/github_jamboard_ed25519,
  host alias github-jamboard). Build step 1 DONE: reporter (plugins/crew-hub, 47 tests) + card preview (card/).
- MQTT verified: HA user crew-nas (non-admin, local only) logs in via auth_api; crew-hubd on the NAS publishes
  crew/nas/status + crew/nas/snapshot (retained). Config ~/.claude/crew-hub/config.json (0600, no secrets); MQTT password in secrets.json (0600; user settings deny Read/cat of it; never print it).
- Plugin crew-hub@jamboard installed (user scope, from this folder) on the NAS 2026-10-26; live edits apply to new hook runs.
- HA: installed 2026-10-26 by Mr Manager via HACS (repo Starkkaman/JamBoard), backup 7b08d458 before restart; entry "Crew Hub"
  prefix crew, no machines configured yet (step 5 adds nas + command secret); dashboard /crew-hub (admin, panel, custom:crew-hub-card).
  HACS updates: push to GitHub, then update.crew_hub_update in HA (needs restart).
- Bridge tests done (docs/evidence/bridge-tests.md). Step 5 DONE 2026-10-26: crew_hub 0.2.0 + plugin 0.2.0, commands per
  docs/COMMANDS.md; machine nas configured in HA (Isaac entered the secret himself); verified HA -> crew_hub.send_message -> session.
  NAS command secret = secrets.json commandSecret (auto mode blocks reading it; don't try). Folder presets: only ~/project (trusted).
- 0.4.0 (2026-10-26): Active plans stack, idle section, stopped agents (restart/inactive/TaskStop), Clear finished + dismiss (HA Store crew_hub.dismissed), human names, plan-rule install on pairing.
- 0.3.1: secrets + webhook ids in private HA Store (crew_hub.secrets), never in config-entry options; mqtt machines pair by code too.
- 0.3.0 (2026-10-26): webhook transport for roaming machines (docs/WEBHOOK.md) via Isaac's proxy, pairing code shown once
  in HA, `crew-hub-pair` run by the human in their own terminal (never via Claude). Interop test tests/ha/test_interop.py.
  NAS stays on MQTT. Mac does NOT need broker ACLs/Tailscale anymore (ACLs still nice-to-have).
- Crew Flow plugin BUILT 2026-10-27 (plugins/crew-flow 0.1.0, docs/CREW-FLOW.md, rules from docs/research/orchestration-efficiency.md): tracking
  via built-in task list + agent/task title joins; e2e verified (~$0.02). Installed on the NAS 2026-10-27 (user scope); main CLAUDE.md plan-file rule shortened (backup in scratchpad).
- 0.4.1 (2026-10-27): waiting = prompt open now (registry-driven); false "needs you" came from Macs' idle_prompt. Macs need the update.
- Playwright for screenshots: `pw-node script.js` (~/.local/opt/playwright + chromium-libs, userland).

## People
- Jamfin session = local name `project-ac` (CCR session_01BPprSFdhBQg3taJLueQEUy; PROMPT.md says "project-4c", wrong).
  Agreed: plan file ~/.claude/crew-hub/plans/<local session_id>.json, whole-file rewrites via Write/Edit, fields in DESIGN.md §3.
- Home Assistant Mr Manager = local name `project-1e` since the 2026-10-26 16:31 restart (was project-2c) (CCR session_012zoHCHE3a15umwYuRC4XwL). Owns all HA changes.
  Local names change on restart: check ~/.claude/sessions/*.json (`name`, `bridgeSessionId`) or ListAgents.

## HA facts (from Mr Manager, 2026-10-26)
- Mosquitto core_mosquitto 7.1.2: no ACL (every user can read and write everything), auth_api on (HA users can log in), no TLS certs (8883 refused).
- No Samba; File editor + Terminal & SSH apps. /config/www allowed. New dashboard, NOT Astrion (RosCard keys drive the remote).
- Remote access is the user's own reverse proxy (external_url unset; MQTT is LAN only). The card must use HA's websocket only.
- Never retain command topics (NerveOS lesson). LWT on <base>/status.

## Rules
- Zero-token reporting (hooks only). Never publish tool inputs/outputs, commands, prompts or agent briefs.
- No secrets in git/chat (MQTT passwords, HMAC secrets, webhook ids, ha-mcp path).
- Crew Board: Jamfin owns doc status/flow; my cards are agents/crewhub-* until the hub shows itself.
