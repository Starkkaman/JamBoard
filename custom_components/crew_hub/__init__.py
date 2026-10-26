"""Crew Hub: live Claude Code sessions from MQTT, pushed to a Lovelace card over Home Assistant's websocket.

docs/DESIGN.md §1: each machine's crew-hubd publishes <prefix>/<machine>/snapshot (retained) and <prefix>/<machine>/status
(LWT). This integration mirrors them in memory (store.py, never recorded), pushes changes to admin websocket subscribers
(websocket.py) and exposes a few summary entities. Admin users can send signed commands back to a machine
(commands.py, docs/COMMANDS.md) through crew_hub/command or the crew_hub.* services; acks arrive on <prefix>/+/ack.
Machines off the LAN use the webhook transport instead (webhook_transport.py, docs/WEBHOOK.md): one HA webhook per
machine feeds the same store, and their commands wait in a queue for the machine's poll.
Machine secrets (command secret, webhook id) live in a private Store, not in the config entry (secret_store.py).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.hdrs import METH_POST

from homeassistant.components import frontend, mqtt, webhook
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import commands, secret_store, services, websocket
from .const import (
    CARD_FILENAME,
    CARD_URL,
    CONF_MACHINE_ID,
    CONF_MACHINE_NAME,
    CONF_MACHINE_TRANSPORT,
    CONF_PREFIX,
    DATA_FRONTEND,
    DATA_SECRETS,
    DATA_STORE,
    DATA_WEBHOOK,
    DEFAULT_PREFIX,
    DOMAIN,
    TRANSPORT_WEBHOOK,
    VERSION,
)
from .store import CrewStore
from .webhook_transport import WebhookHub

_LOGGER = logging.getLogger(__name__)

LOVELACE_DOMAIN = "lovelace"  # hass.data key and component name; not imported so a missing lovelace never breaks setup

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CARD_PATH = Path(__file__).parent / "www" / CARD_FILENAME

CrewHubConfigEntry = ConfigEntry[CrewStore]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the websocket commands once per Home Assistant run."""
    websocket.async_register(hass)
    services.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CrewHubConfigEntry) -> bool:
    """Move secrets out of the options (once), subscribe to the machines' MQTT topics and start the entities."""
    # First, so secrets leave the options even when MQTT isn't up yet. Runs before the update listener is added.
    machines = await secret_store.async_migrate_entry(hass, entry)
    secrets = await secret_store.async_get_secrets(hass)
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT is not available")

    prefix = entry.data.get(CONF_PREFIX, DEFAULT_PREFIX)
    # The store outlives reloads (options changes) so open websocket subscriptions keep their feed.
    store: CrewStore | None = hass.data.get(DATA_STORE)
    if store is None:
        store = hass.data[DATA_STORE] = CrewStore(prefix, machines)
    else:
        store.set_prefix(prefix)
        store.set_configured(machines)
    entry.runtime_data = store

    # Webhook machines (docs/WEBHOOK.md): one webhook each; MQTT traffic under their ids is ignored.
    hooked = [
        (m, secrets.webhook_id(m[CONF_MACHINE_ID]), secrets.secret(m[CONF_MACHINE_ID]))
        for m in machines
        if m.get(CONF_MACHINE_TRANSPORT) == TRANSPORT_WEBHOOK and isinstance(m.get(CONF_MACHINE_ID), str)
    ]
    hooked = [(m, wid, secret) for m, wid, secret in hooked if wid and secret]
    hub: WebhookHub | None = hass.data.get(DATA_WEBHOOK)
    if hub is None or hub.store is not store:
        hub = hass.data[DATA_WEBHOOK] = WebhookHub(hass, store)
    hub.set_machines([(m[CONF_MACHINE_ID], wid, secret) for m, wid, secret in hooked])
    entry.async_on_unload(hub.suspend)
    for m, wid, _secret in hooked:
        _register_webhook(hass, entry, hub, m, wid)
    webhook_ids = {m[CONF_MACHINE_ID] for m, _wid, _secret in hooked}
    head = prefix + "/"

    def _from_webhook_machine(topic: str) -> bool:
        return topic.startswith(head) and topic[len(head):].split("/", 1)[0] in webhook_ids

    @callback
    def _message(msg: mqtt.ReceiveMessage) -> None:
        if webhook_ids and _from_webhook_machine(msg.topic):
            return
        store.handle_message(msg.topic, msg.payload)

    for topic in store.topics:
        entry.async_on_unload(await mqtt.async_subscribe(hass, topic, _message, qos=1, encoding=None))

    @callback
    def _ack(msg: mqtt.ReceiveMessage) -> None:
        if webhook_ids and _from_webhook_machine(msg.topic):
            return
        commands.handle_ack(hass, prefix, msg.topic, msg.payload)

    entry.async_on_unload(await mqtt.async_subscribe(hass, f"{prefix}/+/ack", _ack, qos=1, encoding=None))

    store.active = True
    await _async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


