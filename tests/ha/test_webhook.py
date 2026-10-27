"""Webhook transport (docs/WEBHOOK.md): pairing, request auth, snapshot -> store -> websocket, poll/queue, ack, offline.

Real HTTP POSTs to /api/webhook/<id> through hass_client_no_auth (the webhook view needs no HA login).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from homeassistant.components.webhook import _HANDLERS as WEBHOOK_HANDLERS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.crew_hub import webhook_transport
from custom_components.crew_hub.const import DATA_PENDING, DATA_SECRETS, DATA_STORE, DATA_WEBHOOK, DOMAIN

SECRET = "s3cret-s3cret-s3cret"
MAC_SECRET = "mac-secret-mac-secret-mac-secret-0123456789"
MAC_HOOK = "a" * 64
MAC = {"id": "mac", "name": "MacBook", "transport": "webhook", "public_url": "https://hass.example.com",
       "webhook_id": MAC_HOOK, "secret": MAC_SECRET}
NAS = {"id": "nas", "name": "NAS", "secret": SECRET}  # no "transport": an MQTT machine from before 0.3.0
MANAGER = "4d2b9c71-8a0e-4f3b-b5d6-91c7e2a0f4b8"  # fixture session with controls send+answer


# ---- helpers ----
def _sign(secret: str, req: dict) -> str:
    """Independent implementation of the WEBHOOK.md signature."""
    body = {k: req[k] for k in ("v", "m", "ts", "nonce", "type", "data")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canon, hashlib.sha256).hexdigest()


def _req(kind: str, data: dict | None = None, *, m: str = "mac", secret: str = MAC_SECRET, ts: int | None = None,
         nonce: str | None = None) -> dict:
    req = {"v": 1, "m": m, "ts": ts if ts is not None else int(time.time() * 1000), "nonce": nonce or str(uuid.uuid4()),
           "type": kind, "data": data if data is not None else {}}
    req["sig"] = _sign(secret, req)
    return req


def _decode(code: str) -> dict:
    assert code.startswith("crewhub1:")
    body = code[len("crewhub1:"):]
    assert "=" not in body
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _cmd_sig(env: dict, secret: str) -> str:
    body = {k: env[k] for k in ("v", "id", "ts", "action", "args")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(secret.encode(), canon, hashlib.sha256).hexdigest()


async def _setup(hass: HomeAssistant, machines=None) -> MockConfigEntry:
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Crew Hub", data={"prefix": "crew"}, unique_id=DOMAIN,
                            options={"machines": machines if machines is not None else [NAS, MAC]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _post(client, body, hook: str = MAC_HOOK):
    data = body if isinstance(body, (bytes, str)) else json.dumps(body)
    resp = await client.post(f"/api/webhook/{hook}", data=data, headers={"Content-Type": "application/json"})
    return resp.status, await resp.read()


def _store(hass):
    return hass.data[DATA_STORE]


# ---- pairing ----
async def test_add_webhook_machine_shows_code_once(hass, mqtt_mock, hass_client_no_auth, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    entry = await _setup(hass, machines=[NAS])
    flow = hass.config_entries.options

    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add"})
    for bad in ("http://hass.example.com", "https://hass.example.com/api", "https://hass.example.com?x=1",
                "https://user:pw@hass.example.com", "hass.example.com", ""):
        result = await flow.async_configure(result["flow_id"], {"id": "mac", "transport": "webhook", "public_url": bad})
        assert result["type"] is FlowResultType.FORM and result["errors"] == {"public_url": "invalid_public_url"}, bad

    result = await flow.async_configure(
        result["flow_id"], {"id": "Mac", "name": "MacBook", "transport": "webhook", "public_url": "https://Hass.Example.com/"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pair"
    assert result["data_schema"].schema == {}
    code = result["description_placeholders"]["code"]
    paired = _decode(code)
    assert set(paired) == {"t", "u", "w", "s", "m"} and paired["t"] == "webhook"
    assert paired["u"] == "https://hass.example.com" and paired["m"] == "mac"
    assert len(paired["w"]) == 64 and int(paired["w"], 16) >= 0
    assert len(paired["s"]) >= 40

    # saved and registered while the code is on screen, so crew-hub-pair's hello works before the user confirms
    await hass.async_block_till_done()
    mac = entry.options["machines"][1]
    assert mac == {"id": "mac", "name": "MacBook", "transport": "webhook", "public_url": "https://hass.example.com",
                   "has_secret": True}
    secrets = hass.data[DATA_SECRETS]
    assert (secrets.webhook_id("mac"), secrets.secret("mac")) == (paired["w"], paired["s"])
    assert paired["w"] in hass.data[WEBHOOK_HANDLERS]
    client = await hass_client_no_auth()
    status, body = await _post(client, _req("hello", {"version": "0.3.0"}, secret=paired["s"]), paired["w"])
    assert status == 200 and json.loads(body) == {"ok": True}

    result = await flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert code not in json.dumps(result, default=str)
    await hass.async_block_till_done()
    assert entry.options["machines"][1] == mac
    assert hass.states.get("binary_sensor.crew_hub_mac_online").state == "on"

    # the edit form never shows the code or the secret
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "edit"})
    result = await flow.async_configure(result["flow_id"], {"id": "mac"})
    assert result["step_id"] == "edit_webhook"
    shown = json.dumps(result, default=str)
    assert code not in shown and paired["s"] not in shown and paired["w"] not in shown

    # never logged (the test harness's own storage mock logs whatever it writes; real HA logs only the file path)
    logged = "\n".join(r.getMessage() for r in caplog.records
                       if not r.name.startswith("pytest_homeassistant_custom_component"))
    assert code not in logged and paired["s"] not in logged
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components.crew_hub"))
    assert paired["w"] not in ours


async def test_closed_pairing_dialog_keeps_machine(hass, mqtt_mock) -> None:
    """Closing the dialog instead of confirming: the machine stays (re-pair for a new code); the code isn't stored."""
    entry = await _setup(hass, machines=[])
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add"})
    result = await flow.async_configure(result["flow_id"], {"id": "mac", "transport": "webhook",
                                                            "public_url": "https://hass.example.com"})
    assert result["step_id"] == "pair"
    code = result["description_placeholders"]["code"]
    flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert [m["id"] for m in entry.options["machines"]] == ["mac"]
    assert hass.data[DATA_SECRETS].webhook_id("mac") in hass.data[WEBHOOK_HANDLERS]
    shown = json.dumps(dict(entry.options))
    assert code not in shown and hass.data[DATA_SECRETS].secret("mac") not in shown


