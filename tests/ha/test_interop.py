"""Interoperability: the REAL machine side (plugins/crew-hub/lib/crewhub: pair.py, daemon.py, webhook.py,
commands.py) against the REAL HA integration (custom_components/crew_hub), over real HTTP on loopback.

HA side: the aiohttp test server behind hass_client_no_auth (a real listening socket on 127.0.0.1).
Machine side: crew-hub-pair --stdin --no-daemon and crew-hubd's Daemon loop, both in threads, in a temp HOME with
CREWHUB_TEST_ALLOW_HTTP=1 (the machine side's own test-only switch for plain http on loopback).

Test-only deviations, nothing else is faked:
- TEST-ONLY: config_flow.normalize_public_url is patched to also accept http://127.0.0.1:<port>, because the real
  check (rightly) only accepts https and the test server speaks plain http. The pairing code itself is made by the
  real options flow.
- crewhub.inbox.send_frame is replaced by a recorder (it would write to a Claude Code session's inbox socket); the
  executor's lookup of the live session (inbox.find_live) is real, against a registry entry for this test process.
- webhook_transport.POLL_HOLD is cut from 25 s to 2 s so the machine's held polls turn over quickly.
"""

import asyncio
import io
import json
import os
import sys
import threading
import time
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.crew_hub import config_flow, webhook_transport
from custom_components.crew_hub.const import DATA_PENDING, DATA_STORE, DOMAIN

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
import _util  # noqa: E402  (puts plugins/crew-hub/lib on sys.path)

from crewhub import config as m_config  # noqa: E402
from crewhub import daemon as m_daemon  # noqa: E402
from crewhub import inbox as m_inbox  # noqa: E402
from crewhub import pair as m_pair  # noqa: E402
from crewhub import webhook as m_webhook  # noqa: E402
from crewhub.redact import redact  # noqa: E402

SESSION = "7c1addf5-73c5-41d7-9225-77ba4bbbb2d5"  # the session in the first sample hook payloads
_real_normalize = webhook_transport.normalize_public_url


def _normalize_allow_loopback_http(value):
    """TEST-ONLY: the real normalize_public_url, plus plain http for the loopback test server."""
    if isinstance(value, str) and value.startswith("http://127.0.0.1:"):
        return value.rstrip("/")
    return _real_normalize(value)


async def _until(cond, timeout=8.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _write_spool_and_registry(home: str) -> None:
    """A spool with the sample session, and a live registry entry (this process) with an inbox key for it."""
    hub = os.path.join(home, ".claude", "crew-hub")
    t0 = time.time()
    with open(os.path.join(hub, "spool.jsonl"), "a") as f:
        for i, p in enumerate(_util.sample_payloads()[:5]):
            f.write(json.dumps(redact(p, t0 + i * 0.001, None, None)) + "\n")
    sessions = os.path.join(home, ".claude", "sessions")
    os.makedirs(sessions, 0o700)
    pid = os.getpid()
    start = m_inbox.proc_start(pid)
    sock = os.path.join(home, "inbox.sock")
    open(sock, "w").close()  # inbox_ready only checks that it exists; send_frame is replaced
    entry = {"pid": pid, "procStart": start, "sessionId": SESSION, "kind": "interactive", "name": "interop",
             "messagingSocketPath": sock, "updatedAt": int(t0 * 1000)}
    with open(os.path.join(sessions, f"{pid}.json"), "w") as f:
        json.dump(entry, f)
    with open(m_inbox.key_path(sessions, entry), "w") as f:
        json.dump({"peerToken": "interop-token", "procStart": start}, f)


def _run_pair(code: str) -> tuple[int, str]:
    """crew-hub-pair --stdin --no-daemon with the code on stdin (runs in an executor thread)."""
    out = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(code + "\n")), patch.object(sys, "stdout", out), \
            patch.object(sys, "stderr", out):
        rc = m_pair.main(["--stdin", "--no-daemon", "--name", "MacBook"])
    return rc, out.getvalue()


async def _add_machine(hass: HomeAssistant, entry, public_url: str) -> str:
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "add"})
    result = await flow.async_configure(
        result["flow_id"], {"id": "mac", "name": "MacBook", "transport": "webhook", "public_url": public_url})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pair"
    code = result["description_placeholders"]["code"]
    result = await flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return code


async def _repair(hass: HomeAssistant, entry) -> str:
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "edit"})
    result = await flow.async_configure(result["flow_id"], {"id": "mac"})
    result = await flow.async_configure(result["flow_id"], {"name": "MacBook", "repair": True})
    assert result["step_id"] == "pair"
    code = result["description_placeholders"]["code"]
    result = await flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return code


