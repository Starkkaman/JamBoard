# Crew Hub command protocol (v1)

Shared contract between the HA integration (signs and publishes) and crew-hubd (verifies and executes). See DESIGN.md §4–§5.

## Transport
- HA publishes to `crew/<machine>/cmd`, QoS 1, **never retained**.
- crew-hubd answers on `crew/<machine>/ack`, QoS 1, not retained: `{"id", "ok": bool, "error"?: str, "result"?: {}}`.

## Envelope
```
{"v": 1, "id": "<uuid4>", "ts": <epoch ms>, "action": "<action>", "args": {...}, "sig": "<hex>"}
```
`sig` = HMAC-SHA256(machine command secret, canonical). The canonical form is
`json.dumps({"v","id","ts","action","args"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`.

crew-hubd rejects (with an ack error, except when unsigned or bad-signature, which are logged and dropped without an ack):
- a retained message;
- `|now - ts| > 60 s`;
- an `id` seen in the last 10 min;
- a bad signature;
- an unknown action, invalid args, or commands disabled;
- a session that isn't live on this machine.

## Actions (args)
| action | args | rules |
|---|---|---|
| `send` | `{session, text ≤4000, urgent: bool}` | `session` = local session id. Delivered through the inbox socket, priority `now` if urgent else `next`. Text is prefixed `[From Isaac via Crew Hub] `. |
| `answer` | `{session, requestId, decision: "allow"\|"deny", message? ≤500, answers?: {questionText: label}}` | Writes `~/.claude/crew-hub/answers/<requestId>.json` (0600) for the waiting hook. Error `not_waiting` / `already_answered`. |
| `interrupt` | `{session}` | Background (`kind` bg) sessions only: `claude attach <id>` in a pty, one ESC. |
| `stop` | `{session}` | Background sessions only: `claude stop <id>`. |
| `start` | `{folder (preset key), model, permissionMode, name ≤60, prompt ≤4000}` | `claude --bg --remote-control <name> -n <name> --permission-mode <m> --model <model> <prompt>` in the preset folder. The folder must be trusted. Result `{bgId, sessionId?}`. |

## Snapshot additions
- `machine.commands`: `{enabled: bool, folders: [{key, label}], models: [..], permissionModes: [..]}`. Present only when enabled.
- `session.kind`: `bg` | `interactive` | `print` (from the session registry).
- `session.controls`: the actions allowed for that session (e.g. `["send","answer"]`, plus `interrupt`, `stop` for bg).
- `session.waiting`: `{kind: "permission"|"question", requestId, tool, text, since, questions?: [{question, header?, multiSelect, options: [label…]}], hubCanAnswer: bool}`.
