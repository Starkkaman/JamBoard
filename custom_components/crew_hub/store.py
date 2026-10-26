"""In-memory model of every machine's latest Crew Hub snapshot.

No Home Assistant imports, so it can be unit-tested with the stdlib (tests/test_store.py).
Nothing here is persisted: retained MQTT messages rebuild the model after a restart.

Model: {machineId: {"status": online|offline|unknown, "snapshot": dict|None, "receivedAt": epoch ms|None,
                    "configured": bool}}
Events sent to listeners: {"type": "full", "machines": {...}} or {"type": "machine", "id": ..., "data": {...}}.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Iterable

_LOGGER = logging.getLogger(__name__)

MAX_PAYLOAD = 256 * 1024  # bytes; larger snapshots are dropped
MAX_MACHINES = 64  # unconfigured ids beyond this are ignored (anyone on the LAN can publish under the prefix)
MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STATUSES = ("online", "offline")
MAX_WAITING_NAMES = 5
MAX_NAME_LEN = 60

Listener = Callable[[dict], None]


def now_ms() -> int:
    return int(time.time() * 1000)


def _sessions(snapshot: Any) -> list[dict]:
    if not isinstance(snapshot, dict):
        return []
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list):
        return []
    return [s for s in sessions if isinstance(s, dict)]


class CrewStore:
    """Latest status + snapshot per machine, with change listeners."""

    def __init__(self, prefix: str = "crew", configured: Any = None, clock: Callable[[], int] = now_ms) -> None:
        self._prefix = prefix
        self._configured: dict[str, str] = {}
        self._machines: dict[str, dict] = {}
        self._listeners: list[Listener] = []
        self._clock = clock
        self.active = False  # True while a config entry is loaded (set by __init__.py)
        if configured:
            self._configured = self._normalize_configured(configured)

    # ---- configuration ----
    @property
    def prefix(self) -> str:
        return self._prefix

    @property
    def topics(self) -> list[str]:
        return [f"{self._prefix}/+/snapshot", f"{self._prefix}/+/status"]

    @property
    def configured(self) -> dict[str, str]:
        return dict(self._configured)

    @staticmethod
    def _normalize_configured(machines: Any) -> dict[str, str]:
        """Accept {id: name} or [{"id":..,"name":..,...}] (config entry options); secrets are never kept here."""
        out: dict[str, str] = {}
        if isinstance(machines, dict):
            items: Iterable = machines.items()
        else:
            items = ((m.get("id"), m.get("name")) for m in (machines or []) if isinstance(m, dict))
        for mid, name in items:
            if isinstance(mid, str) and MACHINE_ID_RE.match(mid):
                out[mid] = str(name or mid)
        return out

    def set_prefix(self, prefix: str) -> None:
        if prefix == self._prefix:
            return
        self._prefix = prefix
        self._machines.clear()
        self._emit_full()

    def set_configured(self, machines: Any) -> None:
        new = self._normalize_configured(machines)
        if new == self._configured:
            return
        self._configured = new
        self._emit_full()

    def clear(self) -> None:
        self._machines.clear()
        self._emit_full()

    def reset(self) -> None:
        """Forget everything (config entry removed); subscribers get one empty "full"."""
        self._configured = {}
        self._machines.clear()
        self._emit_full()

    # ---- listeners ----
    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    def _emit(self, event: dict) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # one broken listener must not starve the others
                _LOGGER.exception("Crew Hub listener failed")

    def _emit_full(self) -> None:
        self._emit({"type": "full", "machines": self.as_dict()})

    # ---- views ----
    def _view(self, mid: str) -> dict:
        entry = self._machines.get(mid) or {}
        return {
            "status": entry.get("status", "unknown"),
            "snapshot": entry.get("snapshot"),
            "receivedAt": entry.get("receivedAt"),
            "configured": mid in self._configured,
        }

    def as_dict(self) -> dict[str, dict]:
        ids = list(self._configured) + [m for m in self._machines if m not in self._configured]
        return {mid: self._view(mid) for mid in ids}

    def machine(self, mid: str) -> dict | None:
        if mid not in self._machines and mid not in self._configured:
            return None
        return self._view(mid)

    def status(self, mid: str) -> str:
        return (self._machines.get(mid) or {}).get("status", "unknown")

    def _live_sessions(self) -> Iterable[dict]:
        # Sessions on an offline machine are stale (its crew-hubd is gone), so they don't count.
        for entry in self._machines.values():
            if entry.get("status") == "offline":
                continue
            yield from _sessions(entry.get("snapshot"))

    def working_count(self) -> int:
        return sum(1 for s in self._live_sessions() if s.get("status") == "working")

    def waiting_names(self) -> list[str]:
        names = []
        for s in self._live_sessions():
            if s.get("status") == "waiting":
                name = s.get("name") or s.get("project") or s.get("id") or "session"
                names.append(str(name)[:MAX_NAME_LEN])
        return names[:MAX_WAITING_NAMES]

    def any_waiting(self) -> bool:
        return any(s.get("status") == "waiting" for s in self._live_sessions())

    # ---- webhook input (already verified by webhook_transport.py) ----
    def set_status(self, mid: str, status: str) -> bool:
        """Set a machine's status; listeners are told only when it changed. Returns True on change."""
        if not isinstance(mid, str) or not MACHINE_ID_RE.match(mid):
            return False
        status = status if status in STATUSES else "unknown"
        entry = self._machines.get(mid)
        if entry is not None and entry.get("status") == status:
            return False
        if entry is None and mid not in self._configured and len(self._machines) >= MAX_MACHINES:
            return False
        entry = dict(entry or {"snapshot": None})
        entry["status"] = status
        entry["receivedAt"] = self._clock()  # same as an MQTT status message
        self._machines[mid] = entry
        self._emit({"type": "machine", "id": mid, "data": self._view(mid)})
        return True

    def set_snapshot(self, mid: str, snapshot: dict, status: str | None = None) -> bool:
        """Store a full snapshot (and optionally the status) in one change event, like an MQTT snapshot message."""
        if not isinstance(mid, str) or not MACHINE_ID_RE.match(mid) or not isinstance(snapshot, dict):
            return False
        entry = self._machines.get(mid)
        if entry is None and mid not in self._configured and len(self._machines) >= MAX_MACHINES:
            return False
        entry = dict(entry or {"status": "unknown"})
        if status is not None:
            entry["status"] = status if status in STATUSES else "unknown"
        entry["snapshot"] = snapshot
        entry["receivedAt"] = self._clock()
        self._machines[mid] = entry
        self._emit({"type": "machine", "id": mid, "data": self._view(mid)})
        return True

    # ---- MQTT input ----
    def handle_message(self, topic: str, payload: Any) -> bool:
        """Apply one MQTT message. Returns True when the model changed (and listeners were told)."""
        head = self._prefix + "/"
        if not isinstance(topic, str) or not topic.startswith(head):
            return False
        parts = topic[len(head):].split("/")
        if len(parts) != 2 or parts[1] not in ("snapshot", "status"):
            return False
        mid, leaf = parts
        if not MACHINE_ID_RE.match(mid):
            _LOGGER.debug("Ignoring Crew Hub message with a bad machine id on %s", topic[:200])
            return False

        if isinstance(payload, str):
            raw = payload.encode("utf-8", "replace")
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            raw = bytes(payload)
        else:
            return False
        if len(raw) > MAX_PAYLOAD:
            _LOGGER.debug("Ignoring %d-byte Crew Hub message on %s (cap %d)", len(raw), topic, MAX_PAYLOAD)
            return False

        if mid not in self._machines and mid not in self._configured and len(self._machines) >= MAX_MACHINES:
            _LOGGER.debug("Ignoring Crew Hub machine %s: too many machines", mid)
            return False

        entry = self._machines.get(mid)
        if leaf == "status":
            try:
                text = raw.decode("utf-8").strip().lower()
            except UnicodeDecodeError:
                _LOGGER.debug("Ignoring non-UTF-8 Crew Hub status on %s", topic)
                return False
            status = text if text in STATUSES else "unknown"
            if entry is not None and entry.get("status") == status:
                return False
            entry = dict(entry or {"snapshot": None})
            entry["status"] = status
        else:
            if not raw.strip():
                snapshot = None  # retained message cleared
            else:
                try:
                    snapshot = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as err:
                    _LOGGER.debug("Ignoring bad Crew Hub snapshot JSON on %s: %s", topic, err)
                    return False
                if not isinstance(snapshot, dict):
                    _LOGGER.debug("Ignoring Crew Hub snapshot on %s: not a JSON object", topic)
                    return False
            if entry is None and snapshot is None:
                return False
            entry = dict(entry or {"status": "unknown"})
            entry["snapshot"] = snapshot
        entry["receivedAt"] = self._clock()
        self._machines[mid] = entry
        self._emit({"type": "machine", "id": mid, "data": self._view(mid)})
        return True