async def test_repair_invalidates_old_webhook(hass, mqtt_mock, hass_client_no_auth, nas_snapshot) -> None:
    entry = await _setup(hass)
    client = await hass_client_no_auth()
    assert (await _post(client, _req("hello", {"version": "0.3.0"})))[0] == 200

    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "edit"})
    result = await flow.async_configure(result["flow_id"], {"id": "mac"})
    result = await flow.async_configure(result["flow_id"], {"name": "MacBook", "repair": True})
    assert result["step_id"] == "pair"
    paired = _decode(result["description_placeholders"]["code"])
    assert paired["w"] != MAC_HOOK and paired["s"] != MAC_SECRET and paired["u"] == MAC["public_url"]
    await hass.async_block_till_done()
    assert MAC_HOOK not in hass.data[WEBHOOK_HANDLERS]  # the old one is gone as soon as the new code shows
    assert _store(hass).status("mac") == "offline"  # its credentials are gone
    result = await flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert MAC_HOOK not in hass.data[WEBHOOK_HANDLERS]
    assert paired["w"] in hass.data[WEBHOOK_HANDLERS]
    # the old credentials no longer reach the store (HA answers 200 for unknown webhooks, by design)
    status, _ = await _post(client, _req("snapshot", nas_snapshot))
    assert _store(hass).machine("mac")["snapshot"] is None
    # a request signed with the old secret on the new webhook is refused; the new pair works
    assert (await _post(client, _req("hello", {}), paired["w"]))[0] == 401
    assert (await _post(client, _req("snapshot", nas_snapshot, secret=paired["s"]), paired["w"]))[0] == 200
    assert _store(hass).machine("mac")["snapshot"] == nas_snapshot


