"""Signed commands from Home Assistant to a machine's crew-hubd (docs/COMMANDS.md, the contract).

Flow: validate args against COMMANDS.md and the machine's latest snapshot -> envelope {v, id, ts, action, args} ->
HMAC-SHA256 over the canonical JSON with the machine's command secret -> deliver -> wait up to ACK_TIMEOUT for
{"id", "ok", "error"?, "result"?}.
Delivery depends on the machine's transport: MQTT machines get a publish on <prefix>/<machine>/cmd (QoS 1, never
retained) and ack on <prefix>/<machine>/ack; webhook machines (docs/WEBHOOK.md) get the envelope queued in
webhook_transport.WebhookHub for their next poll and ack in a webhook "ack" request. Both acks end in resolve_ack().

Callers: the admin-only websocket command crew_hub/command and the crew_hub.* services (admin users only).
Never logged: the secret, the signature, message text, prompts, answers. Logged: action, machine, id, outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_MACHINE_ID,
    CONF_MACHINE_SECRET,
    CONF_MACHINE_TRANSPORT,
    CONF_MACHINES,
    DATA_PENDING,
    DATA_STORE,
    DATA_WEBHOOK,
    DOMAIN,
    TRANSPORT_WEBHOOK,
)

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
ACK_TIMEOUT = 15.0  # seconds
MAX_TEXT = 4000
MAX_PROMPT = 4000
MAX_NAME = 60
MAX_MESSAGE = 500
MAX_ID = 128  # session ids, request ids, preset keys, model and mode names
MAX_ANSWERS = 16
MAX_ANSWER_KEY = 1000
MAX_ANSWER_VALUE = 1000
MAX_ACK_PAYLOAD = 64 * 1024

ACTIONS = ("send", "answer", "interrupt", "stop", "start")
SESSION_ACTIONS = ("send", "answer", "interrupt", "stop")
DECISIONS = ("allow", "deny")


class CommandError(Exception):
    """A command refused before publishing; .code is the short error string returned to the caller."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


# ---- signing ----
def canonical(envelope: dict) -> bytes:
    """COMMANDS.md canonical form: the five signed keys, sorted, compact separators, UTF-8."""
    body = {k: envelope[k] for k in ("v", "id", "ts", "action", "args")}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(secret: str, envelope: dict) -> str:
    return hmac.new(secret.encode("utf-8"), canonical(envelope), hashlib.sha256).hexdigest()


def build_envelope(secret: str, action: str, args: dict, *, cmd_id: str | None = None, ts: int | None = None) -> dict:
    env = {
        "v": PROTOCOL_VERSION,
        "id": cmd_id or str(uuid.uuid4()),
        "ts": ts if ts is not None else int(time.time() * 1000),
        "action": action,
        "args": args,
    }
    env["sig"] = sign(secret, env)
    return env


# ---- validation ----
def _str(args: dict, key: str, max_len: int, *, required: bool = True, allow_empty: bool = False) -> str | None:
    if key not in args or args[key] is None:
        if required:
            raise CommandError("invalid_args", f"{key} is required")
        return None
    val = args[key]
    if not isinstance(val, str):
        raise CommandError("invalid_args", f"{key} must be a string")
    if not allow_empty and not val.strip():
        raise CommandError("invalid_args", f"{key} is empty")
    if len(val) > max_len:
        raise CommandError("invalid_args", f"{key} is longer than {max_len} characters")
    return val


def _bool(args: dict, key: str, default: bool = False) -> bool:
    val = args.get(key, default)
    if not isinstance(val, bool):
        raise CommandError("invalid_args", f"{key} must be true or false")
    return val


def _only(args: dict, allowed: tuple[str, ...]) -> None:
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise CommandError("invalid_args", "unknown argument(s): " + ", ".join(extra[:5]))


def _commands(snapshot: dict | None) -> dict:
    cmds = (snapshot or {}).get("machine", {}).get("commands") if isinstance(snapshot, dict) else None
    if not isinstance(cmds, dict) or cmds.get("enabled") is not True:
        raise CommandError("commands_disabled", "commands are not enabled on this machine")
    return cmds


