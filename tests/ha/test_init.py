"""crew_hub end to end in the HA harness: config flow, options, MQTT -> store -> websocket, entities, admin check."""

import hashlib
import json
import pathlib
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.crew_hub.const import CARD_URL, DATA_SECRETS, DATA_STORE, DOMAIN, VERSION

SECRET = "s3cret-s3cret-s3cret"
ROOT = pathlib.Path(__file__).resolve().parents[2]


async def _setup(hass: HomeAssistant, machines=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Crew Hub",
        data={"prefix": "crew"},
        options={"machines": machines if machines is not None else [{"id": "nas", "name": "NAS", "secret": SECRET}]},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# ---- config flow ----
async def test_config_flow_creates_entry(hass: HomeAssistant, mqtt_mock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"prefix": "bad/+"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"prefix": "invalid_prefix"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"prefix": "crew"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Crew Hub"
    assert result["data"] == {"prefix": "crew"}
    assert result["options"] == {"machines": []}
    await hass.async_block_till_done()

    # single instance
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_add_edit_remove_machine(hass: HomeAssistant, mqtt_mock) -> None:
    entry = await _setup(hass, machines=[])
    assert hass.states.get("binary_sensor.crew_hub_nas_online") is None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["add"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add"})
    assert result["step_id"] == "add"
    # legacy path: a typed secret (collapsed "advanced" section) instead of a pairing code
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "nas", "name": "NAS", "advanced": {"secret": "short"}}
    )
    assert result["errors"] == {"base": "secret_too_short"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "NAS", "name": "NAS", "advanced": {"secret": SECRET}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["machines"] == [{"id": "nas", "name": "NAS", "has_secret": True}]
    assert hass.data[DATA_SECRETS].secret("nas") == SECRET
    assert hass.states.get("binary_sensor.crew_hub_nas_online") is not None
    assert hass.states.get("binary_sensor.crew_hub_nas_online").state == "unknown"

    # duplicate id
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["add", "edit", "remove"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"id": "nas"})
    assert result["errors"] == {"id": "machine_exists"}

    # edit: empty secret keeps the old one
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"id": "nas"})
    assert result["step_id"] == "edit_machine"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"name": "The NAS"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["machines"] == [{"id": "nas", "name": "The NAS", "has_secret": True}]
    assert hass.data[DATA_SECRETS].secret("nas") == SECRET

    # remove: entity and registry entry go away
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "remove"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"id": "nas"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["machines"] == []
    assert hass.data[DATA_SECRETS].secret("nas") is None
    assert hass.states.get("binary_sensor.crew_hub_nas_online") is None
    assert er.async_get(hass).async_get("binary_sensor.crew_hub_nas_online") is None


# ---- MQTT -> store -> websocket ----
async def test_mqtt_to_websocket_subscribe(hass: HomeAssistant, mqtt_mock, hass_ws_client, nas_snapshot) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "crew_hub/subscribe"})
    msg = await client.receive_json()
    assert msg["success"], msg
    msg = await client.receive_json()
    assert msg["id"] == 1 and msg["type"] == "event"
    assert msg["event"] == {
        "type": "full",
        "machines": {"nas": {"status": "unknown", "snapshot": None, "receivedAt": None, "configured": True}},
    }

    async_fire_mqtt_message(hass, "crew/nas/status", "online", retain=True)
    await hass.async_block_till_done()
    msg = await client.receive_json()
    assert msg["event"]["type"] == "machine" and msg["event"]["id"] == "nas"
    assert msg["event"]["data"]["status"] == "online"

    async_fire_mqtt_message(hass, "crew/nas/snapshot", json.dumps(nas_snapshot), retain=True)
    await hass.async_block_till_done()
    msg = await client.receive_json()
    data = msg["event"]["data"]
    assert data["snapshot"] == nas_snapshot
    assert data["status"] == "online" and data["configured"] is True
    assert isinstance(data["receivedAt"], int)

    # bad JSON and oversize payloads are ignored (no event); an unconfigured machine still shows
    async_fire_mqtt_message(hass, "crew/nas/snapshot", "{not json")
    async_fire_mqtt_message(hass, "crew/nas/snapshot", json.dumps({"x": "a" * (300 * 1024)}))
    async_fire_mqtt_message(hass, "crew/mac/snapshot", json.dumps({"machine": {"id": "mac"}, "sessions": []}))
    await hass.async_block_till_done()
    msg = await client.receive_json()
    assert msg["event"]["id"] == "mac"
    assert msg["event"]["data"]["configured"] is False

    await client.send_json({"id": 2, "type": "crew_hub/get"})
    msg = await client.receive_json()
    assert msg["success"]
    assert set(msg["result"]["machines"]) == {"nas", "mac"}
    assert msg["result"]["machines"]["nas"]["snapshot"] == nas_snapshot
    assert SECRET not in json.dumps(msg)

    # unsubscribe cleanly
    store = hass.data[DATA_STORE]
    before = store.listener_count
    await client.send_json({"id": 3, "type": "unsubscribe_events", "subscription": 1})
    msg = await client.receive_json()
    assert msg["success"], msg
    assert store.listener_count == before - 1