async def test_edit_without_repair_keeps_credentials(hass, mqtt_mock) -> None:
    entry = await _setup(hass)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "edit"})
    result = await flow.async_configure(result["flow_id"], {"id": "mac"})
    result = await flow.async_configure(result["flow_id"], {"name": "Mac", "public_url": "http://x"})
    assert result["errors"] == {"public_url": "invalid_public_url"}
    result = await flow.async_configure(result["flow_id"], {"name": "Mac", "public_url": "https://h.example.net:8443"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["machines"][1] == {"id": "mac", "name": "Mac", "transport": "webhook",
                                            "public_url": "https://h.example.net:8443", "has_secret": True}
    assert (hass.data[DATA_SECRETS].webhook_id("mac"), hass.data[DATA_SECRETS].secret("mac")) == (MAC_HOOK, MAC_SECRET)
    assert MAC_HOOK in hass.data[WEBHOOK_HANDLERS]


async def test_remove_unregisters_webhook(hass, mqtt_mock) -> None:
    entry = await _setup(hass)
    assert MAC_HOOK in hass.data[WEBHOOK_HANDLERS]
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "remove"})
    result = await flow.async_configure(result["flow_id"], {"id": "mac"})
    await hass.async_block_till_done()
    assert MAC_HOOK not in hass.data[WEBHOOK_HANDLERS]
    assert hass.states.get("binary_sensor.crew_hub_mac_online") is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not any(h.domain == DOMAIN for h in hass.data[WEBHOOK_HANDLERS].values())


# ---- request auth ----
def _oversize():
    return _req("snapshot", {"x": "a" * (webhook_transport.MAX_BODY + 10)})


@pytest.mark.parametrize(
    ("make", "status"),
    [
        (lambda: _req("hello", {"version": "0.3.0"}), 200),
        (lambda: {**_req("hello", {}), "sig": "0" * 64}, 401),
        (lambda: {**_req("hello", {}), "sig": "zz"}, 401),
        (lambda: _req("hello", {}, secret="wrong-secret-wrong-secret"), 401),
        (lambda: _req("hello", {}, ts=int(time.time() * 1000) - 121_000), 401),
        (lambda: _req("hello", {}, ts=int(time.time() * 1000) + 121_000), 401),
        (lambda: _req("hello", {}, m="nas"), 401),
        (lambda: {**_req("hello", {}), "data": {"tampered": True}}, 401),
        (lambda: _req("reboot", {}), 401),
        (lambda: {**_req("hello", {}), "v": 2}, 401),
        (lambda: "{not json", 401),
        (lambda: "[]", 401),
        (_oversize, 413),
    ],
)
async def test_request_auth(hass, mqtt_mock, hass_client_no_auth, caplog, make, status) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.crew_hub")
    await _setup(hass)
    client = await hass_client_no_auth()
    code, body = await _post(client, make())
    assert code == status
    if status == 200:
        assert json.loads(body) == {"ok": True}
        assert _store(hass).status("mac") == "online"
    else:
        assert body == b""
        assert _store(hass).status("mac") == "unknown"
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components.crew_hub"))
    assert MAC_SECRET not in ours and MAC_HOOK not in ours


async def test_replayed_nonce_refused(hass, mqtt_mock, hass_client_no_auth) -> None:
    await _setup(hass)
    client = await hass_client_no_auth()
    req = _req("hello", {})
    assert (await _post(client, req))[0] == 200
    assert (await _post(client, req))[0] == 401
    # same nonce, fresh signature and ts: still a replay
    assert (await _post(client, _req("hello", {"version": "x"}, nonce=req["nonce"])))[0] == 401


