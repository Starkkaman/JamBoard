---
name: crew-hub-setup
description: Set up this machine for the Home Assistant Crew Hub (pair it over MQTT on the LAN or the HTTPS webhook when it roams). The person runs the pairing tool in their own terminal; Claude never sees the code or any secret. Invoke with /crew-hub-setup.
disable-model-invocation: true
---

# Crew Hub setup

Help the person connect this machine to their Home Assistant Crew Hub.

**Hard rule: never ask for, read, print, paste or handle a secret.** That covers the pairing code, MQTT passwords,
command secrets and webhook ids. Never read `~/.claude/crew-hub/secrets.json` or pass a code in a command. If the
person pastes one into the chat anyway, don't use it; tell them to make a new one in Home Assistant (the old one is
exposed).

## Steps

1. **Plugin root.** This skill's base directory is `<plugin root>/skills/crew-hub-setup`, so the plugin root is two
   levels up. The tool is `<plugin root>/bin/crew-hub-pair`. Work out the absolute path.
2. **Python.** Run `python3 --version`. It needs 3.9 or newer (macOS: the system `/usr/bin/python3` from the Command
   Line Tools is fine; `xcode-select --install` if it's missing). Nothing to pip install.
3. **Pick the transport.** Ask which applies:
   - **On the home LAN all the time** (a desktop, a server) → MQTT. The person needs the broker host and port, an
     MQTT user and password, the machine id they added in Crew Hub, and its command secret.
   - **Roams off the LAN** (a laptop) → webhook. In Home Assistant: Settings → Devices & services → Crew Hub →
     Configure → Add machine, transport **webhook**. HA shows a pairing code once (it starts with `crewhub1:`).
4. **Hand over.** Tell them to run it **themselves, in their own Terminal window (not through Claude)**, with the
   absolute path from step 1 in quotes:
   - webhook: `"<plugin root>/bin/crew-hub-pair"` (paste the code at the hidden prompt; optional `--name "MacBook"`)
   - MQTT: `"<plugin root>/bin/crew-hub-pair" --mqtt`

   It writes `~/.claude/crew-hub/config.json` and `secrets.json` (0600), says "paired as <id> with <host>" (webhook),
   and restarts crew-hubd. Wait until they say it's done.
5. **Verify** (no secrets involved):
   - `tail -n 20 ~/.claude/crew-hub/crew-hubd.log`: look for `webhook connected to <host> as machine <id>` or
     `mqtt connected to <host>:<port> as machine <id>`. Failures say `failed (HTTP 401 …)`, `unknown webhook` (make a
     new code and pair again), `TLS certificate not trusted`, or a connection error.
   - `python3 -c 'import json,os; s=json.load(open(os.path.expanduser("~/.claude/crew-hub/snapshot.json"))); print(s["machine"]["id"], len(s["sessions"]), "sessions", "commands" in s["machine"])'`
   - The machine should show online on the Crew Hub card.
6. **Plan-file rule.** Successful pairing already added it to the user-level `~/.claude/CLAUDE.md` (the tool said
   `plan-file rule added to …` or `… is up to date`). If it didn't, or pairing was done with an older plugin, run it
   yourself (no prompts, no secrets, safe to run through Claude):
   `"<plugin root>/bin/crew-hub-pair" --install-plan-rule`. It is idempotent: it keeps everything in that file and only
   adds or replaces the block between `<!-- crew-hub:plan-file -->` and `<!-- /crew-hub:plan-file -->`. Check with
   `grep -c 'crew-hub:plan-file' ~/.claude/CLAUDE.md` (prints 2: the start and end markers). Don't edit the block by
   hand; it is rewritten on the next install.
7. **Folder presets (optional).** To start sessions from HA, add presets to `config.json` → `commands.folders`, e.g.
   `"folders": {"projects": {"path": "/Users/me/projects", "label": "Projects"}}`. The folder must already be trusted
   in Claude Code. You may edit `config.json` (it holds no secrets); then restart crew-hubd:
   `kill "$(cat ~/.claude/crew-hub/crew-hubd.pid)"; sleep 1; nohup "<plugin root>/bin/crew-hubd" >/dev/null 2>&1 &`
