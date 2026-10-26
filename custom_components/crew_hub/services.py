"""crew_hub.* services: the same signed commands as the crew_hub/command websocket (commands.py).

Only calls made by an admin user are allowed. Calls without a user in their context (automations, scripts started
by triggers, other integrations) are refused on purpose: a command can run work on a machine, so it needs a person.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv

from .commands import (
    DECISIONS,
    MAX_ID,
    MAX_MESSAGE,
    MAX_NAME,
    MAX_PROMPT,
    MAX_TEXT,
    async_send_command,
)
from .const import DOMAIN

_MACHINE = {vol.Required("machine"): vol.All(cv.string, vol.Length(min=1, max=64))}
_SESSION = {vol.Required("session"): vol.All(cv.string, vol.Length(min=1, max=MAX_ID))}

# service name -> (protocol action, schema)
SERVICES: dict[str, tuple[str, vol.Schema]] = {
    "send_message": ("send", vol.Schema({
        **_MACHINE, **_SESSION,
        vol.Required("text"): vol.All(cv.string, vol.Length(min=1, max=MAX_TEXT)),
        vol.Optional("urgent", default=False): cv.boolean,
    })),
    "answer": ("answer", vol.Schema({
        **_MACHINE, **_SESSION,
        vol.Required("requestId"): vol.All(cv.string, vol.Length(min=1, max=MAX_ID)),
        vol.Required("decision"): vol.In(DECISIONS),
        vol.Optional("message"): vol.All(cv.string, vol.Length(max=MAX_MESSAGE)),
        vol.Optional("answers"): {cv.string: cv.string},
    })),
    "interrupt": ("interrupt", vol.Schema({**_MACHINE, **_SESSION})),
    "stop": ("stop", vol.Schema({**_MACHINE, **_SESSION})),
    "start_session": ("start", vol.Schema({
        **_MACHINE,
        vol.Required("folder"): vol.All(cv.string, vol.Length(min=1, max=MAX_ID)),
        vol.Required("model"): vol.All(cv.string, vol.Length(min=1, max=MAX_ID)),
        vol.Required("permissionMode"): vol.All(cv.string, vol.Length(min=1, max=MAX_ID)),
        vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=MAX_NAME)),
        vol.Required("prompt"): vol.All(cv.string, vol.Length(min=1, max=MAX_PROMPT)),
    })),
}


async def _async_require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    user_id = call.context.user_id
    if not user_id:
        raise ServiceValidationError(
            "Crew Hub commands must come from an admin user (dashboard or Developer tools); "
            "automations and scripts without a user can't send them"
        )
    user = await hass.auth.async_get_user(user_id)
    if user is None or not user.is_admin:
        raise Unauthorized(context=call.context)


def async_register(hass: HomeAssistant) -> None:
    for service, (action, schema) in SERVICES.items():

        async def _handle(call: ServiceCall, action: str = action) -> ServiceResponse:
            await _async_require_admin(hass, call)
            data: dict[str, Any] = dict(call.data)
            machine = data.pop("machine")
            result = await async_send_command(hass, machine, action, data)
            if not result.get("ok"):
                raise HomeAssistantError(f"Crew Hub {action} on {machine} failed: {result.get('error')}")
            return result if call.return_response else None

        hass.services.async_register(DOMAIN, service, _handle, schema=schema, supports_response=SupportsResponse.OPTIONAL)
