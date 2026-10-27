"""In-memory model of every machine's latest Crew Hub snapshot.

No Home Assistant imports, so it can be unit-tested with the stdlib (tests/test_store.py).
Snapshots are not persisted: retained MQTT messages rebuild the model after a restart. Dismissals (0.4.0) are kept here
too and persisted by dismissed.py through load_dismissed()/dismissed_data()/on_dismissed_change.

Model: {machineId: {"status": online|offline|unknown, "snapshot": dict|None, "receivedAt": epoch ms|None,
                    "configured": bool}}
Events sent to listeners: {"type": "full", "machines": {...}} or {"type": "machine", "id": ..., "data": {...}}.

Dismissed (0.4.0, crew_hub/dismiss from the card, shared by every device):
{machineId: {sessionId: {"agents": [agentId], "session": bool, "seen": epoch ms, "agentSeen": {agentId: epoch ms}}}}.
Outgoing data (events, as_dict) leaves out dismissed agents, and dismissed sessions while they are idle or ended; machine()
stays unfiltered for commands and diagnostics. A dismissed session that turns working or waiting loses its flag (it comes
back). Ids missing from the machine's snapshots for 24 h are pruned.
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
DISMISS_TTL_MS = 24 * 3600 * 1000  # dismissed ids unseen in the machine's snapshots this long are pruned
MAX_DISMISS_ID_LEN = 128
MAX_DISMISS_AGENTS = 200  # per session
MAX_DISMISS_SESSIONS = 200  # per machine
ACTIVE_STATUSES = ("working", "waiting")  # a dismissed session in one of these is shown again (flag cleared)

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
        self._dismissed: dict[str, dict[str, dict]] = {}
        self.on_dismissed_change: Callable[[], None] | None = None  # set by dismissed.py (schedules a save)
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
        self._dismissed.clear()
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
    def _view(self, mid: str, public: bool = True) -> dict:
        entry = self._machines.get(mid) or {}
        snapshot = entry.get("snapshot")
        return {
            "status": entry.get("status", "unknown"),
            "snapshot": self._public_snapshot(mid, snapshot) if public else snapshot,
            "receivedAt": entry.get("receivedAt"),
            "configured": mid in self._configured,
        }

    def as_dict(self) -> dict[str, dict]:
        """Every machine as the card sees it (dismissed agents/sessions left out)."""
        ids = list(self._configured) + [m for m in self._machines if m not in self._configured]
        return {mid: self._view(mid) for mid in ids}

    def machine(self, mid: str) -> dict | None:
        """One machine, unfiltered (commands validate against the real snapshot)."""
        if mid not in self._machines and mid not in self._configured:
            return None
        return self._view(mid, public=False)

    def _public_snapshot(self, mid: str, snapshot: Any) -> Any:
        """The snapshot without dismissed agents and dismissed idle/ended sessions; the same object when nothing applies."""
        dismissed = self._dismissed.get(mid)
        if not dismissed or not isinstance(snapshot, dict) or not isinstance(snapshot.get("sessions"), list):
            return snapshot
        out: list = []
        changed = False
        for s in snapshot["sessions"]:
            item = dismissed.get(s.get("id")) if isinstance(s, dict) and isinstance(s.get("id"), str) else None
            if not item:
                out.append(s)
                continue
            if item["session"] and s.get("status") not in ACTIVE_STATUSES:
                changed = True
                continue
            agents = s.get("agents")
            if item["agents"] and isinstance(agents, list):
                gone = set(item["agents"])
                keep = [a for a in agents if not (isinstance(a, dict) and a.get("id") in gone)]
                if len(keep) != len(agents):
                    s = {**s, "agents": keep}
                    changed = True
            out.append(s)
        return {**snapshot, "sessions": out} if changed else snapshot

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

    # ---- dismissals (crew_hub/dismiss) ----
    @staticmethod
    def _ok_id(value: Any) -> bool:
        return isinstance(value, str) and 0 < len(value) <= MAX_DISMISS_ID_LEN

    def load_dismissed(self, data: Any) -> None:
        """Replace the dismissals with stored data (bad items are dropped); subscribers get a "full" when it changed."""
        clean: dict[str, dict[str, dict]] = {}
        now = self._clock()
        if isinstance(data, dict):
            for mid, sessions in data.items():
                if not (isinstance(mid, str) and MACHINE_ID_RE.match(mid)) or not isinstance(sessions, dict):
                    continue
                for sid, item in list(sessions.items())[:MAX_DISMISS_SESSIONS]:
                    if not self._ok_id(sid) or not isinstance(item, dict):
                        continue
                    agents = [a for a in item.get("agents") or [] if self._ok_id(a)][:MAX_DISMISS_AGENTS]
                    flag = item.get("session") is True
                    if not agents and not flag:
                        continue
                    seen = item.get("seen") if isinstance(item.get("seen"), int) else now
                    raw_seen = item.get("agentSeen") if isinstance(item.get("agentSeen"), dict) else {}
                    agent_seen = {a: raw_seen[a] if isinstance(raw_seen.get(a), int) else seen for a in agents}
                    clean.setdefault(mid, {})[sid] = {"agents": list(dict.fromkeys(agents)), "session": flag,
                                                      "seen": seen, "agentSeen": agent_seen}
        if clean == self._dismissed:
            return
        self._dismissed = clean
        self._emit_full()

    def dismissed_data(self) -> dict[str, dict[str, dict]]:
        """A copy for saving."""
        return {mid: {sid: {"agents": list(item["agents"]), "session": item["session"], "seen": item["seen"],
                            "agentSeen": dict(item["agentSeen"])} for sid, item in sessions.items()}
                for mid, sessions in self._dismissed.items()}

    def dismiss(self, mid: str, session: str, agents: Any = None, session_dismissed: bool = False) -> str | None:
        """Dismiss agents of a session and/or the session itself. Returns an error code, or None when accepted."""
        if not isinstance(mid, str) or not MACHINE_ID_RE.match(mid) or (mid not in self._machines and mid not in self._configured):
            return "unknown_machine"
        if not self._ok_id(session):
            return "invalid_args"
        agents = agents or []
        if not isinstance(agents, list) or len(agents) > MAX_DISMISS_AGENTS or not all(self._ok_id(a) for a in agents):
            return "invalid_args"
        if not agents and not session_dismissed:
            return "invalid_args"
        now = self._clock()
        sessions = self._dismissed.setdefault(mid, {})
        if session not in sessions and len(sessions) >= MAX_DISMISS_SESSIONS:
            self._prune_dismissed(mid, None, now)
            if len(sessions) >= MAX_DISMISS_SESSIONS:
                return "too_many"
        item = sessions.setdefault(session, {"agents": [], "session": False, "seen": now, "agentSeen": {}})
        item["seen"] = now
        for a in agents:
            if a not in item["agentSeen"]:
                item["agents"].append(a)
            item["agentSeen"][a] = now
        del item["agents"][:max(0, len(item["agents"]) - MAX_DISMISS_AGENTS)]
        item["agentSeen"] = {a: item["agentSeen"][a] for a in item["agents"]}
        if session_dismissed:
            item["session"] = True
        # A session that is working or waiting right now is not hidden (and keeps no flag).
        self._prune_dismissed(mid, (self._machines.get(mid) or {}).get("snapshot"), now)
        if self.on_dismissed_change:
            self.on_dismissed_change()
        self._emit({"type": "machine", "id": mid, "data": self._view(mid)})
        return None

    def _prune_dismissed(self, mid: str, snapshot: Any, now: int) -> bool:
        """Refresh seen times from the snapshot, clear flags of active sessions, drop ids unseen for 24 h.
        Returns True when the stored dismissals changed in a way worth saving (not for seen times alone)."""
        sessions = self._dismissed.get(mid)
        if not sessions:
            return False
        live = {s["id"]: s for s in _sessions(snapshot) if isinstance(s.get("id"), str)}
        changed = False
        for sid in list(sessions):
            item = sessions[sid]
            s = live.get(sid)
            if s is None:
                if now - item["seen"] > DISMISS_TTL_MS:
                    del sessions[sid]
                    changed = True
                continue
            item["seen"] = now
            if item["session"] and s.get("status") in ACTIVE_STATUSES:
                item["session"] = False
                changed = True
            present = {a.get("id") for a in s.get("agents") or [] if isinstance(a, dict)}
            for aid in list(item["agents"]):
                if aid in present:
                    item["agentSeen"][aid] = now
                elif now - item["agentSeen"].get(aid, now) > DISMISS_TTL_MS:
                    item["agents"].remove(aid)
                    item["agentSeen"].pop(aid, None)
                    changed = True
            if not item["agents"] and not item["session"]:
                del sessions[sid]
                changed = True
        if not sessions:
            del self._dismissed[mid]
        return changed

    def _snapshot_arrived(self, mid: str, snapshot: Any) -> None:
        if mid in self._dismissed and self._prune_dismissed(mid, snapshot, self._clock()) and self.on_dismissed_change:
            self.on_dismissed_change()

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
        self._snapshot_arrived(mid, snapshot)
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
            if snapshot is not None:
                self._snapshot_arrived(mid, snapshot)
        entry["receivedAt"] = self._clock()
        self._machines[mid] = entry
        self._emit({"type": "machine", "id": mid, "data": self._view(mid)})
        return True
