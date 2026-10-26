"""binary_sensor.crew_hub_needs_me (any session waiting) and binary_sensor.crew_hub_<machine>_online per configured machine."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from . import CrewHubConfigEntry
from .const import DOMAIN
from .entity import CrewHubEntity
from .store import CrewStore


async def async_setup_entry(
    hass: HomeAssistant, entry: CrewHubConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    store = entry.runtime_data
    configured = store.configured
    # Drop registry entries of machines that were removed in the options flow.
    registry = er.async_get(hass)
    wanted = {f"{entry.entry_id}_machine_{mid}_online" for mid in configured}
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg.domain == "binary_sensor" and reg.unique_id.endswith("_online") and reg.unique_id not in wanted:
            registry.async_remove(reg.entity_id)

    entities: list[CrewHubEntity] = [CrewNeedsMeSensor(entry, store, "needs_me")]
    entities += [CrewMachineOnlineSensor(entry, store, mid, name) for mid, name in configured.items()]
    async_add_entities(entities)


class CrewNeedsMeSensor(CrewHubEntity, BinarySensorEntity):
    _attr_translation_key = "needs_me"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _unrecorded_attributes = frozenset({"sessions"})

    def _snapshot_key(self) -> tuple:
        return tuple(self._store.waiting_names()), self._store.any_waiting()

    @property
    def is_on(self) -> bool:
        return self._store.any_waiting()

    @property
    def extra_state_attributes(self) -> dict:
        return {"sessions": self._store.waiting_names()}


class CrewMachineOnlineSensor(CrewHubEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry: CrewHubConfigEntry, store: CrewStore, mid: str, name: str) -> None:
        super().__init__(entry, store, f"machine_{mid}_online")
        self._mid = mid
        self._attr_name = f"{name} online"
        # Entity id from the machine id (stable even if the display name changes).
        self.entity_id = f"binary_sensor.{DOMAIN}_{slugify(mid)}_online"

    def _snapshot_key(self) -> str:
        return self._store.status(self._mid)

    @property
    def is_on(self) -> bool | None:
        status = self._store.status(self._mid)
        return None if status == "unknown" else status == "online"