async def test_oversize_without_content_length(hass, mqtt_mock, hass_client_no_auth) -> None:
    """A chunked body (no Content-Length) is cut off at the cap as well."""
    await _setup(hass)
    client = await hass_client_no_auth()
    raw = json.dumps(_oversize()).encode()

    async def chunks():
        for i in range(0, len(raw), 32 * 1024):
            yield raw[i:i + 32 * 1024]

    resp = await client.post(f"/api/webhook/{MAC_HOOK}", data=chunks())
    assert resp.status == 413


# ---- snapshot -> store -> websocket ----
async def test_snapshot_reaches_websocket(hass, mqtt_mock, hass_client_no_auth, hass_ws_client, nas_snapshot) -> None:
    await _setup(hass)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "crew_hub/subscribe"})
    assert (await ws.receive_json())["success"]
    full = await ws.receive_json()
    assert full["event"]["machines"]["mac"] == {"status": "unknown", "snapshot": None, "receivedAt": None,
                                                "configured": True}

    client = await hass_client_no_auth()
    code, body = await _post(client, _req("snapshot", nas_snapshot))
    assert code == 200 and json.loads(body) == {"ok": True}
    msg = await ws.receive_json()
    assert msg["event"]["type"] == "machine" and msg["event"]["id"] == "mac"
    data = msg["event"]["data"]
    assert data["status"] == "online" and data["snapshot"] == nas_snapshot and data["configured"] is True
    assert isinstance(data["receivedAt"], int)
    assert hass.states.get("binary_sensor.crew_hub_mac_online").state == "on"
    assert hass.states.get("sensor.crew_hub_working").state == "3"
    assert MAC_SECRET not in json.dumps(msg)


# ---- poll / queue / ack ----
async def _online_with_commands(hass, client, nas_snapshot):
    assert (await _post(client, _req("snapshot", nas_snapshot)))[0] == 200


async def test_poll_returns_queued_command_and_ack_completes(
    hass, mqtt_mock, hass_client_no_auth, hass_ws_client, nas_snapshot
) -> None:
    await _setup(hass)
    client = await hass_client_no_auth()
    await _online_with_commands(hass, client, nas_snapshot)
    ws = await hass_ws_client(hass)
    with patch("custom_components.crew_hub.commands.mqtt.async_publish") as publish:
        await ws.send_json({"id": 1, "type": "crew_hub/command", "machine": "mac", "action": "send",
                            "args": {"session": MANAGER, "text": "hello from the hub", "urgent": False}})
        hub = hass.data[DATA_WEBHOOK]
        for _ in range(50):
            if hub.queued("mac"):
                break
            await asyncio.sleep(0.01)
        assert hub.queued("mac") == 1

        start = time.monotonic()
        code, body = await _post(client, _req("poll"))
        assert time.monotonic() - start < 2  # not held: a command was waiting
        assert code == 200
        cmds = json.loads(body)["commands"]
        assert len(cmds) == 1
        env = cmds[0]
        assert set(env) == {"v", "id", "ts", "action", "args", "sig"}
        assert env["action"] == "send" and env["args"] == {"session": MANAGER, "text": "hello from the hub",
                                                           "urgent": False}
        assert env["sig"] == _cmd_sig(env, MAC_SECRET)

        # at most once: the next poll doesn't get it again
        with patch.object(webhook_transport, "POLL_HOLD", 0.05):
            assert json.loads((await _post(client, _req("poll")))[1]) == {"commands": []}

        # an ack for an unknown id is accepted but changes nothing; the real one completes the command
        assert json.loads((await _post(client, _req("ack", {"id": "nope", "ok": True})))[1]) == {"ok": True}
        code, body = await _post(client, _req("ack", {"id": env["id"], "ok": True, "result": {"delivered": True}}))
        assert code == 200 and json.loads(body) == {"ok": True}
        msg = await ws.receive_json()
    assert msg["success"] and msg["result"] == {"ok": True, "result": {"delivered": True}}
    publish.assert_not_called()  # webhook machines never get MQTT commands
    assert hass.data[DATA_PENDING] == {}


