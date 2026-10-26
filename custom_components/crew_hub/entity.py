"""Shared base for Crew Hub entities: one "Crew Hub" device, push updates only when the state changes."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, NAME, VERSION
from .store import CrewStore


class CrewHubEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, store: CrewStore, key: str) -> None:
        self._store = store
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer=NAME,
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )
        self._last: Any = None

    def _snapshot_key(self) -> Any:
        """Everything that shows in the entity's state; a write happens only when this changes."""
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        self._last = self._snapshot_key()
        self.async_on_remove(self._store.add_listener(self._handle_event))

    @callback
    def _handle_event(self, event: dict) -> None:
        key = self._snapshot_key()
        if key != self._last:
            self._last = key
            self.async_write_ha_state()