async def test_machine_and_ha_interoperate(hass, mqtt_mock, hass_client_no_auth, hass_ws_client, tmp_path,
                                           monkeypatch) -> None:
    home = str(tmp_path / "home")
    os.makedirs(home)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv(m_config.TEST_HTTP_ENV, "1")
    monkeypatch.setattr(webhook_transport, "POLL_HOLD", 2.0)
    monkeypatch.setattr(config_flow, "normalize_public_url", _normalize_allow_loopback_http)  # TEST-ONLY, see top
    sent_frames = []
    monkeypatch.setattr(m_inbox, "send_frame", lambda sessions_dir, entry, frame, timeout=5.0:
                        sent_frames.append((entry.get("sessionId"), frame)))

    # ---- HA: crew_hub + a real listening HTTP server ----
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Crew Hub", data={"prefix": "crew"}, unique_id=DOMAIN,
                            options={"machines": []})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    base = f"http://127.0.0.1:{client.server.port}"
    assert client.server.host == "127.0.0.1"
    store = hass.data[DATA_STORE]

    # ---- 1. pairing code from the real options flow ----
    code = await _add_machine(hass, entry, base)

    # ---- 2. machine side: crew-hub-pair --stdin --no-daemon ----
    rc, out = await hass.async_add_executor_job(_run_pair, code)
    assert rc == 0, out
    assert "paired as mac with 127.0.0.1" in out
    assert code not in out
    assert store.status("mac") == "online"  # the hello was accepted
    cfg = m_config.load()
    assert (cfg.transport, cfg.machine_id, cfg.machine_name) == ("webhook", "mac", "MacBook")
    assert cfg.webhook["port"] == client.server.port and cfg.commands.active

    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "crew_hub/subscribe"})
    assert (await ws.receive_json())["success"]
    full = await ws.receive_json()
    assert full["event"]["machines"]["mac"]["status"] == "online"

    # ---- 3. machine side: crew-hubd's Daemon loop in a thread ----
    _write_spool_and_registry(home)
    log_lines: list[str] = []
    log_file = m_daemon.Log(os.path.join(home, ".claude", "crew-hub", "crew-hubd.log"))

    def log(msg):
        log_lines.append(msg)
        log_file(msg)

    daemon = m_daemon.Daemon(log)
    assert isinstance(daemon.link, m_webhook.WebhookLink)
    errors = []

    def run():
        try:
            daemon.run()
        except BaseException as err:  # noqa: BLE001 - reported by the test
            errors.append(err)

    thread = threading.Thread(target=run, name="crew-hubd-interop", daemon=True)
    thread.start()
    try:
        # ---- 4a. snapshot posted by the machine reaches the store and the websocket ----
        await _until(lambda: (store.machine("mac") or {}).get("snapshot") is not None, what="snapshot in store")
        snap = store.machine("mac")["snapshot"]
        assert snap["machine"]["id"] == "mac" and snap["machine"]["name"] == "MacBook"
        assert snap["machine"]["commands"]["enabled"] is True
        session = next(s for s in snap["sessions"] if s["id"] == SESSION)
        assert "send" in session["controls"]
        while True:
            msg = await asyncio.wait_for(ws.receive_json(), 5)
            ev = msg["event"]
            if ev["type"] == "machine" and ev["id"] == "mac" and ev["data"]["snapshot"] is not None:
                break
        assert ev["data"]["status"] == "online" and ev["data"]["snapshot"]["machine"]["id"] == "mac"

        # ---- 4b. command over the websocket -> poll -> verify -> executor -> ack -> websocket result ----
        await ws.send_json({"id": 2, "type": "crew_hub/command", "machine": "mac", "action": "send",
                            "args": {"session": SESSION, "text": "hello from interop", "urgent": True}})
        while True:
            msg = await asyncio.wait_for(ws.receive_json(), 15)
            if msg.get("id") == 2 and msg.get("type") == "result":
                break
        assert msg["success"] and msg["result"] == {"ok": True}, msg
        assert len(sent_frames) == 1
        sid, frame = sent_frames[0]
        assert sid == SESSION
        assert frame["priority"] == "now"
        assert frame["message"] == {"role": "user", "content": "[From Isaac via Crew Hub] hello from interop"}
        assert hass.data[DATA_PENDING] == {}
        assert any(line.startswith("cmd send id=") and line.endswith(" ok") for line in log_lines), log_lines

        # ---- 4c. a request signed with the wrong secret is rejected (401) ----
        bad_target = dict(cfg.webhook, secret="not-the-secret-not-the-secret-0000")
        try:
            await hass.async_add_executor_job(m_webhook.post_once, bad_target, "mac", "hello", {"version": "x"})
        except m_webhook.HttpStatus as err:
            assert err.status == 401
        else:
            raise AssertionError("a wrongly signed request was accepted")
        # the right secret still works through the same code path
        assert await hass.async_add_executor_job(
            m_webhook.post_once, cfg.webhook, "mac", "hello", {"version": "x"}) == {"ok": True}

        # ---- 4d. re-pair: the old webhook stops working ----
        new_code = await _repair(hass, entry)
        assert new_code != code
        assert store.status("mac") == "offline"  # old credentials dropped
        try:
            await hass.async_add_executor_job(m_webhook.post_once, cfg.webhook, "mac", "hello", {"version": "x"})
        except m_webhook.BadResponse as err:
            assert "unknown webhook" in str(err)
        else:
            raise AssertionError("the old webhook still answers")
        # the running daemon (still on the old pairing) is refused too and never brings the machine back online
        await _until(lambda: any("unknown webhook" in line for line in log_lines), timeout=10,
                     what="daemon noticing the old webhook is gone")
        assert store.status("mac") == "offline"

        # pairing again with the new code works
        rc, out = await hass.async_add_executor_job(_run_pair, new_code)
        assert rc == 0 and "paired as mac" in out, out
        assert store.status("mac") == "online"
    finally:
        daemon.stop = True
        end = time.monotonic() + 15
        while thread.is_alive() and time.monotonic() < end:
            await asyncio.sleep(0.05)
        assert not thread.is_alive(), "crew-hubd thread did not stop"
        thread.join()
    assert errors == []
    assert not daemon.link.sender.is_alive() and not daemon.link.poller.is_alive()
    worker = daemon.commands.worker
    assert worker is None or not worker.is_alive()

    # nothing secret in the machine's log
    with open(os.path.join(home, ".claude", "crew-hub", "secrets.json")) as f:
        secrets_ = json.load(f)
    text = "\n".join(log_lines)
    for bad in (secrets_["webhookId"], secrets_["commandSecret"], cfg.webhook["id"], cfg.webhook["secret"]):
        assert bad not in text

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