async def test_held_poll_gets_command_and_timeout_returns_empty(
    hass, mqtt_mock, hass_client_no_auth, nas_snapshot
) -> None:
    await _setup(hass)
    client = await hass_client_no_auth()
    await _online_with_commands(hass, client, nas_snapshot)

    with patch.object(webhook_transport, "POLL_HOLD", 0.05):
        start = time.monotonic()
        code, body = await _post(client, _req("poll"))
        assert code == 200 and json.loads(body) == {"commands": []}
        assert time.monotonic() - start >= 0.05

    from custom_components.crew_hub import commands
    poll = asyncio.ensure_future(_post(client, _req("poll")))
    await asyncio.sleep(0.05)
    assert not poll.done()  # held
    send = asyncio.ensure_future(
        commands.async_send_command(hass, "mac", "answer",
                                    {"session": MANAGER, "requestId": "req_7f31a2", "decision": "deny"}, timeout=2))
    code, body = await asyncio.wait_for(poll, 2)
    env = json.loads(body)["commands"][0]
    assert env["action"] == "answer"
    await _post(client, _req("ack", {"id": env["id"], "ok": False, "error": "not_waiting"}))
    assert await send == {"ok": False, "error": "not_waiting"}


async def test_newer_poll_releases_older(hass, mqtt_mock, hass_client_no_auth, nas_snapshot) -> None:
    await _setup(hass)
    client = await hass_client_no_auth()
    await _online_with_commands(hass, client, nas_snapshot)
    first = asyncio.ensure_future(_post(client, _req("poll")))
    await asyncio.sleep(0.05)
    assert not first.done()
    second = asyncio.ensure_future(_post(client, _req("poll")))
    code, body = await asyncio.wait_for(first, 2)
    assert code == 200 and json.loads(body) == {"commands": []}
    assert not second.done()
    hass.data[DATA_WEBHOOK].enqueue("mac", {"v": 1, "id": "x"})
    assert json.loads((await asyncio.wait_for(second, 2))[1]) == {"commands": [{"v": 1, "id": "x"}]}


async def test_queue_cap_and_ttl(hass, mqtt_mock) -> None:
    await _setup(hass)
    hub = hass.data[DATA_WEBHOOK]
    now = [1000.0]
    hub._clock = lambda: now[0]
    for i in range(20):
        assert hub.enqueue("mac", {"id": str(i)})
    assert not hub.enqueue("mac", {"id": "21"})
    assert not hub.enqueue("nas", {"id": "mqtt"})  # not a webhook machine
    now[0] += 61
    assert hub.queued("mac") == 20
    assert hub.enqueue("mac", {"id": "late"})  # expired ones are dropped first
    assert hub.queued("mac") == 1


async def test_queue_full_refuses_command(hass, mqtt_mock, hass_client_no_auth, nas_snapshot) -> None:
    from custom_components.crew_hub import commands
    await _setup(hass)
    client = await hass_client_no_auth()
    await _online_with_commands(hass, client, nas_snapshot)
    hub = hass.data[DATA_WEBHOOK]
    for i in range(20):
        hub.enqueue("mac", {"id": str(i)})
    res = await commands.async_send_command(hass, "mac", "send", {"session": MANAGER, "text": "hi"}, timeout=0.05)
    assert res == {"ok": False, "error": "queue_full"}
    assert hass.data[DATA_PENDING] == {}


# ---- online / offline ----
async def test_offline_after_silence(hass, mqtt_mock, hass_client_no_auth, nas_snapshot) -> None:
    await _setup(hass)
    client = await hass_client_no_auth()
    await _online_with_commands(hass, client, nas_snapshot)
    assert hass.states.get("binary_sensor.crew_hub_mac_online").state == "on"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()
    assert _store(hass).status("mac") == "online"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=95))
    await hass.async_block_till_done()
    assert _store(hass).status("mac") == "offline"
    assert hass.states.get("binary_sensor.crew_hub_mac_online").state == "off"
    assert hass.states.get("sensor.crew_hub_working").state == "0"

    from custom_components.crew_hub import commands
    res = await commands.async_send_command(hass, "mac", "send", {"session": MANAGER, "text": "hi"})
    assert res["ok"] is False and res["error"] == "offline"

    assert (await _post(client, _req("hello", {})))[0] == 200  # back online on the next valid request
    assert _store(hass).status("mac") == "online"


