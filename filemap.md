# File map

- `CLAUDE.md` — project context, people, HA facts, rules / update on every status change.
- `PROMPT.md` — original brief from the user / read-only reference.
- `docs/DESIGN.md` — architecture, snapshot data model, verified/assumed table, security, build steps / touch when the design or a verified fact changes.
- `docs/evidence/hooks-2.1.295-sample.jsonl` — real (pre-redacted) hook stdin payloads from Claude Code 2.1.295, one per line / add new captures when hook shapes are verified; tests replay it.
- `reference/` — reference material from other sessions / read-only.
- `.claude-plugin/marketplace.json` — marketplace "jamboard" listing the crew-hub plugin / touch when the plugin version or list changes.
- `plugins/crew-hub/.claude-plugin/plugin.json` — plugin manifest (name, version) / bump the version together with `lib/crewhub/__init__.py`.
- `plugins/crew-hub/hooks/hooks.json` — all 16 hook events → `bin/crew-hook`, timeout 5 / touch when adding or dropping an event.
- `plugins/crew-hub/bin/crew-hook` — hook entry point; thin wrapper over `lib/crewhub/hook.py` / rarely.
- `plugins/crew-hub/bin/crew-hubd` — daemon entry point; thin wrapper over `lib/crewhub/daemon.py` (`--once` runs one pass) / rarely.
- `plugins/crew-hub/lib/crewhub/__init__.py` — package marker + VERSION / bump with plugin.json.
- `plugins/crew-hub/lib/crewhub/paths.py` — every ~/.claude/crew-hub path (resolved from HOME at call time) / touch when adding a file.
- `plugins/crew-hub/lib/crewhub/redact.py` — the allowlist: hook payload → one spool record / touch when a new safe field is needed; add a leak test first.
- `plugins/crew-hub/lib/crewhub/hook.py` — crew-hook body: claude pid, O_APPEND spool write, daemon liveness + detached spawn / touch for hook behaviour; keep imports minimal (<50 ms).
- `plugins/crew-hub/lib/crewhub/state.py` — Model: folds records into the DESIGN.md §2 snapshot, enrichment (sessions/*.json, transcript tail, plan files), pruning, 40 KB cap / touch for snapshot fields or status rules.
- `plugins/crew-hub/lib/crewhub/spool.py` — spool tailing from a persisted (inode, offset) and safe rename-based rotation at 5 MB / touch only with the rotation race comment in mind.
- `plugins/crew-hub/lib/crewhub/mqtt.py` — minimal MQTT 3.1.1 client (CONNECT/LWT, PUBLISH QoS0/1, SUBSCRIBE, PING) / touch for protocol features.
- `plugins/crew-hub/lib/crewhub/config.py` — config.json loading (machine id, prefix, mqtt, contextMax), topic names, ctx max rules / touch for new settings.
- `plugins/crew-hub/lib/crewhub/daemon.py` — crew-hubd main loop, MQTT link with backoff, snapshot/state writes, pidfile lock, signals / touch for publish cadence or command handling (step 5).
- `card/crew-hub-card.js` — Lovelace card rendering machine snapshots in the Crew Board v4 look (no deps, textContent only) / touch for card UI. (entry restored by the reporter worker; owner: card step)
- `card/preview.html` — standalone preview page loading the card / touch with the card.
- `card/fixtures/snapshot-sample.json` — recorded snapshot fed to the preview / refresh when snapshot fields change.
- `card/screenshots/` — preview screenshots (390/1280 px) and the v4 reference / regenerate after visual changes.
- `tests/_util.py` — paths, sample loader, in-process fake MQTT broker / touch when tests need new fixtures.
- `tests/test_redact.py` — no-leak and safe-field tests over the evidence file + synthetic payloads.
- `tests/test_state.py` — fold/enrich/plan/cap tests (sample replay → expected snapshot).
- `tests/test_mqtt.py` — packet encoding, loopback client, MqttLink online/LWT/cmd logging/backoff.
- `tests/test_hook_e2e.py` — runs the real crew-hook with a temp HOME (silence, exit 0, 0600, daemon spawn only on SessionStart).
- `tests/test_daemon.py` — runs the real crew-hubd with a temp HOME (file-only, restart offset, fake-broker MQTT) + spool rotation.

Run tests: `python3 -m unittest discover -s tests` (stdlib only, ~6 s).
