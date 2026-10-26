"""crew_hub end to end in the HA harness: config flow, options, MQTT -> store -> websocket, entities, admin check."""

import json
import pathlib

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.crew_hub.const import CARD_URL, DATA_STORE, DOMAIN

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
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "nas", "name": "NAS", "secret": "short"}
    )
    assert result["errors"] == {"secret": "secret_too_short"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"id": "NAS", "name": "NAS", "secret": SECRET}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["machines"] == [{"id": "nas", "name": "NAS", "secret": SECRET}]
    assert hass.states.get("binary_sensor.crew_hub_nas_online") is not None
    assert hass.states.get("binary_sensor.crew_hub_nas_online").state == "unknown"

    # duplicate id
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["add", "edit", "remove"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"id": "nas", "secret": SECRET})
    assert result["errors"] == {"id": "machine_exists"}

    # edit: empty secret keeps the old one
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "edit"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"id": "nas"})
    assert result["step_id"] == "edit_machine"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"name": "The NAS"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["machines"] == [{"id": "nas", "name": "The NAS", "secret": SECRET}]

    # remove: entity and registry entry go away
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "remove"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"id": "nas"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["machines"] == []
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
    assert needs.attributes["sessions"] == ["Home Assistant Mr Manager"]

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
