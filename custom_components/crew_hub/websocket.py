"""Admin-only websocket API feeding the crew-hub-card.

crew_hub/subscribe: result, then {"type": "full", "machines": {...}}, then {"type": "machine", "id", "data"} per change.
crew_hub/get: the full machine map as the result.
crew_hub/command: {machine, action, args} -> signed command over MQTT or the webhook queue (commands.py); result {ok, error?, result?}.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .commands import async_send_command
from .const import DATA_STORE, WS_COMMAND, WS_GET, WS_SUBSCRIBE
from .store import CrewStore


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_get)
    websocket_api.async_register_command(hass, ws_command)


def _store(hass: HomeAssistant) -> CrewStore | None:
    store = hass.data.get(DATA_STORE)
    return store if store is not None and store.active else None


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_SUBSCRIBE})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    store = _store(hass)
    if store is None:
        connection.send_error(msg["id"], "not_loaded", "Crew Hub is not set up")
        return

    @callback
    def forward(event: dict) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], event))

    connection.subscriptions[msg["id"]] = store.add_listener(forward)
    connection.send_result(msg["id"])
    forward({"type": "full", "machines": store.as_dict()})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_GET})
@callback
def ws_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    store = _store(hass)
    if store is None:
        connection.send_error(msg["id"], "not_loaded", "Crew Hub is not set up")
        return
    connection.send_result(msg["id"], {"machines": store.as_dict()})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_COMMAND,
    vol.Required("machine"): str,
    vol.Required("action"): str,
    vol.Optional("args", default=dict): dict,
})
@websocket_api.async_response
async def ws_command(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Every outcome (refused, no ack, daemon error, ok) is a result {ok, error?, result?} so the card shows it inline."""
    if _store(hass) is None:
        connection.send_error(msg["id"], "not_loaded", "Crew Hub is not set up")
        return
    result = await async_send_command(hass, msg["machine"], msg["action"], msg["args"])
    connection.send_result(msg["id"], result)
