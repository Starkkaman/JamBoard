# Crew Hub

Home Assistant hub for live progress of every Claude Code session/agent, with commands back to sessions, shipped as a
Claude Code plugin. Brief: PROMPT.md. Design + verified/assumed table: docs/DESIGN.md. Evidence: docs/evidence/.

## Status
- 2026-10-26: design v1 (docs/DESIGN.md). Repo pushed to GitHub Starkkaman/JamBoard (deploy key ~/.ssh/github_jamboard_ed25519,
  host alias github-jamboard). Build step 1 DONE: reporter (plugins/crew-hub, 47 tests) + card preview (card/).
- MQTT verified: HA user crew-nas (non-admin, local only) logs in via auth_api; crew-hubd on the NAS publishes
  crew/nas/status + crew/nas/snapshot (retained). Config ~/.claude/crew-hub/config.json (0600, no secrets); MQTT password in secrets.json (0600; user settings deny Read/cat of it; never print it).
- Plugin NOT yet installed into the NAS's sessions (auto mode wants the user's OK). Bridge tests (docs/evidence/bridge-tests.md) in progress.
- Playwright for screenshots: `pw-node script.js` (~/.local/opt/playwright + chromium-libs, userland).

## People
- Jamfin session = local name `project-ac` (CCR session_01BPprSFdhBQg3taJLueQEUy; PROMPT.md says "project-4c", wrong).
  Agreed: plan file ~/.claude/crew-hub/plans/<local session_id>.json, whole-file rewrites via Write/Edit, fields in DESIGN.md §3.
- Home Assistant Mr Manager = local name `project-2c` (CCR session_012zoHCHE3a15umwYuRC4XwL). Owns all HA changes.
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
