"""Dismissed agents and sessions (0.4.0), shared by every device that shows the card.

The card's "Clear finished" and "Dismiss" buttons call crew_hub/dismiss (websocket.py); store.py keeps the dismissals
and filters them out of what subscribers get. This module persists them in a private Home Assistant Store
.storage/crew_hub.dismissed (version 1): {machineId: {sessionId: {"agents": [ids], "session": bool, "seen": epoch ms,
"agentSeen": {id: epoch ms}}}}. One Store per Home Assistant run (hass.data[DATA_DISMISSED]); like the CrewStore it
outlives config entry reloads. Only ids live here: no names, summaries or other snapshot text.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DATA_DISMISSED
from .store import CrewStore

STORAGE_KEY = "crew_hub.dismissed"
STORAGE_VERSION = 1
SAVE_DELAY = 5  # seconds; background changes (pruning, a flag cleared) are batched, a dismiss is saved at once


class Dismissed:
    def __init__(self, hass: HomeAssistant, store: CrewStore) -> None:
        self.store = store
        self._file: Store[dict] = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)

    async def async_load(self) -> None:
        self.store.load_dismissed(await self._file.async_load())
        self.store.on_dismissed_change = self._schedule_save

    @callback
    def _schedule_save(self) -> None:
        self._file.async_delay_save(self.store.dismissed_data, SAVE_DELAY)

    async def async_save(self) -> None:
        await self._file.async_save(self.store.dismissed_data())

    async def async_remove_file(self) -> None:
        self.store.on_dismissed_change = None
        await self._file.async_remove()


async def async_attach(hass: HomeAssistant, store: CrewStore) -> Dismissed:
    """Load the dismissals into store once per run (again only when the store object was replaced)."""
    current: Dismissed | None = hass.data.get(DATA_DISMISSED)
    if current is not None and current.store is store:
        return current
    current = Dismissed(hass, store)
    await current.async_load()
    hass.data[DATA_DISMISSED] = current
    return current
