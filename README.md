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

## Development

- Card source: `card/crew-hub-card.js`. After editing, run `scripts/sync-card.sh` (copies it into
  `custom_components/crew_hub/www/`; a test fails if the copies differ). Previews: `card/preview.html`,
  `card/preview-hass.html` (fake hass feed).
- Stdlib tests: `python3 -m unittest discover -s tests`.
- Home Assistant tests: `/home/node/.local/opt/ha-test-venv/bin/python -m pytest tests/ha`
  (venv with `pytest-homeassistant-custom-component`, Python 3.14).