async def test_websocket_refuses_non_admin(hass: HomeAssistant, mqtt_mock, hass_ws_client, hass_read_only_access_token) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    for i, cmd in ((1, "crew_hub/subscribe"), (2, "crew_hub/get")):
        await client.send_json({"id": i, "type": cmd})
        msg = await client.receive_json()
        assert not msg["success"]
        assert msg["error"]["code"] == "unauthorized"


async def test_websocket_not_loaded(hass: HomeAssistant, mqtt_mock, hass_ws_client) -> None:
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "crew_hub/subscribe"})
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "not_loaded"


# ---- entities ----
async def test_entities_follow_mqtt(hass: HomeAssistant, mqtt_mock, nas_snapshot) -> None:
    await _setup(hass)
    assert hass.states.get("sensor.crew_hub_working").state == "0"
    assert hass.states.get("binary_sensor.crew_hub_needs_me").state == "off"
    online = hass.states.get("binary_sensor.crew_hub_nas_online")
    assert online.state == "unknown"
    assert online.attributes["device_class"] == "connectivity"
    assert hass.states.get("binary_sensor.crew_hub_needs_me").attributes["device_class"] == "problem"

    async_fire_mqtt_message(hass, "crew/nas/status", "online")
    async_fire_mqtt_message(hass, "crew/nas/snapshot", json.dumps(nas_snapshot))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.crew_hub_nas_online").state == "on"
    assert hass.states.get("sensor.crew_hub_working").state == "1"
    needs = hass.states.get("binary_sensor.crew_hub_needs_me")
    assert needs.state == "on"
    assert needs.attributes["sessions"] == ["Home Assistant Mr Manager", "Music Assistant tidy-up"]

    # same snapshot again: no new state write
    last = hass.states.get("sensor.crew_hub_working").last_reported
    async_fire_mqtt_message(hass, "crew/nas/snapshot", json.dumps(nas_snapshot))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.crew_hub_working").last_reported == last

    # machine goes offline (LWT): its sessions stop counting
    async_fire_mqtt_message(hass, "crew/nas/status", "offline")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.crew_hub_nas_online").state == "off"
    assert hass.states.get("sensor.crew_hub_working").state == "0"
    assert hass.states.get("binary_sensor.crew_hub_needs_me").state == "off"


async def test_card_served_and_identical(hass: HomeAssistant, mqtt_mock, hass_client) -> None:
    assert await async_setup_component(hass, "http", {})
    await _setup(hass)
    client = await hass_client()
    resp = await client.get(CARD_URL)
    assert resp.status == 200
    body = await resp.read()
    assert body == (ROOT / "card" / "crew-hub-card.js").read_bytes()


# ---- Lovelace resource for the card ----
CARD_DIGEST = hashlib.sha256((ROOT / "custom_components" / "crew_hub" / "www" / "crew-hub-card.js").read_bytes()).hexdigest()[:12]
CARD_RESOURCE_URL = f"{CARD_URL}?v={VERSION}-{CARD_DIGEST}"
STALE_URL = "/crew_hub/crew-hub-card.js?v=0.1.0-07b1ae4aa5b4"
OTHER = {"id": "other1", "type": "module", "url": "/hacsfiles/mushroom/mushroom.js"}


