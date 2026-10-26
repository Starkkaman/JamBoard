"""Diagnostics for the Crew Hub config entry (Settings > Devices & services > Crew Hub > Download diagnostics).

Holds the entry's data and options, the integration version and a summary of each machine as the in-memory store sees
it (status, when the last snapshot arrived, how many sessions it had, queued webhook commands). Never the secret Store
(secret_store.py), never snapshots themselves (session names, folders). Everything passes async_redact_data over the
secret keys, and every URL keeps only its scheme and host (no path, which could carry a webhook id).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_MACHINE_ID, CONF_MACHINES, DATA_STORE, DATA_WEBHOOK, VERSION

REDACTED = "**REDACTED**"
# Options keys from before 0.3.1 (secret, webhook_id), the Store's spelling (webhookId) and request/command fields.
TO_REDACT = {"secret", "webhook_id", "webhookId", "sig", "s", "w", "code", "password", "commandSecret"}


def _redact_url_paths(value: Any) -> Any:
    """Any http(s) URL string with a path, query or fragment becomes scheme://host[:port]/**REDACTED**."""
    if isinstance(value, dict):
        return {k: _redact_url_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_url_paths(v) for v in value]
    if isinstance(value, str) and "://" in value:
        try:
            parts = urlsplit(value)
        except ValueError:
            return REDACTED
        if parts.scheme.lower() in ("http", "https") and parts.netloc:
            netloc = parts.netloc.rsplit("@", 1)[-1]  # never credentials
            if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
                return f"{parts.scheme}://{netloc}/{REDACTED}"
    return value


def _machine_summary(hass: HomeAssistant, mid: str) -> dict[str, Any]:
    store = hass.data.get(DATA_STORE)
    view = store.machine(mid) if store is not None else None
    hub = hass.data.get(DATA_WEBHOOK)
    out: dict[str, Any] = {"status": None, "receivedAt": None, "sessions": None}
    if view:
        snapshot = view.get("snapshot")
        sessions = snapshot.get("sessions") if isinstance(snapshot, dict) else None
        out.update(status=view.get("status"), receivedAt=view.get("receivedAt"),
                   sessions=len(sessions) if isinstance(sessions, list) else None)
    if hub is not None and hub.is_webhook_machine(mid):
        out["queuedCommands"] = hub.queued(mid)
    return out


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    machines = [m for m in entry.options.get(CONF_MACHINES, []) if isinstance(m, dict)]
    data = {
        "version": VERSION,
        "entry": {"data": dict(entry.data), "options": dict(entry.options), "state": str(entry.state)},
        "machines": {
            str(m.get(CONF_MACHINE_ID)): _machine_summary(hass, str(m.get(CONF_MACHINE_ID))) for m in machines
        },
    }
    return _redact_url_paths(async_redact_data(data, TO_REDACT))
