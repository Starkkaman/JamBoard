# Crew Hub

Home Assistant hub for live progress of every Claude Code session/agent, with commands back to sessions, shipped as a
Claude Code plugin. Brief: PROMPT.md. Design + verified/assumed table: docs/DESIGN.md. Evidence: docs/evidence/.

## Status
- 2026-10-26: design proposal v1 written; waiting on the user for: socket-injection test permission, repo hosting
  (GitHub vs Gitea), and later the HA batch (DESIGN.md §6). Nothing built, no HA changes.

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