def _session(snapshot: dict, session_id: str, action: str) -> dict:
    for s in snapshot.get("sessions") or []:
        if isinstance(s, dict) and s.get("id") == session_id:
            controls = s.get("controls")
            if not isinstance(controls, list) or action not in controls:
                raise CommandError("not_allowed", f"{action} is not available for this session")
            return s
    raise CommandError("unknown_session", "session not found on this machine")


def _allowed_values(cmds: dict, key: str, field: str = "") -> list[str]:
    out = []
    for item in cmds.get(key) or []:
        if field and isinstance(item, dict) and isinstance(item.get(field), str):
            out.append(item[field])
        elif not field and isinstance(item, str):
            out.append(item)
    return out


def validate(action: str, args: Any, snapshot: dict | None) -> dict:
    """Return a clean copy of args for the envelope, or raise CommandError."""
    if action not in ACTIONS:
        raise CommandError("unknown_action", "unknown action")
    if not isinstance(args, dict):
        raise CommandError("invalid_args", "args must be an object")
    cmds = _commands(snapshot)
    clean: dict[str, Any] = {}
    if action in SESSION_ACTIONS:
        clean["session"] = _str(args, "session", MAX_ID)
        _session(snapshot or {}, clean["session"], action)
    if action == "send":
        _only(args, ("session", "text", "urgent"))
        clean["text"] = _str(args, "text", MAX_TEXT)
        clean["urgent"] = _bool(args, "urgent")
    elif action == "answer":
        _only(args, ("session", "requestId", "decision", "message", "answers"))
        clean["requestId"] = _str(args, "requestId", MAX_ID)
        decision = args.get("decision")
        if decision not in DECISIONS:
            raise CommandError("invalid_args", "decision must be allow or deny")
        clean["decision"] = decision
        message = _str(args, "message", MAX_MESSAGE, required=False, allow_empty=True)
        if message:
            clean["message"] = message
        answers = args.get("answers")
        if answers is not None:
            if not isinstance(answers, dict) or len(answers) > MAX_ANSWERS:
                raise CommandError("invalid_args", "answers must be an object of question text to label")
            for k, v in answers.items():
                if not isinstance(k, str) or not isinstance(v, str) or not k or len(k) > MAX_ANSWER_KEY or len(v) > MAX_ANSWER_VALUE:
                    raise CommandError("invalid_args", "answers must map question text to a label")
            if answers:
                clean["answers"] = dict(answers)
    elif action in ("interrupt", "stop"):
        _only(args, ("session",))
    elif action == "start":
        _only(args, ("folder", "model", "permissionMode", "name", "prompt"))
        folder = _str(args, "folder", MAX_ID)
        if folder not in _allowed_values(cmds, "folders", "key"):
            raise CommandError("invalid_args", "unknown folder")
        model = _str(args, "model", MAX_ID)
        if model not in _allowed_values(cmds, "models"):
            raise CommandError("invalid_args", "model not allowed")
        mode = _str(args, "permissionMode", MAX_ID)
        if mode not in _allowed_values(cmds, "permissionModes"):
            raise CommandError("invalid_args", "permission mode not allowed")
        clean.update(folder=folder, model=model, permissionMode=mode,
                     name=_str(args, "name", MAX_NAME).strip(), prompt=_str(args, "prompt", MAX_PROMPT))
    return clean


# ---- ack routing ----
def _pending(hass: HomeAssistant) -> dict[str, tuple[str, asyncio.Future]]:
    return hass.data.setdefault(DATA_PENDING, {})


@callback
def handle_ack(hass: HomeAssistant, prefix: str, topic: str, payload: Any) -> None:
    """Resolve the pending command whose id matches an ack on <prefix>/<machine>/ack. Anything else is ignored."""
    head = prefix + "/"
    if not isinstance(topic, str) or not topic.startswith(head):
        return
    parts = topic[len(head):].split("/")
    if len(parts) != 2 or parts[1] != "ack":
        return
    raw = payload.encode("utf-8", "replace") if isinstance(payload, str) else bytes(payload or b"")
    if len(raw) > MAX_ACK_PAYLOAD:
        return
    try:
        ack = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return
    resolve_ack(hass, parts[0], ack)