def _register_webhook(hass: HomeAssistant, entry: CrewHubConfigEntry, hub: WebhookHub, machine: dict,
                      webhook_id: str) -> None:
    mid = machine[CONF_MACHINE_ID]

    async def _handle(hass: HomeAssistant, _webhook_id: str, request: web.Request) -> web.Response:
        return await hub.async_handle(mid, request)

    webhook.async_register(hass, DOMAIN, f"Crew Hub {machine.get(CONF_MACHINE_NAME) or mid}", webhook_id, _handle,
                           local_only=False, allowed_methods=[METH_POST])
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))


async def _async_options_updated(hass: HomeAssistant, entry: CrewHubConfigEntry) -> None:
    """Machines changed: reload so the per-machine entities follow."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CrewHubConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and (store := hass.data.get(DATA_STORE)) is not None:
        store.active = False
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: CrewHubConfigEntry) -> None:
    """Entry deleted: drop the store, the secrets file and the card's Lovelace resource (unload keeps them)."""
    await _async_remove_resource(hass)
    secrets = await secret_store.async_get_secrets(hass)
    await secrets.async_remove_file()
    hass.data.pop(DATA_SECRETS, None)
    if (hub := hass.data.pop(DATA_WEBHOOK, None)) is not None:
        hub.shutdown()
    if (store := hass.data.pop(DATA_STORE, None)) is not None:
        store.active = False
        store.reset()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _lovelace_resources(hass: HomeAssistant) -> Any | None:
    """The Lovelace resource collection when it is in storage mode, else None.

    hass.data["lovelace"] is a LovelaceData dataclass in recent Home Assistant (resource_mode, resources); older
    versions used a dict ("mode"/"resource_mode", "resources"). Both are read defensively.
    """
    data = hass.data.get(LOVELACE_DOMAIN)
    if data is None:
        return None
    if isinstance(data, dict):
        mode = data.get("resource_mode") or data.get("mode")
        resources = data.get("resources")
    else:
        mode = getattr(data, "resource_mode", None) or getattr(data, "mode", None)
        resources = getattr(data, "resources", None)
    if mode != "storage" or resources is None:
        return None
    if not all(hasattr(resources, a) for a in ("async_items", "async_create_item", "async_update_item")):
        return None
    return resources


def _is_card_resource(item: dict) -> bool:
    return str(item.get("url", "")).split("?", 1)[0] == CARD_URL


async def _async_sync_resource(hass: HomeAssistant, url: str) -> bool:
    """Keep exactly one storage-mode Lovelace resource for the card, pointing at url. False when not possible."""
    resources = _lovelace_resources(hass)
    if resources is None:
        return False
    if hasattr(resources, "async_get_info"):
        await resources.async_get_info()  # loads the storage collection if it isn't yet
    elif not getattr(resources, "loaded", True):
        await resources.async_load()
        resources.loaded = True
    ours = [item for item in resources.async_items() if _is_card_resource(item)]
    if not ours:
        await resources.async_create_item({"res_type": "module", "url": url})
        _LOGGER.debug("Added Lovelace resource %s", url)
        return True
    keep, extra = ours[0], ours[1:]
    if keep.get("url") != url or keep.get("type") != "module":
        await resources.async_update_item(keep["id"], {"res_type": "module", "url": url})
        _LOGGER.debug("Updated Lovelace resource %s to %s", keep["id"], url)
    for item in extra:
        await resources.async_delete_item(item["id"])
        _LOGGER.debug("Removed duplicate Lovelace resource %s", item["id"])
    return True


async def _async_remove_resource(hass: HomeAssistant) -> None:
    """Delete the card's Lovelace resource(s); used when the config entry is removed."""
    try:
        resources = _lovelace_resources(hass)
        if resources is None or not hasattr(resources, "async_delete_item"):
            return
        if hasattr(resources, "async_get_info"):
            await resources.async_get_info()
        for item in [item for item in resources.async_items() if _is_card_resource(item)]:
            await resources.async_delete_item(item["id"])
    except Exception as err:  # noqa: BLE001 - removal must not fail on a Lovelace API change
        _LOGGER.warning("Could not remove the Crew Hub card's Lovelace resource: %s", err)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the card at /crew_hub/crew-hub-card.js and make the frontend load it.

    Storage-mode Lovelace: a managed Lovelace resource (survives in long-lived app shells such as the Companion app,
    which never re-read extra_js_url). Otherwise (YAML mode, no lovelace, or an error): add_extra_js_url. Never both,
    so two versions of the guarded customElements.define can't race. Decided once per Home Assistant run.
    """
    if hass.data.get(DATA_FRONTEND) or "http" not in hass.config.components or hass.http is None:
        return
    digest = await hass.async_add_executor_job(_file_hash, CARD_PATH)
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_PATH), True)])
    url = f"{CARD_URL}?v={VERSION}-{digest}"
    managed = False
    if LOVELACE_DOMAIN in hass.config.components:
        try:
            managed = await _async_sync_resource(hass, url)
        except Exception as err:  # noqa: BLE001 - fall back to extra_js_url on any Lovelace API surprise
            _LOGGER.warning("Could not manage the Crew Hub card's Lovelace resource, loading it via extra_js_url: %s", err)
    if not managed and "frontend" in hass.config.components:
        frontend.add_extra_js_url(hass, url)
    hass.data[DATA_FRONTEND] = True
