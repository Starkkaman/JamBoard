# Crew Hub webhook transport (v1)

For machines that roam off the LAN (the MacBook). They reach HA through Isaac's reverse proxy at
`https://<public HA url>/api/webhook/<webhookId>`. MQTT and the broker stay LAN-only. Commands use the same signed envelopes
as docs/COMMANDS.md.

## Pairing
- In HA, Crew Hub → Configure → Add machine, transport `webhook`. Fields: `id`, `name`, `public_url` (e.g. https://hass.example.com).
- HA generates `webhookId` = `secrets.token_hex(32)` and `secret` = `secrets.token_urlsafe(32)`, stores them in the entry options,
  and shows the pairing code **once** in the next step:
  `crewhub1:` + base64url(JSON `{"u": public_url, "w": webhookId, "s": secret, "m": id}`), no padding.
  Re-pairing generates new values, and the old ones stop working.
- On the machine, Isaac runs `crew-hub-pair` in his own terminal, not through Claude. It reads the code with getpass (hidden).
  It writes config.json (`transport: "webhook"`, `webhook.url`, `machineId`) and secrets.json (`webhookId`, `commandSecret`),
  both 0600. It sends one signed `hello`, prints ok/error, and restarts crew-hubd.

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
