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
4. Crew Hub → Configure → **Add a machine** for each machine: its `machineId` (for example `nas`), a display name and
   its command secret (at least 16 characters; kept in Home Assistant's config entry, never shown again).
   Machines that publish but aren't added still show on the card, marked "unconfigured".
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

## Commands

Admin users can act on sessions from the card. Each command is signed with the machine's command secret
(HMAC-SHA256, docs/COMMANDS.md), published once on `<prefix>/<machine>/cmd` (QoS 1, never retained), and the card shows
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

- `~/.claude/crew-hub/secrets.json` (mode 0600): `"commandSecret": "<the same secret you typed into Crew Hub>"`
  next to `mqttPassword`. Without it crew-hubd drops every command.
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
  `pw-node card/verify-controls.js` (writes `card/screenshots/card-controls-*.png`).
- Stdlib tests: `python3 -m unittest discover -s tests`.
- Home Assistant tests: `/home/node/.local/opt/ha-test-venv/bin/python -m pytest tests/ha`
  (venv with `pytest-homeassistant-custom-component`, Python 3.14).
