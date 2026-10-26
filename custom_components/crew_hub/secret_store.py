"""Machine secrets, kept out of the config entry (0.3.1).

Config entry options are readable through the config-entry APIs (REST, websocket, ha-mcp), so since 0.3.1 the
command secret and the webhook id of every machine live in a private Home Assistant Store instead:
.storage/crew_hub.secrets (version 1, written 0600): {machineId: {"secret": str, "webhookId": str (webhook machines)}}.
Options keep only id, name, transport, public_url and "has_secret": true.

One SecretStore per Home Assistant run (hass.data[DATA_SECRETS]); it outlives config entry reloads. Its contents are
never logged, never put into a form, the websocket API, diagnostics or the config entry.

async_migrate_entry() moves secrets that are still in the options (entries from 0.3.0 and earlier) into the Store and
rewrites the options without them. Store first, options second, so a crash in between loses nothing; it only calls
async_update_entry when the options actually hold a secret, so it runs once and can't start a reload loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_MACHINE_HAS_SECRET,
    CONF_MACHINE_ID,
    CONF_MACHINE_SECRET,
    CONF_MACHINE_WEBHOOK_ID,
    CONF_MACHINES,
    DATA_SECRETS,
    DATA_SECRETS_LOCK,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "crew_hub.secrets"
STORAGE_VERSION = 1
KEY_SECRET = "secret"
KEY_WEBHOOK_ID = "webhookId"
SECRET_KEYS = (CONF_MACHINE_SECRET, CONF_MACHINE_WEBHOOK_ID)  # keys that must never stay in the options


class SecretStore:
    """{machineId: {"secret", "webhookId"?}} in memory, saved to a private Store on every change."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, dict[str, str]]] = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        self._data: dict[str, dict[str, str]] = {}

    async def async_load(self) -> None:
        raw = await self._store.async_load()
        self._data = {}
        if isinstance(raw, dict):
            for mid, item in raw.items():
                if isinstance(mid, str) and isinstance(item, dict) and isinstance(item.get(KEY_SECRET), str):
                    clean = {KEY_SECRET: item[KEY_SECRET]}
                    if isinstance(item.get(KEY_WEBHOOK_ID), str) and item[KEY_WEBHOOK_ID]:
                        clean[KEY_WEBHOOK_ID] = item[KEY_WEBHOOK_ID]
                    self._data[mid] = clean

    async def _async_save(self) -> None:
        await self._store.async_save({mid: dict(item) for mid, item in self._data.items()})

    # ---- reads ----
    def secret(self, mid: str) -> str | None:
        item = self._data.get(mid)
        return item.get(KEY_SECRET) if item else None

    def webhook_id(self, mid: str) -> str | None:
        item = self._data.get(mid)
        return item.get(KEY_WEBHOOK_ID) if item else None

    def machine_ids(self) -> list[str]:
        return list(self._data)

    # ---- writes (each one is saved before it returns) ----
    async def async_set(self, mid: str, secret: str, webhook_id: str | None = None) -> None:
        item = {KEY_SECRET: secret}
        if webhook_id:
            item[KEY_WEBHOOK_ID] = webhook_id
        self._data[mid] = item
        await self._async_save()

    async def async_set_many(self, items: dict[str, dict[str, str]]) -> None:
        for mid, item in items.items():
            self._data[mid] = dict(item)
        await self._async_save()

    async def async_remove(self, *mids: str) -> None:
        if any(mid in self._data for mid in mids):
            for mid in mids:
                self._data.pop(mid, None)
            await self._async_save()

    async def async_remove_file(self) -> None:
        self._data = {}
        await self._store.async_remove()


async def async_get_secrets(hass: HomeAssistant) -> SecretStore:
    """The run's SecretStore, loaded on first use."""
    secrets = hass.data.get(DATA_SECRETS)
    if secrets is not None:
        return secrets
    lock: asyncio.Lock = hass.data.setdefault(DATA_SECRETS_LOCK, asyncio.Lock())
    async with lock:
        secrets = hass.data.get(DATA_SECRETS)
        if secrets is None:
            secrets = SecretStore(hass)
            await secrets.async_load()
            hass.data[DATA_SECRETS] = secrets
    return secrets


def public_machine(machine: dict[str, Any], has_secret: bool) -> dict[str, Any]:
    """The options form of a machine: every key except the secrets, plus has_secret when it has one."""
    out = {k: v for k, v in machine.items() if k not in SECRET_KEYS and k != CONF_MACHINE_HAS_SECRET}
    if has_secret:
        out[CONF_MACHINE_HAS_SECRET] = True
    return out


def options_hold_secrets(options: Any) -> bool:
    machines = options.get(CONF_MACHINES, []) if hasattr(options, "get") else []
    return any(isinstance(m, dict) and any(k in m for k in SECRET_KEYS) for m in machines or [])


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    """Move secrets from the entry options into the SecretStore; return the machines as they are now in the options.

    Idempotent: options without secrets are left untouched (no async_update_entry, so no reload). Machines missing from
    the options are dropped from the Store (removed while Home Assistant was down, or a half-finished removal).
    """
    secrets = await async_get_secrets(hass)
    machines = [m for m in entry.options.get(CONF_MACHINES, []) if isinstance(m, dict)]
    if options_hold_secrets(entry.options):
        moved: dict[str, dict[str, str]] = {}
        for m in machines:
            mid, secret = m.get(CONF_MACHINE_ID), m.get(CONF_MACHINE_SECRET)
            if isinstance(mid, str) and isinstance(secret, str) and secret:
                item = {KEY_SECRET: secret}
                if isinstance(m.get(CONF_MACHINE_WEBHOOK_ID), str) and m[CONF_MACHINE_WEBHOOK_ID]:
                    item[KEY_WEBHOOK_ID] = m[CONF_MACHINE_WEBHOOK_ID]
                moved[mid] = item
        await secrets.async_set_many(moved)  # saved before the options lose them
        machines = [
            public_machine(m, bool(secrets.secret(m.get(CONF_MACHINE_ID, "")))) for m in machines
        ]
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_MACHINES: machines})
        _LOGGER.info("Crew Hub: moved the secrets of %d machine(s) out of the config entry", len(moved))
    known = {m.get(CONF_MACHINE_ID) for m in machines}
    orphans = [mid for mid in secrets.machine_ids() if mid not in known]
    if orphans:
        await secrets.async_remove(*orphans)
    return machines
