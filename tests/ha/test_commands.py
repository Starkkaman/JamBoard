"""Signed commands (docs/COMMANDS.md): websocket crew_hub/command, crew_hub.* services, ack routing, refusals."""

import asyncio
import hashlib
import hmac
import json
import logging
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized

from custom_components.crew_hub import commands
from custom_components.crew_hub.const import DATA_PENDING, DOMAIN

SECRET = "s3cret-s3cret-s3cret"
MANAGER = "4d2b9c71-8a0e-4f3b-b5d6-91c7e2a0f4b8"  # interactive, waiting permission, controls send+answer
CREW = "b8e41f03-2c6a-47d9-8e15-3a90c6d2f7e1"  # bg, controls send/answer/interrupt/stop
TEXT = "please check the TV entity again"


async def _setup(hass: HomeAssistant, nas_snapshot, snapshot=True) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Crew Hub", data={"prefix": "crew"}, unique_id=DOMAIN,
        options={"machines": [{"id": "nas", "name": "NAS", "secret": SECRET}]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if snapshot:
        async_fire_mqtt_message(hass, "crew/nas/status", "online", retain=True)
        async_fire_mqtt_message(hass, "crew/nas/snapshot", json.dumps(nas_snapshot), retain=True)
        await hass.async_block_till_done()
    return entry


class Recorder:
    """Stands in for mqtt.async_publish; optionally answers with an ack on crew/nas/ack."""

    def __init__(self, hass, ack=None):
        self.hass, self.ack, self.calls = hass, ack, []

    async def __call__(self, hass, topic, payload, qos=0, retain=False, **kw):
        self.calls.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        if self.ack is not None:
            env = json.loads(payload)
            ack = dict(self.ack, id=env["id"])
            hass.loop.call_soon(async_fire_mqtt_message, hass, "crew/nas/ack", json.dumps(ack))


def _independent_sig(env: dict) -> str:
    body = {"v": env["v"], "id": env["id"], "ts": env["ts"], "action": env["action"], "args": env["args"]}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(SECRET.encode(), canon, hashlib.sha256).hexdigest()


async def _ws(client, i, machine, action, args):
    await client.send_json({"id": i, "type": "crew_hub/command", "machine": machine, "action": action, "args": args})
    return await client.receive_json()


async def test_send_signed_published_and_acked(hass, mqtt_mock, hass_ws_client, nas_snapshot, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.crew_hub")
    await _setup(hass, nas_snapshot)
    rec = Recorder(hass, ack={"ok": True, "result": {"delivered": True}})
    client = await hass_ws_client(hass)
    with patch("custom_components.crew_hub.commands.mqtt.async_publish", rec):
        msg = await _ws(client, 1, "nas", "send", {"session": MANAGER, "text": "Grüße — " + TEXT, "urgent": True})
    assert msg["success"], msg
    assert msg["result"] == {"ok": True, "result": {"delivered": True}}

    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["topic"] == "crew/nas/cmd"
    assert call["qos"] == 1 and call["retain"] is False
    env = json.loads(call["payload"])
    assert set(env) == {"v", "id", "ts", "action", "args", "sig"}
    assert env["v"] == 1 and env["action"] == "send"
    assert env["args"] == {"session": MANAGER, "text": "Grüße — " + TEXT, "urgent": True}
    assert isinstance(env["ts"], int) and env["ts"] > 1_700_000_000_000
    assert len(env["id"]) == 36
    assert env["sig"] == _independent_sig(env)
    assert hass.data[DATA_PENDING] == {}

    # never logged: secret, signature, message text; logged: action, machine, id, outcome
    # (HA's own websocket debug log echoes incoming messages; only crew_hub's records are checked)
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components.crew_hub"))
    assert SECRET not in ours and env["sig"] not in ours and TEXT not in ours
    assert f"send for nas id {env['id']}: ok" in ours


async def test_ack_error_and_wrong_machine_ignored(hass, mqtt_mock, hass_ws_client, nas_snapshot) -> None:
    await _setup(hass, nas_snapshot)
    client = await hass_ws_client(hass)

    async def publish(hass_, topic, payload, qos=0, retain=False, **kw):
        cmd_id = json.loads(payload)["id"]
        # an ack for the same id on another machine's topic, an unknown id, and junk are all ignored
        hass_.loop.call_soon(async_fire_mqtt_message, hass_, "crew/mac/ack", json.dumps({"id": cmd_id, "ok": True}))
        hass_.loop.call_soon(async_fire_mqtt_message, hass_, "crew/nas/ack", json.dumps({"id": "nope", "ok": True}))
        hass_.loop.call_soon(async_fire_mqtt_message, hass_, "crew/nas/ack", "{junk")
        hass_.loop.call_soon(async_fire_mqtt_message, hass_, "crew/nas/ack",
                             json.dumps({"id": cmd_id, "ok": False, "error": "not_waiting"}))

    with patch("custom_components.crew_hub.commands.mqtt.async_publish", publish):
        msg = await _ws(client, 1, "nas", "answer", {"session": MANAGER, "requestId": "req_7f31a2", "decision": "allow"})
    assert msg["result"] == {"ok": False, "error": "not_waiting"}


async def test_no_ack_times_out(hass, mqtt_mock, hass_ws_client, nas_snapshot) -> None:
    await _setup(hass, nas_snapshot)
    rec = Recorder(hass)  # never acks
    client = await hass_ws_client(hass)
    with patch("custom_components.crew_hub.commands.mqtt.async_publish", rec), \
            patch("custom_components.crew_hub.commands.ACK_TIMEOUT", 0.05):
        msg = await _ws(client, 1, "nas", "stop", {"session": CREW})
    assert msg["result"] == {"ok": False, "error": "no_ack"}
    assert len(rec.calls) == 1
    assert hass.data[DATA_PENDING] == {}


async def test_real_mqtt_publish_not_retained(hass, mqtt_mock, mqtt_client_mock, nas_snapshot) -> None:
    """Through HA's real MQTT client (paho mocked): QoS 1, retain False."""
    await _setup(hass, nas_snapshot)
    mqtt_client_mock.publish.reset_mock()
    res = await commands.async_send_command(hass, "nas", "interrupt", {"session": CREW}, timeout=0.05)
    assert res == {"ok": False, "error": "no_ack"}
    calls = [c for c in mqtt_client_mock.publish.call_args_list if c.args[0] == "crew/nas/cmd"]
    assert len(calls) == 1
    topic, payload, qos, retain = calls[0].args[:4]
    assert qos == 1 and retain is False
    assert json.loads(payload)["action"] == "interrupt"


async def test_non_admin_refused(hass, mqtt_mock, hass_ws_client, hass_read_only_access_token, nas_snapshot) -> None:
    await _setup(hass, nas_snapshot)
    rec = Recorder(hass, ack={"ok": True})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    with patch("custom_components.crew_hub.commands.mqtt.async_publish", rec):
        msg = await _ws(client, 1, "nas", "send", {"session": MANAGER, "text": "hi", "urgent": False})
    assert not msg["success"] and msg["error"]["code"] == "unauthorized"
    assert rec.calls == []


@pytest.mark.parametrize(
    ("machine", "action", "args", "error"),
    [
        ("mac", "send", {"session": "x", "text": "hi"}, "not_configured"),
        ("nas", "interrupt", {"session": MANAGER}, "not_allowed"),  # interactive: not in controls
        ("nas", "stop", {"session": "no-such-session"}, "unknown_session"),
        ("nas", "reboot", {}, "unknown_action"),
        ("nas", "send", {"session": MANAGER, "text": "x" * 4001}, "invalid_args"),
        ("nas", "send", {"session": MANAGER, "text": "hi", "urgent": "yes"}, "invalid_args"),
        ("nas", "send", {"session": MANAGER, "text": "hi", "shell": "rm"}, "invalid_args"),
        ("nas", "answer", {"session": MANAGER, "requestId": "r", "decision": "maybe"}, "invalid_args"),
        ("nas", "answer", {"session": MANAGER, "requestId": "r", "decision": "deny", "message": "m" * 501}, "invalid_args"),
        ("nas", "start", {"folder": "/etc", "model": "claude-opus-5-5", "permissionMode": "default", "name": "n", "prompt": "p"}, "invalid_args"),
        ("nas", "start", {"folder": "jamfin", "model": "gpt", "permissionMode": "default", "name": "n", "prompt": "p"}, "invalid_args"),
        ("nas", "start", {"folder": "jamfin", "model": "claude-opus-5-5", "permissionMode": "bypassPermissions", "name": "n", "prompt": "p"}, "invalid_args"),
        ("nas", "start", {"folder": "jamfin", "model": "claude-opus-5-5", "permissionMode": "default", "name": "n" * 61, "prompt": "p"}, "invalid_args"),
    ],
)
async def test_refusals(hass, mqtt_mock, hass_ws_client, nas_snapshot, machine, action, args, error) -> None:
    await _setup(hass, nas_snapshot)
    async_fire_mqtt_message(hass, "crew/mac/snapshot", json.dumps(nas_snapshot))  # publishes but isn't configured
    await hass.async_block_till_done()
    rec = Recorder(hass, ack={"ok": True})
    client = await hass_ws_client(hass)
    with patch("custom_components.crew_hub.commands.mqtt.async_publish", rec):
        msg = await _ws(client, 1, machine, action, args)
    assert msg["success"], msg
    assert msg["result"]["ok"] is False and msg["result"]["error"] == error
    assert rec.calls == []


async def test_commands_disabled_and_offline(hass, mqtt_mock, hass_ws_client, nas_snapshot) -> None:
    snap = json.loads(json.dumps(nas_snapshot))
    snap["machine"].pop("commands")
    await _setup(hass, snap)
    client = await hass_ws_client(hass)
    rec = Recorder(hass, ack={"ok": True})
    with patch("custom_components.crew_hub.commands.mqtt.async_publish", rec):
        msg = await _ws(client, 1, "nas", "send", {"session": MANAGER, "text": "hi"})
        assert msg["result"]["error"] == "commands_disabled"
        async_fire_mqtt_message(hass, "crew/nas/snapshot", json.dumps(nas_snapshot))
        async_fire_mqtt_message(hass, "crew/nas/status", "offline")
        await hass.async_block_till_done()
        msg = await _ws(client, 2, "nas", "send", {"session": MANAGER, "text": "hi"})
        assert msg["result"]["error"] == "offline"
    assert rec.calls == []


async def test_start_ok_with_result(hass, mqtt_mock, hass_ws_client, nas_snapshot) -> None:
    await _setup(hass, nas_snapshot)
    rec = Recorder(hass, ack={"ok": True, "result": {"bgId": "bg_1"}})
    client = await hass_ws_client(hass)
    args = {"folder": "jamfin", "model": "claude-opus-5-5", "permissionMode": "auto", "name": " Disc test ", "prompt": "Go"}
    with patch("custom_components.crew_hub.commands.mqtt.async_publish", rec):
        msg = await _ws(client, 1, "nas", "start", args)
    assert msg["result"] == {"ok": True, "result": {"bgId": "bg_1"}}
    env = json.loads(rec.calls[0]["payload"])
    assert env["args"] == {"folder": "jamfin", "model": "claude-opus-5-5", "permissionMode": "auto", "name": "Disc test", "prompt": "Go"}
    assert env["sig"] == _independent_sig(env)


# ---- services ----
async def test_services_reject_context_without_user(hass, mqtt_mock, nas_snapshot) -> None:
    await _setup(hass, nas_snapshot)
    rec = Recorder(hass, ack={"ok": True})
    with patch("custom_components.crew_hub.commands.mqtt.async_publish", rec):
        for service, data in (
            ("send_message", {"machine": "nas", "session": MANAGER, "text": "hi"}),
            ("stop", {"machine": "nas", "session": CREW}),
        ):
            with pytest.raises(ServiceValidationError, match="admin user"):
                await hass.services.async_call(DOMAIN, service, data, blocking=True, context=Context())
    assert rec.calls == []


async def test_services_non_admin_unauthorized(hass, mqtt_mock, nas_snapshot, hass_read_only_user) -> None:
    await _setup(hass, nas_snapshot)
    rec = Recorder(hass, ack={"ok": True})
    with patch("custom_components.crew_hub.commands.mqtt.async_publish", rec), pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "interrupt", {"machine": "nas", "session": CREW}, blocking=True,
                                       context=Context(user_id=hass_read_only_user.id))
    assert rec.calls == []


async def test_services_admin_same_path(hass, mqtt_mock, nas_snapshot, hass_admin_user) -> None:
    await _setup(hass, nas_snapshot)
    rec = Recorder(hass, ack={"ok": True, "result": {"x": 1}})
    ctx = Context(user_id=hass_admin_user.id)
    with patch("custom_components.crew_hub.commands.mqtt.async_publish", rec):
        res = await hass.services.async_call(
            DOMAIN, "answer",
            {"machine": "nas", "session": MANAGER, "requestId": "req_7f31a2", "decision": "deny", "message": "not now"},
            blocking=True, context=ctx, return_response=True,
        )
        assert res == {"ok": True, "result": {"x": 1}}
        env = json.loads(rec.calls[0]["payload"])
        assert env["action"] == "answer"
        assert env["args"] == {"session": MANAGER, "requestId": "req_7f31a2", "decision": "deny", "message": "not now"}
        assert env["sig"] == _independent_sig(env)

        # a refused command raises for the caller
        with pytest.raises(HomeAssistantError, match="not_allowed"):
            await hass.services.async_call(DOMAIN, "stop", {"machine": "nas", "session": MANAGER}, blocking=True, context=ctx)
        assert len(rec.calls) == 1


def test_canonical_matches_contract() -> None:
    env = commands.build_envelope(SECRET, "send", {"text": "ü", "session": "s", "urgent": False}, cmd_id="i", ts=5)
    assert commands.canonical(env) == '{"action":"send","args":{"session":"s","text":"ü","urgent":false},"id":"i","ts":5,"v":1}'.encode()
    assert env["sig"] == _independent_sig(env)