async def test_valid_request_restarts_online_window(hass, mqtt_mock, hass_client_no_auth) -> None:
    await _setup(hass)
    client = await hass_client_no_auth()
    with patch.object(webhook_transport, "ONLINE_WINDOW", 0.5):
        assert (await _post(client, _req("hello", {})))[0] == 200
        await asyncio.sleep(0.35)
        assert (await _post(client, _req("hello", {})))[0] == 200
        await asyncio.sleep(0.35)  # 0.7 s after the first request, 0.35 s after the second
        assert _store(hass).status("mac") == "online"
        await asyncio.sleep(0.4)
        assert _store(hass).status("mac") == "offline"
        # a rejected request doesn't bring it back
        assert (await _post(client, {**_req("hello", {}), "sig": "0" * 64}))[0] == 401
        assert _store(hass).status("mac") == "offline"


async def test_online_survives_reload(hass, mqtt_mock, hass_client_no_auth, nas_snapshot) -> None:
    entry = await _setup(hass)
    client = await hass_client_no_auth()
    await _online_with_commands(hass, client, nas_snapshot)
    hass.data[DATA_WEBHOOK].enqueue("mac", {"id": "kept"})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _store(hass).status("mac") == "online"
    assert hass.data[DATA_WEBHOOK].queued("mac") == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=95))
    await hass.async_block_till_done()
    assert _store(hass).status("mac") == "offline"


# ---- MQTT machines unchanged ----
async def test_mqtt_machine_still_works(hass, mqtt_mock, hass_client_no_auth, nas_snapshot) -> None:
    await _setup(hass)
    async_fire_mqtt_message(hass, "crew/nas/status", "online")
    async_fire_mqtt_message(hass, "crew/nas/snapshot", json.dumps(nas_snapshot))
    # MQTT traffic under a webhook machine's id is ignored
    async_fire_mqtt_message(hass, "crew/mac/status", "online")
    async_fire_mqtt_message(hass, "crew/mac/snapshot", json.dumps(nas_snapshot))
    await hass.async_block_till_done()
    assert _store(hass).machine("nas")["snapshot"] == nas_snapshot
    assert _store(hass).status("nas") == "online"
    assert _store(hass).machine("mac") == {"status": "unknown", "snapshot": None, "receivedAt": None,
                                          "configured": True}

    calls = []

    async def publish(hass_, topic, payload, qos=0, retain=False, **kw):
        calls.append((topic, qos, retain))
        env = json.loads(payload)
        assert env["sig"] == _cmd_sig(env, SECRET)
        hass_.loop.call_soon(async_fire_mqtt_message, hass_, "crew/nas/ack", json.dumps({"id": env["id"], "ok": True}))

    from custom_components.crew_hub import commands
    with patch("custom_components.crew_hub.commands.mqtt.async_publish", publish):
        res = await commands.async_send_command(hass, "nas", "send", {"session": MANAGER, "text": "hi"})
    assert res == {"ok": True}
    assert calls == [("crew/nas/cmd", 1, False)]
    assert hass.data[DATA_WEBHOOK].queued("nas") == 0


def test_pairing_code_and_url_helpers() -> None:
    code = webhook_transport.pairing_code("https://h.example.com", "ab" * 32, "s-_x", "mac")
    assert _decode(code) == {"t": "webhook", "u": "https://h.example.com", "w": "ab" * 32, "s": "s-_x", "m": "mac"}
    assert _decode(webhook_transport.mqtt_pairing_code("s-_x", "nas")) == {"t": "mqtt", "m": "nas", "s": "s-_x"}
    norm = webhook_transport.normalize_public_url
    assert norm("https://H.example.com/") == "https://h.example.com"
    assert norm(" https://h.example.com:8443 ") == "https://h.example.com:8443"
    assert norm("https://[::1]:8123") == "https://[::1]:8123"
    for bad in (None, "", "http://h", "https://", "https://h/x", "https://h#f", "https://h:99999", "ftp://h"):
        assert norm(bad) is None, bad
