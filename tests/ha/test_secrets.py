"""0.3.1: machine secrets live in a private Store (crew_hub.secrets), never in the config entry.

Migration of 0.3.0 entries (secrets in the options), no secret in any config-entry read, the options flow never shows a
stored secret, MQTT pairing codes and re-pair, diagnostics redaction, Store cleanup on machine/entry removal.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message, flush_store
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.crew_hub import diagnostics, secret_store
from custom_components.crew_hub.const import DATA_SECRETS, DOMAIN

SECRET = "s3cret-s3cret-s3cret"  # the NAS's real secret stands in here: 0.3.0 kept it in the options
MAC_SECRET = "mac-secret-mac-secret-mac-secret-0123456789"
MAC_HOOK = "a" * 64
LEGACY_NAS = {"id": "nas", "name": "NAS", "secret": SECRET}
LEGACY_MAC = {"id": "mac", "name": "MacBook", "transport": "webhook", "public_url": "https://hass.example.com",
              "webhook_id": MAC_HOOK, "secret": MAC_SECRET}
NAS = {"id": "nas", "name": "NAS", "has_secret": True}
MAC = {"id": "mac", "name": "MacBook", "transport": "webhook", "public_url": "https://hass.example.com",
       "has_secret": True}
MANAGER = "4d2b9c71-8a0e-4f3b-b5d6-91c7e2a0f4b8"  # fixture session with controls send+answer
KEY = "crew_hub.secrets"


# ---- helpers ----
def _entry(machines) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, title="Crew Hub", data={"prefix": "crew"}, unique_id=DOMAIN,
                           options={"machines": machines})


async def _setup(hass: HomeAssistant, machines=None) -> MockConfigEntry:
    assert await async_setup_component(hass, "http", {})
    entry = _entry(machines if machines is not None else [LEGACY_NAS, LEGACY_MAC])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _decode(code: str) -> dict:
    assert code.startswith("crewhub1:")
    body = code[len("crewhub1:"):]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _cmd_sig(env: dict, secret: str) -> str:
    body = {k: env[k] for k in ("v", "id", "ts", "action", "args")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(secret.encode(), canon, hashlib.sha256).hexdigest()


def _req_sig(secret: str, req: dict) -> str:
    body = {k: req[k] for k in ("v", "m", "ts", "nonce", "type", "data")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(secret.encode(), canon, hashlib.sha256).hexdigest()


def _hello(m: str, secret: str) -> dict:
    req = {"v": 1, "m": m, "ts": int(time.time() * 1000), "nonce": str(uuid.uuid4()), "type": "hello", "data": {}}
    req["sig"] = _req_sig(secret, req)
    return req


async def _online(hass: HomeAssistant, mid: str, snapshot: dict) -> None:
    async_fire_mqtt_message(hass, f"crew/{mid}/status", "online")
    async_fire_mqtt_message(hass, f"crew/{mid}/snapshot", json.dumps(snapshot))
    await hass.async_block_till_done()


async def _send_and_capture(hass: HomeAssistant, mid: str) -> dict:
    """Send a command to an MQTT machine; return the published envelope (acked so the call returns)."""
    from custom_components.crew_hub import commands

    published = []

    async def publish(hass_, topic, payload, qos=0, retain=False, **kw):
        env = json.loads(payload)
        published.append(env)
        hass_.loop.call_soon(async_fire_mqtt_message, hass_, f"crew/{mid}/ack", json.dumps({"id": env["id"], "ok": True}))

    with patch("custom_components.crew_hub.commands.mqtt.async_publish", publish):
        res = await commands.async_send_command(hass, mid, "send", {"session": MANAGER, "text": "hi"})
    assert res == {"ok": True}
    assert len(published) == 1
    return published[0]


def _assert_nowhere(text: str, *secrets: str) -> None:
    for s in secrets:
        assert s not in text


async def _entry_reads(hass: HomeAssistant, entry, hass_ws_client) -> str:
    """Everything the config-entry APIs and the config entry file can show, as one string."""
    await flush_store(hass.config_entries._store)

    assert await async_setup_component(hass, "config", {})
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "config_entries/get", "domain": DOMAIN})
    listed = await ws.receive_json()
    assert listed["success"], listed
    await ws.send_json({"id": 2, "type": "config_entries/get_single", "entry_id": entry.entry_id})
    single = await ws.receive_json()
    assert single["success"], single
    return "\n".join([json.dumps(entry.as_dict(), default=str), json.dumps(dict(entry.options)),
                      json.dumps(listed), json.dumps(single)])


# ---- migration ----
async def test_migration_moves_secrets_out_of_the_options(
    hass, mqtt_mock, hass_storage, hass_client_no_auth, nas_snapshot
) -> None:
    reload_listener = AsyncMock()
    with patch("custom_components.crew_hub._async_options_updated", reload_listener):
        entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    reload_listener.assert_not_called()  # no reload loop: the options were rewritten before the listener existed

    assert entry.options["machines"] == [NAS, MAC]
    _assert_nowhere(json.dumps(entry.as_dict(), default=str), SECRET, MAC_SECRET, MAC_HOOK)
    stored = hass_storage[KEY]
    assert stored["version"] == 1 and stored["key"] == KEY
    assert stored["data"] == {"nas": {"secret": SECRET}, "mac": {"secret": MAC_SECRET, "webhookId": MAC_HOOK}}
    assert hass.data[DATA_SECRETS]._store._private is True  # written 0600

    # the MQTT machine still signs with the same secret, the webhook machine still answers on the same webhook
    await _online(hass, "nas", nas_snapshot)
    env = await _send_and_capture(hass, "nas")
    assert env["sig"] == _cmd_sig(env, SECRET)
    client = await hass_client_no_auth()
    resp = await client.post(f"/api/webhook/{MAC_HOOK}", data=json.dumps(_hello("mac", MAC_SECRET)))
    assert resp.status == 200 and await resp.json() == {"ok": True}

    # a reload (options change elsewhere, or HA restart) doesn't migrate again
    with patch.object(hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry) as upd, \
            patch.object(secret_store.SecretStore, "async_set_many") as set_many:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    upd.assert_not_called()
    set_many.assert_not_called()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.options["machines"] == [NAS, MAC]


async def test_secrets_survive_a_restart(hass, mqtt_mock, hass_storage, nas_snapshot) -> None:
    """A new Home Assistant run loads the secrets from the Store file, not from the (clean) options."""
    entry = await _setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.data.pop(DATA_SECRETS)  # what a restart forgets
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _online(hass, "nas", nas_snapshot)
    env = await _send_and_capture(hass, "nas")
    assert env["sig"] == _cmd_sig(env, SECRET)
    assert entry.options["machines"] == [NAS, MAC]


async def test_half_finished_migration_completes(hass, mqtt_mock, hass_storage) -> None:
    """Store written but options not yet cleaned (crash in between): the next setup finishes the job; the options'
    value wins over an older Store value; Store entries of machines that are gone are dropped."""
    hass_storage[KEY] = {"version": 1, "minor_version": 1, "key": KEY,
                         "data": {"nas": {"secret": "older-secret-older-secret"}, "gone": {"secret": "x" * 20}}}
    entry = await _setup(hass)
    assert entry.options["machines"] == [NAS, MAC]
    assert hass_storage[KEY]["data"] == {"nas": {"secret": SECRET}, "mac": {"secret": MAC_SECRET, "webhookId": MAC_HOOK}}


async def test_migration_before_mqtt_is_up(hass, mqtt_mock, hass_storage) -> None:
    """Secrets leave the options even when setup has to wait for MQTT."""
    entry = _entry([LEGACY_NAS])
    entry.add_to_hass(hass)
    with patch("custom_components.crew_hub.mqtt.async_wait_for_mqtt_client", return_value=False):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.options["machines"] == [NAS]
    assert hass_storage[KEY]["data"] == {"nas": {"secret": SECRET}}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow_migrates_an_unloaded_entry(hass, mqtt_mock, hass_storage) -> None:
    entry = _entry([LEGACY_NAS])
    entry.add_to_hass(hass)  # never set up (e.g. disabled)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert entry.options["machines"] == [NAS]
    assert hass_storage[KEY]["data"] == {"nas": {"secret": SECRET}}
    _assert_nowhere(json.dumps(result, default=str), SECRET)


# ---- no secret in any config-entry read ----
async def test_config_entry_reads_hold_no_secret(hass, mqtt_mock, hass_storage, hass_ws_client) -> None:
    entry = await _setup(hass)
    flow = hass.config_entries.options
    # one generated MQTT secret and one webhook pair on top of the migrated ones
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add"})
    result = await flow.async_configure(result["flow_id"], {"id": "pc"})
    mqtt_code = result["description_placeholders"]["code"]
    await flow.async_configure(result["flow_id"], {})
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add"})
    result = await flow.async_configure(result["flow_id"], {"id": "laptop", "transport": "webhook",
                                                            "public_url": "https://hass.example.com"})
    hook_code = result["description_placeholders"]["code"]
    await flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    secrets = [SECRET, MAC_SECRET, MAC_HOOK, mqtt_code, hook_code, _decode(mqtt_code)["s"], _decode(hook_code)["s"],
               _decode(hook_code)["w"]]
    text = await _entry_reads(hass, entry, hass_ws_client)
    _assert_nowhere(text, *secrets)
    _assert_nowhere(json.dumps(hass_storage["core.config_entries"]), *secrets)
    assert '"has_secret": true' in json.dumps(hass_storage["core.config_entries"])
    for m in entry.options["machines"]:
        assert set(m) <= {"id", "name", "transport", "public_url", "has_secret"}
        assert m["has_secret"] is True


# ---- the options flow never shows a stored secret ----
async def test_flow_never_shows_a_stored_secret(hass, mqtt_mock, hass_storage) -> None:
    entry = await _setup(hass)
    flow = hass.config_entries.options
    shown = []
    result = await flow.async_init(entry.entry_id)
    shown.append(result)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add"})
    shown.append(result)
    result = await flow.async_configure(result["flow_id"], {"id": "nas"})  # error: re-shown form
    assert result["errors"] == {"id": "machine_exists"}
    shown.append(result)
    for mid, step in (("nas", "edit_machine"), ("mac", "edit_webhook")):
        result = await flow.async_init(entry.entry_id)
        result = await flow.async_configure(result["flow_id"], {"next_step_id": "edit"})
        shown.append(result)
        result = await flow.async_configure(result["flow_id"], {"id": mid})
        assert result["step_id"] == step
        shown.append(result)
        if mid == "nas":
            result = await flow.async_configure(result["flow_id"], {"name": "NAS", "advanced": {"secret": "short"}})
            assert result["errors"] == {"base": "secret_too_short"}
            shown.append(result)
            result = await flow.async_configure(result["flow_id"], {"repair": True,
                                                                    "advanced": {"secret": "x" * 20}})
            assert result["errors"] == {"base": "secret_and_repair"}
            shown.append(result)
        flow.async_abort(result["flow_id"])
    text = "\n".join(json.dumps(r, default=str) for r in shown)
    for r in shown:  # suggested values live inside the schema objects, so look there too
        if r.get("data_schema") is not None:
            text += repr([(k, getattr(k, "description", None)) for k in r["data_schema"].schema])
    _assert_nowhere(text, SECRET, MAC_SECRET, MAC_HOOK)
    assert hass.data[DATA_SECRETS].secret("nas") == SECRET  # nothing changed by the refused edits


# ---- MQTT pairing code and re-pair ----
async def test_add_mqtt_machine_shows_pairing_code(hass, mqtt_mock, hass_storage, nas_snapshot) -> None:
    entry = await _setup(hass, machines=[])
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add"})
    result = await flow.async_configure(result["flow_id"], {"id": "pc", "name": "Gaming PC", "transport": "mqtt"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pair_mqtt"
    assert result["data_schema"].schema == {}
    code = result["description_placeholders"]["code"]
    paired = _decode(code)
    assert paired.keys() == {"t", "m", "s"} and paired["t"] == "mqtt" and paired["m"] == "pc"
    assert len(paired["s"]) >= 40
    await hass.async_block_till_done()
    # saved while the code is on screen (closing the dialog keeps the machine; re-pair for a new code)
    assert entry.options["machines"] == [{"id": "pc", "name": "Gaming PC", "has_secret": True}]
    assert hass.data[DATA_SECRETS].secret("pc") == paired["s"]
    result = await flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    _assert_nowhere(json.dumps(result, default=str), code, paired["s"])
    await hass.async_block_till_done()

    await _online(hass, "pc", nas_snapshot)
    env = await _send_and_capture(hass, "pc")
    assert env["sig"] == _cmd_sig(env, paired["s"])


async def test_mqtt_repair_replaces_the_secret(hass, mqtt_mock, hass_storage, nas_snapshot) -> None:
    entry = await _setup(hass)
    await _online(hass, "nas", nas_snapshot)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "edit"})
    result = await flow.async_configure(result["flow_id"], {"id": "nas"})
    result = await flow.async_configure(result["flow_id"], {"name": "NAS", "repair": True})
    assert result["step_id"] == "pair_mqtt"
    paired = _decode(result["description_placeholders"]["code"])
    assert paired["m"] == "nas" and paired["s"] != SECRET
    # in force as soon as the code shows, before the dialog is confirmed
    env = await _send_and_capture(hass, "nas")
    assert env["sig"] == _cmd_sig(env, paired["s"]) and env["sig"] != _cmd_sig(env, SECRET)
    flow.async_abort(result["flow_id"])  # closing the dialog keeps the new secret
    await hass.async_block_till_done()
    assert hass_storage[KEY]["data"]["nas"] == {"secret": paired["s"]}
    assert entry.options["machines"][0] == NAS
    env = await _send_and_capture(hass, "nas")
    assert env["sig"] == _cmd_sig(env, paired["s"])

    # legacy path: typing a secret replaces it as well, without a code
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "edit"})
    result = await flow.async_configure(result["flow_id"], {"id": "nas"})
    result = await flow.async_configure(result["flow_id"], {"name": "NAS", "advanced": {"secret": "typed-secret-123456"}})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    env = await _send_and_capture(hass, "nas")
    assert env["sig"] == _cmd_sig(env, "typed-secret-123456")


async def test_old_code_without_type_is_still_a_webhook_code() -> None:
    """WEBHOOK.md: a code without "t" (0.3.0) means webhook; the machine side checks that (tests/test_pair.py)."""
    from custom_components.crew_hub import webhook_transport

    assert _decode(webhook_transport.pairing_code("https://h", "w" * 32, "s" * 20, "mac"))["t"] == "webhook"


# ---- diagnostics ----
async def test_diagnostics_are_redacted(hass, mqtt_mock, hass_storage, hass_client, nas_snapshot) -> None:
    entry = await _setup(hass)
    await _online(hass, "nas", nas_snapshot)
    with patch.object(secret_store.SecretStore, "secret") as read_secret, \
            patch.object(secret_store.SecretStore, "webhook_id") as read_hook:
        diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    read_secret.assert_not_called()
    read_hook.assert_not_called()
    text = json.dumps(diag)
    _assert_nowhere(text, SECRET, MAC_SECRET, MAC_HOOK, KEY)
    assert diag["entry"]["options"]["machines"] == [NAS, MAC]
    assert diag["machines"]["nas"]["status"] == "online" and diag["machines"]["nas"]["sessions"] == len(
        nas_snapshot["sessions"])
    assert diag["machines"]["mac"]["queuedCommands"] == 0

    # an entry that still has 0.3.0 options (not set up yet): secret keys and URL paths are redacted
    legacy = _entry([LEGACY_NAS, {**LEGACY_MAC, "public_url": "https://hass.example.com/api/webhook/" + MAC_HOOK}])
    out = await diagnostics.async_get_config_entry_diagnostics(hass, legacy)
    text = json.dumps(out)
    _assert_nowhere(text, SECRET, MAC_SECRET, MAC_HOOK, "/api/webhook")
    nas, mac = out["entry"]["options"]["machines"]
    assert nas["secret"] == "**REDACTED**" and mac["webhook_id"] == "**REDACTED**"
    assert mac["public_url"] == "https://hass.example.com/**REDACTED**"
    assert diagnostics._redact_url_paths({"u": "https://h.example.com", "x": ["http://u:p@h/x?y"]}) == {
        "u": "https://h.example.com", "x": ["http://h/**REDACTED**"]}


# ---- removal ----
async def test_remove_machine_and_entry_clean_the_store(hass, mqtt_mock, hass_storage) -> None:
    entry = await _setup(hass)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "remove"})
    result = await flow.async_configure(result["flow_id"], {"id": "nas"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass_storage[KEY]["data"] == {"mac": {"secret": MAC_SECRET, "webhookId": MAC_HOOK}}
    assert hass.data[DATA_SECRETS].secret("nas") is None

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert KEY not in hass_storage
    assert DATA_SECRETS not in hass.data