def _resources(hass: HomeAssistant):
    return hass.data["lovelace"].resources


def _card_items(hass: HomeAssistant) -> list[dict]:
    return [i for i in _resources(hass).async_items() if i["url"].split("?")[0] == CARD_URL]


async def _setup_with_lovelace(hass: HomeAssistant, lovelace_conf: dict | None = None):
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "lovelace", {"lovelace": lovelace_conf or {}})
    hass.config.components.add("frontend")  # what the fallback checks; the real frontend isn't needed here
    with patch("custom_components.crew_hub.frontend.add_extra_js_url") as extra:
        entry = await _setup(hass)
    return entry, extra


async def test_storage_mode_creates_resource(hass: HomeAssistant, mqtt_mock, hass_storage) -> None:
    hass_storage["lovelace_resources"] = {"version": 1, "key": "lovelace_resources", "data": {"items": [OTHER]}}
    _, extra = await _setup_with_lovelace(hass)
    items = _card_items(hass)
    assert len(items) == 1
    assert items[0]["url"] == CARD_RESOURCE_URL
    assert items[0]["type"] == "module"
    assert any(i["id"] == "other1" for i in _resources(hass).async_items())
    extra.assert_not_called()


async def test_storage_mode_updates_stale_in_place_and_drops_duplicates(
    hass: HomeAssistant, mqtt_mock, hass_storage
) -> None:
    hass_storage["lovelace_resources"] = {
        "version": 1,
        "key": "lovelace_resources",
        "data": {
            "items": [
                {"id": "mrmanager1", "type": "module", "url": STALE_URL},
                OTHER,
                {"id": "dup2", "type": "module", "url": "/crew_hub/crew-hub-card.js"},
                {"id": "dup3", "type": "js", "url": "/crew_hub/crew-hub-card.js?v=0.0.9-aaaaaaaaaaaa"},
            ]
        },
    }
    _, extra = await _setup_with_lovelace(hass)
    items = _card_items(hass)
    assert items == [{"id": "mrmanager1", "type": "module", "url": CARD_RESOURCE_URL}]
    assert [i["id"] for i in _resources(hass).async_items()] == ["mrmanager1", "other1"]
    extra.assert_not_called()


async def test_storage_mode_current_resource_left_alone(hass: HomeAssistant, mqtt_mock, hass_storage) -> None:
    hass_storage["lovelace_resources"] = {
        "version": 1,
        "key": "lovelace_resources",
        "data": {"items": [{"id": "cur", "type": "module", "url": CARD_RESOURCE_URL}]},
    }
    with patch(
        "homeassistant.components.lovelace.resources.ResourceStorageCollection.async_update_item"
    ) as update:
        await _setup_with_lovelace(hass)
    update.assert_not_called()
    assert _card_items(hass) == [{"id": "cur", "type": "module", "url": CARD_RESOURCE_URL}]


async def test_yaml_mode_falls_back_to_extra_js_url(hass: HomeAssistant, mqtt_mock) -> None:
    _, extra = await _setup_with_lovelace(hass, {"resource_mode": "yaml"})
    extra.assert_called_once_with(hass, CARD_RESOURCE_URL)
    assert _card_items(hass) == []


async def test_resource_error_falls_back_to_extra_js_url(hass: HomeAssistant, mqtt_mock, caplog) -> None:
    with patch(
        "homeassistant.components.lovelace.resources.ResourceStorageCollection.async_create_item",
        side_effect=RuntimeError("boom"),
    ):
        _, extra = await _setup_with_lovelace(hass)
    extra.assert_called_once_with(hass, CARD_RESOURCE_URL)
    assert "extra_js_url: boom" in caplog.text
    assert "Traceback" not in caplog.text


async def test_unload_keeps_resource_remove_deletes_it(hass: HomeAssistant, mqtt_mock, hass_storage) -> None:
    hass_storage["lovelace_resources"] = {"version": 1, "key": "lovelace_resources", "data": {"items": [OTHER]}}
    entry, _ = await _setup_with_lovelace(hass)
    assert len(_card_items(hass)) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(_card_items(hass)) == 1

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _card_items(hass) == []
    assert [i["id"] for i in _resources(hass).async_items()] == ["other1"]
