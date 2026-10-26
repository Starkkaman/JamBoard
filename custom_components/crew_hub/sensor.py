"""sensor.crew_hub_working: sessions with status working across online machines."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CrewHubConfigEntry
from .entity import CrewHubEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: CrewHubConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([CrewWorkingSensor(entry, entry.runtime_data, "working")])


class CrewWorkingSensor(CrewHubEntity, SensorEntity):
    _attr_translation_key = "working"

    def _snapshot_key(self) -> int:
        return self._store.working_count()

    @property
    def native_value(self) -> int:
        return self._store.working_count()
