"""Config flow (MQTT prefix, single instance) and options flow (machines: id, name, transport; secrets generated).

Home Assistant generates every machine's secret and shows it once, as a pairing code (docs/WEBHOOK.md "Pairing"):
- webhook machines: webhook id + secret, "pair" step, code {"t": "webhook", "u", "w", "s", "m"};
- MQTT machines: command secret, "pair_mqtt" step, code {"t": "mqtt", "m", "s"}. Typing a secret yourself is still
  possible in the collapsed "advanced" section (legacy path; no code is shown then).
Re-pair (edit a machine) makes new ones and shows a new code once; the old secret stops working at once.
Secrets live in the private Store of secret_store.py, never in the config entry (options only get has_secret). They
are never logged, never pre-filled back into a form, never sent to the websocket API (the store only keeps {id: name}).
The code exists only in the pair step's description placeholders.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import secret_store
from .const import (
    CONF_ADVANCED,
    CONF_MACHINE_HAS_SECRET,
    CONF_MACHINE_ID,
    CONF_MACHINE_NAME,
    CONF_MACHINE_PUBLIC_URL,
    CONF_MACHINE_SECRET,
    CONF_MACHINE_TRANSPORT,
    CONF_MACHINES,
    CONF_PREFIX,
    DEFAULT_PREFIX,
    DOMAIN,
    NAME,
    TRANSPORT_MQTT,
    TRANSPORT_WEBHOOK,
    TRANSPORTS,
)
from .webhook_transport import mqtt_pairing_code, normalize_public_url, pairing_code

# Same character set the reporter uses for topic segments (plugins/crew-hub/lib/crewhub/config.py _topic_safe).
SEGMENT_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
MIN_SECRET_LEN = 16
MAX_NAME_LEN = 80

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_TEXT = TextSelector()
_URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
_TRANSPORT = SelectSelector(
    SelectSelectorConfig(options=list(TRANSPORTS), mode=SelectSelectorMode.LIST, translation_key="transport")
)
CONF_REPAIR = "repair"


_ADVANCED_SECRET = section(vol.Schema({vol.Optional(CONF_MACHINE_SECRET): _PASSWORD}), {"collapsed": True})


def _new_secret() -> str:
    """WEBHOOK.md: secret = token_urlsafe(32), for both transports."""
    return secrets.token_urlsafe(32)


def _new_webhook_id() -> str:
    """WEBHOOK.md: webhookId = token_hex(32)."""
    return secrets.token_hex(32)


def _typed_secret(user_input: dict[str, Any]) -> str:
    """The optional secret from the collapsed "advanced" section."""
    advanced = user_input.get(CONF_ADVANCED)
    return str((advanced or {}).get(CONF_MACHINE_SECRET) or "") if isinstance(advanced, dict) else ""


def _is_webhook(machine: dict) -> bool:
    return machine.get(CONF_MACHINE_TRANSPORT) == TRANSPORT_WEBHOOK


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
        self._machines: list[dict[str, Any]] | None = None
        self._secrets: secret_store.SecretStore | None = None
        self._edit_id: str | None = None
        self._pair_machine: str | None = None  # machine whose new pairing code the pair step shows

    def _load(self) -> list[dict[str, Any]]:
        if self._machines is None:
            self._machines = [
                secret_store.public_machine(m, bool(m.get(CONF_MACHINE_HAS_SECRET)))
                for m in self.config_entry.options.get(CONF_MACHINES, []) if isinstance(m, dict)
            ]
        return self._machines

    def _find(self, mid: str | None) -> dict[str, Any] | None:
        return next((m for m in self._load() if m[CONF_MACHINE_ID] == mid), None)

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, CONF_MACHINES: self._load()})

    def _persist(self, reload: bool = False) -> None:
        """Save the machines now, before the flow ends (the entry reloads and registers the new webhook).

        reload: the Store changed in a way the running entry must pick up (new webhook id or secret) even when the
        options themselves stay the same (a re-pair only changes the Store).
        """
        changed = self.hass.config_entries.async_update_entry(
            self.config_entry, options={**self.config_entry.options, CONF_MACHINES: self._load()}
        )
        if reload and not changed and self.config_entry.state is ConfigEntryState.LOADED:
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    def _machine_selector(self) -> vol.Schema:
        options = [{"value": m[CONF_MACHINE_ID], "label": f"{m[CONF_MACHINE_NAME]} ({m[CONF_MACHINE_ID]})"} for m in self._load()]
        return vol.Schema({
            vol.Required(CONF_MACHINE_ID): SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))
        })

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # An entry from 0.3.0 or earlier that never loaded since the update still has secrets in its options.
        await secret_store.async_migrate_entry(self.hass, self.config_entry)
        self._secrets = await secret_store.async_get_secrets(self.hass)
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
            transport = user_input.get(CONF_MACHINE_TRANSPORT) or TRANSPORT_MQTT
            typed = _typed_secret(user_input)
            public_url = normalize_public_url(user_input.get(CONF_MACHINE_PUBLIC_URL))
            if not SEGMENT_RE.match(mid):
                errors[CONF_MACHINE_ID] = "invalid_machine_id"
            elif any(m[CONF_MACHINE_ID] == mid for m in machines):
                errors[CONF_MACHINE_ID] = "machine_exists"
            elif transport not in TRANSPORTS:
                errors[CONF_MACHINE_TRANSPORT] = "invalid_transport"
            elif transport == TRANSPORT_WEBHOOK:
                if public_url is None:
                    errors[CONF_MACHINE_PUBLIC_URL] = "invalid_public_url"
                else:
                    # A typed secret is ignored: webhook machines always get generated credentials.
                    await self._secrets.async_set(mid, _new_secret(), _new_webhook_id())
                    machines.append({
                        CONF_MACHINE_ID: mid, CONF_MACHINE_NAME: name, CONF_MACHINE_TRANSPORT: TRANSPORT_WEBHOOK,
                        CONF_MACHINE_PUBLIC_URL: public_url, CONF_MACHINE_HAS_SECRET: True,
                    })
                    self._pair_machine = mid
                    self._persist()
                    return await self.async_step_pair()
            elif typed and len(typed) < MIN_SECRET_LEN:
                errors["base"] = "secret_too_short"
            else:
                await self._secrets.async_set(mid, typed or _new_secret())
                machines.append({CONF_MACHINE_ID: mid, CONF_MACHINE_NAME: name, CONF_MACHINE_HAS_SECRET: True})
                if typed:  # legacy path: the person already has the secret, no code
                    return self._save()
                self._pair_machine = mid
                self._persist()
                return await self.async_step_pair_mqtt()
        schema = vol.Schema({
            vol.Required(CONF_MACHINE_ID): _TEXT,
            vol.Optional(CONF_MACHINE_NAME): _TEXT,
            vol.Optional(CONF_MACHINE_TRANSPORT, default=TRANSPORT_MQTT): _TRANSPORT,
            vol.Optional(CONF_MACHINE_PUBLIC_URL): _URL,
            vol.Optional(CONF_ADVANCED): _ADVANCED_SECRET,
        })
        # Re-show what was typed on error, except the secret.
        suggested = {k: v for k, v in (user_input or {}).items() if k not in (CONF_MACHINE_SECRET, CONF_ADVANCED)}
        return self.async_show_form(
            step_id="add", data_schema=self.add_suggested_values_to_schema(schema, suggested), errors=errors
        )

    async def async_step_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_id = user_input[CONF_MACHINE_ID]
            return await self.async_step_edit_machine()
        return self.async_show_form(step_id="edit", data_schema=self._machine_selector())

    def _show_code(self, step_id: str, code: str | None, machine: str) -> ConfigFlowResult:
        if code is None:
            return self.async_abort(reason="unknown_machine")
        return self.async_show_form(
            step_id=step_id, data_schema=vol.Schema({}),
            description_placeholders={"machine": machine, "code": code},
        )

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Webhook machine: show the new pairing code once (a form without fields that the user confirms).

        The machine is already saved and its webhook registered (_persist), so crew-hub-pair's first hello works while
        the code is on screen. The code lives only in this flow's result; it is never logged or stored as such (the
        Store keeps the webhook id and secret it is made of).
        """
        machine = self._find(self._pair_machine)
        if machine is None:
            return self.async_abort(reason="unknown_machine")
        if user_input is not None:
            return self._save()
        mid = machine[CONF_MACHINE_ID]
        wid, secret = self._secrets.webhook_id(mid), self._secrets.secret(mid)
        code = pairing_code(machine[CONF_MACHINE_PUBLIC_URL], wid, secret, mid) if wid and secret else None
        return self._show_code("pair", code, mid)

    async def async_step_pair_mqtt(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """MQTT machine: show the new command secret once, as a pairing code for crew-hub-pair."""
        machine = self._find(self._pair_machine)
        if machine is None:
            return self.async_abort(reason="unknown_machine")
        if user_input is not None:
            return self._save()
        mid = machine[CONF_MACHINE_ID]
        secret = self._secrets.secret(mid)
        return self._show_code("pair_mqtt", mqtt_pairing_code(secret, mid) if secret else None, mid)

    async def async_step_edit_machine(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """MQTT machine: name, re-pair (new generated secret + code), or a typed secret (advanced section)."""
        machine = self._find(self._edit_id)
        if machine is None:
            return self.async_abort(reason="unknown_machine")
        if _is_webhook(machine):
            return await self.async_step_edit_webhook()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_MACHINE_NAME) or "").strip()[:MAX_NAME_LEN]
            typed = _typed_secret(user_input)
            repair = bool(user_input.get(CONF_REPAIR))
            if typed and repair:
                errors["base"] = "secret_and_repair"
            elif typed and len(typed) < MIN_SECRET_LEN:
                errors["base"] = "secret_too_short"
            else:
                machine[CONF_MACHINE_NAME] = name or machine[CONF_MACHINE_ID]
                if repair:
                    await self._secrets.async_set(machine[CONF_MACHINE_ID], _new_secret())
                    machine[CONF_MACHINE_HAS_SECRET] = True
                    self._pair_machine = machine[CONF_MACHINE_ID]
                    self._persist()
                    return await self.async_step_pair_mqtt()
                if typed:  # empty keeps the current secret
                    await self._secrets.async_set(machine[CONF_MACHINE_ID], typed)
                    machine[CONF_MACHINE_HAS_SECRET] = True
                return self._save()
        schema = vol.Schema({
            vol.Optional(CONF_MACHINE_NAME): _TEXT,
            vol.Optional(CONF_REPAIR, default=False): BooleanSelector(),
            vol.Optional(CONF_ADVANCED): _ADVANCED_SECRET,
        })
        return self.async_show_form(
            step_id="edit_machine",
            data_schema=self.add_suggested_values_to_schema(schema, {CONF_MACHINE_NAME: machine[CONF_MACHINE_NAME]}),
            errors=errors,
            description_placeholders={"machine": machine[CONF_MACHINE_ID]},
        )

    async def async_step_edit_webhook(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Webhook machine: name, public URL, re-pair (new webhook id + secret; the old ones stop working)."""
        machine = self._find(self._edit_id)
        if machine is None:
            return self.async_abort(reason="unknown_machine")
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_MACHINE_NAME) or "").strip()[:MAX_NAME_LEN]
            raw_url = user_input.get(CONF_MACHINE_PUBLIC_URL)
            public_url = normalize_public_url(raw_url) if raw_url else machine.get(CONF_MACHINE_PUBLIC_URL)
            if public_url is None:
                errors[CONF_MACHINE_PUBLIC_URL] = "invalid_public_url"
            else:
                machine[CONF_MACHINE_NAME] = name or machine[CONF_MACHINE_ID]
                machine[CONF_MACHINE_PUBLIC_URL] = public_url
                if user_input.get(CONF_REPAIR):
                    await self._secrets.async_set(machine[CONF_MACHINE_ID], _new_secret(), _new_webhook_id())
                    machine[CONF_MACHINE_HAS_SECRET] = True
                    self._pair_machine = machine[CONF_MACHINE_ID]
                    self._persist(reload=True)  # the options may be unchanged; the old webhook must go now
                    return await self.async_step_pair()
                return self._save()
        schema = vol.Schema({
            vol.Optional(CONF_MACHINE_NAME): _TEXT,
            vol.Optional(CONF_MACHINE_PUBLIC_URL): _URL,
            vol.Optional(CONF_REPAIR, default=False): BooleanSelector(),
        })
        suggested = {CONF_MACHINE_NAME: machine[CONF_MACHINE_NAME],
                     CONF_MACHINE_PUBLIC_URL: machine.get(CONF_MACHINE_PUBLIC_URL, "")}
        return self.async_show_form(
            step_id="edit_webhook",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={"machine": machine[CONF_MACHINE_ID]},
        )

    async def async_step_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            mid = user_input[CONF_MACHINE_ID]
            self._machines = [m for m in self._load() if m[CONF_MACHINE_ID] != mid]
            await self._secrets.async_remove(mid)
            return self._save()
        return self.async_show_form(step_id="remove", data_schema=self._machine_selector())