@callback
def resolve_ack(hass: HomeAssistant, machine: str, ack: Any) -> bool:
    """Complete the pending command ack["id"] when it was sent to this machine. True when one was completed."""
    if not isinstance(ack, dict) or not isinstance(ack.get("id"), str):
        return False
    pending = _pending(hass).get(ack["id"])
    if pending is None or pending[0] != machine or pending[1].done():
        return False
    pending[1].set_result(ack)
    return True


def _ack_result(ack: dict) -> dict:
    out: dict[str, Any] = {"ok": ack.get("ok") is True}
    if not out["ok"]:
        out["error"] = str(ack.get("error") or "failed")[:200]
    if isinstance(ack.get("result"), dict):
        out["result"] = ack["result"]
    return out


# ---- send ----
def _machine_config(hass: HomeAssistant, machine: str) -> dict | None:
    """The machine's options entry when it has a command secret."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        for m in entry.options.get(CONF_MACHINES, []):
            if isinstance(m, dict) and m.get(CONF_MACHINE_ID) == machine and m.get(CONF_MACHINE_SECRET):
                return m
    return None


async def async_send_command(hass: HomeAssistant, machine: Any, action: Any, args: Any,
                             timeout: float | None = None) -> dict:
    """Validate, sign, publish and wait for the ack. Returns {ok, error?, result?}; never raises for command errors."""
    store = hass.data.get(DATA_STORE)
    if store is None or not store.active:
        return {"ok": False, "error": "not_loaded"}
    machine = str(machine) if isinstance(machine, str) else ""
    config = _machine_config(hass, machine) if machine else None
    secret = str(config[CONF_MACHINE_SECRET]) if config else None
    if not secret or machine not in store.configured:
        _LOGGER.info("Crew Hub command %s for %s refused: not_configured", action, machine or "?")
        return {"ok": False, "error": "not_configured"}
    view = store.machine(machine) or {}
    try:
        if view.get("status") == "offline":
            raise CommandError("offline", "machine is offline")
        clean = validate(str(action), args, view.get("snapshot"))
    except CommandError as err:
        _LOGGER.info("Crew Hub command %s for %s refused: %s", action, machine, err.code)
        return {"ok": False, "error": err.code, **({"detail": err.detail} if err.detail else {})}

    env = build_envelope(secret, str(action), clean)
    cmd_id = env["id"]
    fut: asyncio.Future = hass.loop.create_future()
    pending = _pending(hass)
    pending[cmd_id] = (machine, fut)
    try:
        if config.get(CONF_MACHINE_TRANSPORT) == TRANSPORT_WEBHOOK:
            hub = hass.data.get(DATA_WEBHOOK)
            if hub is None:
                return {"ok": False, "error": "not_loaded"}
            if not hub.enqueue(machine, env):
                _LOGGER.info("Crew Hub command %s for %s id %s: queue_full", action, machine, cmd_id)
                return {"ok": False, "error": "queue_full"}
        else:
            try:
                await mqtt.async_publish(hass, f"{store.prefix}/{machine}/cmd",
                                         json.dumps(env, separators=(",", ":"), ensure_ascii=False), qos=1,
                                         retain=False)
            except Exception as err:  # noqa: BLE001 - report, don't raise into the websocket/service
                _LOGGER.warning("Crew Hub command %s for %s id %s: publish failed (%s)", action, machine, cmd_id,
                                type(err).__name__)
                return {"ok": False, "error": "publish_failed"}
        try:
            ack = await asyncio.wait_for(asyncio.shield(fut), ACK_TIMEOUT if timeout is None else timeout)
        except TimeoutError:
            _LOGGER.info("Crew Hub command %s for %s id %s: no_ack", action, machine, cmd_id)
            return {"ok": False, "error": "no_ack"}
    finally:
        pending.pop(cmd_id, None)
        if not fut.done():
            fut.cancel()
    out = _ack_result(ack)
    _LOGGER.info("Crew Hub command %s for %s id %s: %s", action, machine, cmd_id,
                 "ok" if out["ok"] else out["error"])
    return out
