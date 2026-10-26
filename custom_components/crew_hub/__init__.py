"""Crew Hub: live Claude Code sessions from MQTT, pushed to a Lovelace card over Home Assistant's websocket.

docs/DESIGN.md §1: each machine's crew-hubd publishes <prefix>/<machine>/snapshot (retained) and <prefix>/<machine>/status
(LWT). This integration mirrors them in memory (store.py, never recorded), pushes changes to admin websocket subscribers
(websocket.py) and exposes a few summary entities.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components import frontend, mqtt
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .const import (
    CARD_FILENAME,
    CARD_URL,
    CONF_MACHINES,
    CONF_PREFIX,
    DATA_FRONTEND,
    DATA_STORE,
    DEFAULT_PREFIX,
    DOMAIN,
    VERSION,
)
from .store import CrewStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CARD_PATH = Path(__file__).parent / "www" / CARD_FILENAME

CrewHubConfigEntry = ConfigEntry[CrewStore]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the websocket commands once per Home Assistant run."""
    websocket.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CrewHubConfigEntry) -> bool:
    """Subscribe to the machines' MQTT topics and start the entities."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT is not available")

    prefix = entry.data.get(CONF_PREFIX, DEFAULT_PREFIX)
    machines = entry.options.get(CONF_MACHINES, [])
    # The store outlives reloads (options changes) so open websocket subscriptions keep their feed.
    store: CrewStore | None = hass.data.get(DATA_STORE)
    if store is None:
        store = hass.data[DATA_STORE] = CrewStore(prefix, machines)
    else:
        store.set_prefix(prefix)
        store.set_configured(machines)
    entry.runtime_data = store

    @callback
    def _message(msg: mqtt.ReceiveMessage) -> None:
        store.handle_message(msg.topic, msg.payload)

    for topic in store.topics:
        entry.async_on_unload(await mqtt.async_subscribe(hass, topic, _message, qos=1, encoding=None))

    store.active = True
    await _async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: CrewHubConfigEntry) -> None:
    """Machines changed: reload so the per-machine entities follow."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CrewHubConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and (store := hass.data.get(DATA_STORE)) is not None:
        store.active = False
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: CrewHubConfigEntry) -> None:
    if (store := hass.data.pop(DATA_STORE, None)) is not None:
        store.active = False
        store.reset()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the card at /crew_hub/crew-hub-card.js and load it on every frontend page (once per run)."""
    if hass.data.get(DATA_FRONTEND) or "http" not in hass.config.components or hass.http is None:
        return
    digest = await hass.async_add_executor_job(_file_hash, CARD_PATH)
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_PATH), True)])
    if "frontend" in hass.config.components:
        frontend.add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}-{digest}")
    hass.data[DATA_FRONTEND] = True
