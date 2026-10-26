"""Webhook transport for machines off the LAN (docs/WEBHOOK.md, the contract).

One Home Assistant webhook per webhook machine (registered by __init__.py, local_only False, POST only). Each request:
size cap (413) -> JSON shape -> machine id -> ts window -> HMAC-SHA256 (constant time) -> nonce not seen in 10 min
(all rejections: 401, empty body, logged at debug with the reason only). Then by type:
- hello: {"ok": true};
- snapshot: into the same CrewStore as MQTT snapshots, status online;
- poll: held up to POLL_HOLD s until commands are queued, then {"commands": [...]}; [] on timeout. One held poll per
  machine: a newer poll releases the older one with [];
- ack: resolved like an MQTT ack (commands.resolve_ack).
Every valid request marks the machine online and restarts its ONLINE_WINDOW timer; when it runs out, offline.

Commands for webhook machines are queued here (commands.py calls enqueue): max MAX_QUEUE, QUEUE_TTL s, each one handed
out once (at most once). Never logged: the secret, the webhook id, signatures, request bodies.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import urlsplit

from aiohttp import web

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from . import commands
from .store import CrewStore

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
PAIRING_PREFIX = "crewhub1:"
MAX_BODY = 256 * 1024  # bytes
TS_WINDOW_MS = 120_000
NONCE_TTL = 600.0  # seconds
MAX_NONCES = 10_000  # per machine; far above what a well-behaved machine sends in 10 min
MAX_NONCE_LEN = 128
POLL_HOLD = 25.0  # seconds
ONLINE_WINDOW = 90.0  # seconds without a valid request -> offline
MAX_QUEUE = 20
QUEUE_TTL = 60.0  # seconds
TYPES = ("hello", "snapshot", "poll", "ack")


class Rejected(Exception):
    """A request that fails verification; .reason goes to the debug log only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---- pairing ----
