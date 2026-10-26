"""Config flow (MQTT prefix, single instance) and options flow (machines: id, name, command secret).

Secrets are stored in the config entry options (Home Assistant .storage). They are never logged, never pre-filled
back into a form, and never sent to the websocket API (the store only keeps {id: name}).
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_MACHINE_ID,
    CONF_MACHINE_NAME,
    CONF_MACHINE_SECRET,
    CONF_MACHINES,
    CONF_PREFIX,
    DEFAULT_PREFIX,
    DOMAIN,
    NAME,
)

# Same character set the reporter uses for topic segments (plugins/crew-hub/lib/crewhub/config.py _topic_safe).
SEGMENT_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
MIN_SECRET_LEN = 16
MAX_NAME_LEN = 80

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_TEXT = TextSelector()


class CrewHubConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors: dict[str, str] = {}
        if user_input is not None:
            prefix = str(user_input.get(CONF_PREFIX, "")).strip().strip("/").lower()
            if not SEGMENT_RE.match(prefix):
                errors[CONF_PREFIX] = "invalid_prefix"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=NAME, data={CONF_PREFIX: prefix}, options={CONF_MACHINES: []})
        schema = vol.Schema({vol.Required(CONF_PREFIX, default=DEFAULT_PREFIX): _TEXT})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CrewHubOptionsFlow()


class CrewHubOptionsFlow(OptionsFlow):
    """Menu: add / edit / remove a machine. Each change saves the options and reloads the entry."""

    def __init__(self) -> None:
        self._machines: list[dict[str, str]] | None = None
        self._edit_id: str | None = None

    def _load(self) -> list[dict[str, str]]:
        if self._machines is None:
            self._machines = [dict(m) for m in self.config_entry.options.get(CONF_MACHINES, []) if isinstance(m, dict)]
        return self._machines

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, CONF_MACHINES: self._load()})

    def _machine_selector(self) -> vol.Schema:
        options = [{"value": m[CONF_MACHINE_ID], "label": f"{m[CONF_MACHINE_NAME]} ({m[CONF_MACHINE_ID]})"} for m in self._load()]
        return vol.Schema({
            vol.Required(CONF_MACHINE_ID): SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))
        })

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        menu = ["add"]
        if self._load():
            menu += ["edit", "remove"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        machines = self._load()
        errors: dict[str, str] = {}
        if user_input is not None:
            mid = str(user_input.get(CONF_MACHINE_ID, "")).strip().lower()
            name = str(user_input.get(CONF_MACHINE_NAME) or "").strip()[:MAX_NAME_LEN] or mid
            secret = str(user_input.get(CONF_MACHINE_SECRET) or "")
            if not SEGMENT_RE.match(mid):
                errors[CONF_MACHINE_ID] = "invalid_machine_id"
            elif any(m[CONF_MACHINE_ID] == mid for m in machines):
                errors[CONF_MACHINE_ID] = "machine_exists"
            elif len(secret) < MIN_SECRET_LEN:
                errors[CONF_MACHINE_SECRET] = "secret_too_short"
            else:
                machines.append({CONF_MACHINE_ID: mid, CONF_MACHINE_NAME: name, CONF_MACHINE_SECRET: secret})
                return self._save()
        schema = vol.Schema({
            vol.Required(CONF_MACHINE_ID): _TEXT,
            vol.Optional(CONF_MACHINE_NAME): _TEXT,
            vol.Required(CONF_MACHINE_SECRET): _PASSWORD,
        })
        # Re-show what was typed on error, except the secret.
        suggested = {k: v for k, v in (user_input or {}).items() if k != CONF_MACHINE_SECRET}
        return self.async_show_form(
            step_id="add", data_schema=self.add_suggested_values_to_schema(schema, suggested), errors=errors
        )

    async def async_step_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_id = user_input[CONF_MACHINE_ID]
            return await self.async_step_edit_machine()
        return self.async_show_form(step_id="edit", data_schema=self._machine_selector())

    async def async_step_edit_machine(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        machine = next((m for m in self._load() if m[CONF_MACHINE_ID] == self._edit_id), None)
        if machine is None:
            return self.async_abort(reason="unknown_machine")
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_MACHINE_NAME) or "").strip()[:MAX_NAME_LEN]
            secret = str(user_input.get(CONF_MACHINE_SECRET) or "")
            if secret and len(secret) < MIN_SECRET_LEN:
                errors[CONF_MACHINE_SECRET] = "secret_too_short"
            else:
                machine[CONF_MACHINE_NAME] = name or machine[CONF_MACHINE_ID]
                if secret:  # empty keeps the current secret
                    machine[CONF_MACHINE_SECRET] = secret
                return self._save()
        schema = vol.Schema({
            vol.Optional(CONF_MACHINE_NAME): _TEXT,
            vol.Optional(CONF_MACHINE_SECRET): _PASSWORD,
        })
        return self.async_show_form(
            step_id="edit_machine",
            data_schema=self.add_suggested_values_to_schema(schema, {CONF_MACHINE_NAME: machine[CONF_MACHINE_NAME]}),
            errors=errors,
            description_placeholders={"machine": machine[CONF_MACHINE_ID]},
        )

    async def async_step_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            mid = user_input[CONF_MACHINE_ID]
            self._machines = [m for m in self._load() if m[CONF_MACHINE_ID] != mid]
            return self._save()
        return self.async_show_form(step_id="remove", data_schema=self._machine_selector())
