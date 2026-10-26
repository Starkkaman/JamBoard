# Crew Hub webhook transport (v1)

For machines that roam off the LAN (the MacBook). They reach HA through Isaac's reverse proxy at
`https://<public HA url>/api/webhook/<webhookId>`. MQTT and the broker stay LAN-only. Commands use the same signed envelopes
as docs/COMMANDS.md.

## Pairing
- In HA, Crew Hub → Configure → Add machine, transport `webhook`. Fields: `id`, `name`, `public_url` (e.g. https://hass.example.com).
- HA generates `webhookId` = `secrets.token_hex(32)` and `secret` = `secrets.token_urlsafe(32)` and shows the pairing code
  **once** in the next step. Re-pairing generates new values, and the old ones stop working.
- Storage (HA 0.3.1+): both live in a private HA Store, `.storage/crew_hub.secrets` (version 1, written 0600):
  `{"<machine id>": {"secret": "…", "webhookId": "…"}}` (`webhookId` for webhook machines only). The config entry options
  keep only `id`, `name`, `transport`, `public_url` and `"has_secret": true`, because config entries can be read through
  HA's APIs. Entries from 0.3.0 (secret and `webhook_id` in the options) are migrated on setup.
- Pairing code: `crewhub1:` + base64url(JSON), no padding. The JSON's `t` says what it is for:
  - `{"t": "webhook", "u": public_url, "w": webhookId, "s": secret, "m": id}`: a webhook machine.
  - `{"t": "mqtt", "m": id, "s": secret}`: a new command secret for an MQTT machine (add machine with transport `mqtt`, or
    re-pair one). HA 0.3.1+.
  - No `t` (codes from HA 0.3.0): `webhook`. An unknown `t` is refused.
- On the machine, Isaac runs `crew-hub-pair` in his own terminal, not through Claude. It reads the code with getpass (hidden).
  - Webhook code: writes config.json (`transport: "webhook"`, `webhook.url`, `machineId`) and secrets.json (`webhookId`,
    `commandSecret`), both 0600. It sends one signed `hello`, prints ok/error, and restarts crew-hubd.
  - MQTT code: the machine must already use MQTT (config.json has an `mqtt` broker and no `transport: "webhook"`) with
    `machineId` = `m`; otherwise it explains what to do and writes nothing. It replaces only secrets.json `commandSecret`
    (MQTT login and every other key kept), prints `new command secret installed for <m>` (never the secret), and restarts
    crew-hubd.

## Requests (machine → HA)
`POST <u>/api/webhook/<w>`, `Content-Type: application/json`, body ≤ 256 KB:
```
{"v": 1, "m": "<machine id>", "ts": <epoch ms>, "nonce": "<uuid4>", "type": "hello"|"snapshot"|"poll"|"ack", "data": {...}, "sig": "<hex>"}
```
`sig` = HMAC-SHA256(secret, canonical). The canonical form is
`json.dumps({"v","m","ts","nonce","type","data"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`.

HA rejects (HTTP 401, empty body, logged at debug):
- `|now - ts| > 120 s`;
- a nonce seen in the last 10 min;
- a bad signature;
- an `m` that doesn't match the machine the webhook belongs to;
- a bad shape.

| type | data | HA response (200 JSON) |
|---|---|---|
| `hello` | `{version}` | `{"ok": true}` |
| `snapshot` | the full snapshot (DESIGN.md §2) | `{"ok": true}` |
| `poll` | `{}` | Held **up to 25 s** until commands are queued for this machine, then `{"commands": [<COMMANDS.md envelope>, …]}` (or `[]` on timeout). |
| `ack` | the COMMANDS.md ack `{id, ok, error?, result?}` | `{"ok": true}` |

- Online status: online while a valid request arrived in the last 90 s (the poll loop keeps it fresh), otherwise offline.
  Machine status is exposed to the card the same way as MQTT `status`.
- Commands for a webhook machine are queued in HA (max 20; dropped after 60 s if never polled). They are delivered at most once,
  and the machine verifies them exactly as over MQTT (signature, 60 s window, replay).
- The machine posts a snapshot on change (≤ 1/s) and at least every 60 s. It runs one poll at a time, back to back. On network
  errors it uses exponential backoff up to 60 s and keeps only the latest snapshot (that's the offline buffer).
- TLS: normal certificate verification (stdlib `ssl.create_default_context()`). Never disable verification.