def normalize_public_url(value: Any) -> str | None:
    """https://host[:port] with no path, query, fragment or credentials, else None."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port  # raises on a bad port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
        return None
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port is not None else "")


def _encode_code(body: dict[str, str]) -> str:
    raw = json.dumps(body, separators=(",", ":"))
    return PAIRING_PREFIX + base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def pairing_code(public_url: str, webhook_id: str, secret: str, machine: str) -> str:
    """Webhook machine: crewhub1: + unpadded base64url(JSON {"t": "webhook", "u", "w", "s", "m"})."""
    return _encode_code({"t": "webhook", "u": public_url, "w": webhook_id, "s": secret, "m": machine})


def mqtt_pairing_code(secret: str, machine: str) -> str:
    """MQTT machine (new command secret only): crewhub1: + unpadded base64url(JSON {"t": "mqtt", "m", "s"})."""
    return _encode_code({"t": "mqtt", "m": machine, "s": secret})


# ---- verification ----
def canonical(request: dict) -> bytes:
    body = {k: request[k] for k in ("v", "m", "ts", "nonce", "type", "data")}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(secret: str, request: dict) -> str:
    return hmac.new(secret.encode("utf-8"), canonical(request), hashlib.sha256).hexdigest()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_request(raw: bytes) -> dict:
    """JSON object with the WEBHOOK.md fields and types (other keys are ignored), else Rejected("bad_shape")."""
    try:
        req = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise Rejected("bad_shape") from err
    if not isinstance(req, dict):
        raise Rejected("bad_shape")
    if not (
        _is_int(req.get("v"))
        and isinstance(req.get("m"), str)
        and _is_int(req.get("ts"))
        and isinstance(req.get("nonce"), str) and 0 < len(req["nonce"]) <= MAX_NONCE_LEN
        and req.get("type") in TYPES
        and isinstance(req.get("data"), dict)
        and isinstance(req.get("sig"), str)
    ):
        raise Rejected("bad_shape")
    if req["v"] != PROTOCOL_VERSION:
        raise Rejected("bad_shape")
    return req


async def read_limited(request: web.Request, limit: int = MAX_BODY) -> bytes | None:
    """The request body, or None when it is larger than limit (never reads more than limit + 1 bytes)."""
    if request.content_length is not None and request.content_length > limit:
        return None
    buf = bytearray()
    while True:
        chunk = await request.content.read(limit + 1 - len(buf))
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > limit:
            return None


# ---- per-machine state ----
@dataclass
class _Machine:
    machine_id: str
    webhook_id: str
    secret: str
    queue: deque = field(default_factory=deque)  # (expires monotonic, envelope)
    waiter: asyncio.Future | None = None  # the held poll
    nonces: dict[str, float] = field(default_factory=dict)  # nonce -> expires monotonic
    offline_timer: CALLBACK_TYPE | None = None

    def prune(self, now: float) -> None:
        while self.queue and self.queue[0][0] <= now:
            self.queue.popleft()
        if self.nonces:
            for nonce in [n for n, exp in self.nonces.items() if exp <= now]:
                del self.nonces[nonce]

    def take(self, now: float) -> list[dict]:
        self.prune(now)
        out = [env for _, env in self.queue]
        self.queue.clear()
        return out

    def release(self, commands_: list[dict] | None = None) -> None:
        """Answer the held poll (with [] unless commands are given)."""
        waiter, self.waiter = self.waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(commands_ or [])

    def cancel_timer(self) -> None:
        if self.offline_timer is not None:
            self.offline_timer()
            self.offline_timer = None


class WebhookHub:
    """Queues, held polls, nonce caches and online timers of every webhook machine.

    Kept in hass.data across config entry reloads (like the store), so queued commands and online status survive an
    options change. set_machines() drops the state of machines that were removed or re-paired.
    """

    def __init__(self, hass: HomeAssistant, store: CrewStore, clock: Callable[[], float] = time.monotonic) -> None:
        self.hass = hass
        self.store = store
        self._clock = clock
        self._machines: dict[str, _Machine] = {}

    # ---- configuration ----
    @callback
    def set_machines(self, machines: list[tuple[str, str, str]]) -> None:
        """[(machine id, webhook id, secret)] for the current webhook machines."""
        wanted = {mid: (wid, secret) for mid, wid, secret in machines}
        for mid in list(self._machines):
            state = self._machines[mid]
            if wanted.get(mid) != (state.webhook_id, state.secret):
                self._drop(mid)
        for mid, (wid, secret) in wanted.items():
            if mid not in self._machines:
                self._machines[mid] = _Machine(mid, wid, secret)
            if self.store.status(mid) == "online" and self._machines[mid].offline_timer is None:
                self._touch_timer(mid)  # online from before a reload: give it a fresh window

    @callback
    def _drop(self, mid: str) -> None:
        state = self._machines.pop(mid)
        state.release()
        state.cancel_timer()
        state.queue.clear()
        if self.store.status(mid) == "online":
            self.store.set_status(mid, "offline")  # its credentials are gone

    @callback
    def suspend(self) -> None:
        """Entry unloaded: release held polls and stop timers; queues are kept for the reload."""
        for state in self._machines.values():
            state.release()
            state.cancel_timer()

    @callback
    def shutdown(self) -> None:
        """Entry removed: forget everything."""
        for mid in list(self._machines):
            state = self._machines.pop(mid)
            state.release()
            state.cancel_timer()

    def is_webhook_machine(self, mid: str) -> bool:
        return mid in self._machines

    # ---- commands (commands.py) ----
    @callback
    def enqueue(self, mid: str, envelope: dict) -> bool:
        """Queue a signed envelope for the machine's poll. False when the machine is unknown or its queue is full."""
        state = self._machines.get(mid)
        if state is None:
            return False
        now = self._clock()
        state.prune(now)
        if len(state.queue) >= MAX_QUEUE:
            return False
        state.queue.append((now + QUEUE_TTL, envelope))
        if state.waiter is not None and not state.waiter.done():
            state.release(state.take(now))
        return True

    def queued(self, mid: str) -> int:
        state = self._machines.get(mid)
        return len(state.queue) if state else 0

    # ---- online status ----
    @callback
    def _touch_timer(self, mid: str) -> None:
        state = self._machines[mid]
        state.cancel_timer()

        @callback
        def _offline(_now: Any) -> None:
            if self._machines.get(mid) is state:
                state.offline_timer = None
                self.store.set_status(mid, "offline")

        state.offline_timer = async_call_later(self.hass, ONLINE_WINDOW, _offline)

    # ---- requests ----
    def verify(self, mid: str, req: dict) -> None:
        """Raise Rejected unless the parsed request is valid for machine mid; records its nonce when it is."""
        state = self._machines.get(mid)
        if state is None:
            raise Rejected("unknown_machine")
        if req["m"] != mid:
            raise Rejected("wrong_machine")
        if abs(int(time.time() * 1000) - req["ts"]) > TS_WINDOW_MS:
            raise Rejected("stale")
        expected = sign(state.secret, req)
        if not hmac.compare_digest(req["sig"].encode("utf-8", "replace"), expected.encode("ascii")):
            raise Rejected("bad_signature")
        now = self._clock()
        state.prune(now)
        if req["nonce"] in state.nonces:
            raise Rejected("replay")
        if len(state.nonces) >= MAX_NONCES:
            raise Rejected("too_many_nonces")
        state.nonces[req["nonce"]] = now + NONCE_TTL

    async def async_handle(self, mid: str, request: web.Request) -> web.Response:
        raw = await read_limited(request)
        if raw is None:
            _LOGGER.debug("Crew Hub webhook request for %s rejected: too_large", mid)
            return web.Response(status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            req = parse_request(raw)
            self.verify(mid, req)
        except Rejected as err:
            _LOGGER.debug("Crew Hub webhook request for %s rejected: %s", mid, err.reason)
            return web.Response(status=HTTPStatus.UNAUTHORIZED)

        kind, data = req["type"], req["data"]
        state = self._machines[mid]
        self._touch_timer(mid)
        if kind == "snapshot":
            self.store.set_snapshot(mid, data, status="online")
        else:
            self.store.set_status(mid, "online")

        if kind == "poll":
            return web.json_response({"commands": await self._async_poll(state)})
        if kind == "ack":
            commands.resolve_ack(self.hass, mid, data)
        return web.json_response({"ok": True})

    async def _async_poll(self, state: _Machine) -> list[dict]:
        now = self._clock()
        ready = state.take(now)
        if ready:
            state.release()  # an older held poll gets []
            return ready
        state.release()  # one held poll per machine: the newer one wins
        fut: asyncio.Future = self.hass.loop.create_future()
        state.waiter = fut
        try:
            await asyncio.wait({fut}, timeout=POLL_HOLD)
        finally:
            if state.waiter is fut:
                state.waiter = None
            if not fut.done():
                fut.cancel()
        if fut.cancelled():
            return []
        return fut.result()
