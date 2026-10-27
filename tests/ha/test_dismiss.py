"""0.4.0 crew_hub/dismiss: dismissed agents/sessions are filtered out of the card feed for every subscriber, persisted in
the private Store crew_hub.dismissed (reload + restart), a dismissed session comes back when it works again, admins only,
ids unseen for 24 h are pruned, and commands still see the real snapshot."""

import json

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from homeassistant.core import HomeAssistant

from custom_components.crew_hub.const import DATA_DISMISSED, DATA_STORE, DOMAIN

SECRET = "s3cret-s3cret-s3cret"
KEY = "crew_hub.dismissed"
JAMFIN = "8c1f2a4e-3b7d-4e19-9a61-0f2d5c7b9e10"
ERRDEMO = "e5c7a913-0b2d-4f6e-8a14-c3d5e7f9a1b2"
FINISHED = ["a19b8e7c2d3f4a55", "a5c1e0d2b7f94e31", "a6b0c2d4e8f13579"]  # done, done, stopped
DAY = 24 * 3600 * 1000


async def _setup(hass: HomeAssistant, nas_snapshot) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Crew Hub", data={"prefix": "crew"}, unique_id=DOMAIN,
                            options={"machines": [{"id": "nas", "name": "NAS", "secret": SECRET}]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _push(hass, nas_snapshot)
    return entry


async def _push(hass, snap) -> None:
    # Not retained: HA's MQTT client hands a retained message to an existing subscription only once.
    async_fire_mqtt_message(hass, "crew/nas/status", "online")
    async_fire_mqtt_message(hass, "crew/nas/snapshot", json.dumps(snap))
    await hass.async_block_till_done()


def _sess(snapshot, sid):
    return next((s for s in snapshot["sessions"] if s["id"] == sid), None)


async def _get(client, i):
    await client.send_json({"id": i, "type": "crew_hub/get"})
    msg = await client.receive_json()
    assert msg["success"], msg
    return msg["result"]["machines"]["nas"]["snapshot"]


async def _dismiss(client, i, **kw):
    await client.send_json({"id": i, "type": "crew_hub/dismiss", "machine": "nas", **kw})
    while True:
        msg = await client.receive_json()
        if msg["id"] == i and msg["type"] == "result":
            return msg


async def test_dismiss_filters_feed_and_persists(hass, mqtt_mock, hass_ws_client, hass_storage, nas_snapshot) -> None:
    await _setup(hass, nas_snapshot)
    sub = await hass_ws_client(hass)
    await sub.send_json({"id": 1, "type": "crew_hub/subscribe"})
    assert (await sub.receive_json())["success"]
    full = await sub.receive_json()
    assert full["event"]["machines"]["nas"]["snapshot"] == nas_snapshot  # nothing dismissed: unchanged

    client = await hass_ws_client(hass)
    msg = await _dismiss(client, 1, session=JAMFIN, agents=FINISHED)
    assert msg["success"] and msg["result"] == {"ok": True}
    # the other subscriber gets the filtered machine at once
    ev = (await sub.receive_json())["event"]
    assert ev["type"] == "machine" and ev["id"] == "nas"
    ids = {a["id"] for a in _sess(ev["data"]["snapshot"], JAMFIN)["agents"]}
    assert not ids & set(FINISHED) and "a7f3c2e1d9b84a60" in ids
    msg = await _dismiss(client, 2, session=ERRDEMO, session_dismissed=True)
    assert msg["result"] == {"ok": True}
    snap = await _get(client, 3)
    assert _sess(snap, ERRDEMO) is None
    assert len(snap["sessions"]) == len(nas_snapshot["sessions"]) - 1

    # saved before the result: private Store, ids only
    stored = hass_storage[KEY]["data"]
    assert set(stored["nas"]) == {JAMFIN, ERRDEMO}
    assert stored["nas"][JAMFIN]["agents"] == FINISHED and stored["nas"][ERRDEMO]["session"] is True
    assert "Jamfin" not in json.dumps(stored)

    # a new snapshot keeps the filter; the store itself keeps the real snapshot (commands validate against it)
    await _push(hass, nas_snapshot)
    snap = await _get(client, 4)
    assert _sess(snap, ERRDEMO) is None
    assert not {a["id"] for a in _sess(snap, JAMFIN)["agents"]} & set(FINISHED)
    assert hass.data[DATA_STORE].machine("nas")["snapshot"] == nas_snapshot


async def test_dismissals_survive_reload_and_restart(hass, mqtt_mock, hass_ws_client, hass_storage, nas_snapshot) -> None:
    entry = await _setup(hass, nas_snapshot)
    client = await hass_ws_client(hass)
    assert (await _dismiss(client, 1, session=ERRDEMO, session_dismissed=True))["result"] == {"ok": True}

    # reload (options change): same store, filter still there
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _sess(await _get(client, 2), ERRDEMO) is None

    # restart: the in-memory store and the Store helper are gone; the file brings the dismissal back
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.data.pop(DATA_STORE)
    hass.data.pop(DATA_DISMISSED)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _push(hass, nas_snapshot)
    snap = await _get(client, 3)
    assert _sess(snap, ERRDEMO) is None and _sess(snap, JAMFIN) is not None


async def test_dismissed_session_reappears_when_working(hass, mqtt_mock, hass_ws_client, hass_storage, nas_snapshot) -> None:
    await _setup(hass, nas_snapshot)
    client = await hass_ws_client(hass)
    await _dismiss(client, 1, session=ERRDEMO, session_dismissed=True)
    assert _sess(await _get(client, 2), ERRDEMO) is None

    snap = json.loads(json.dumps(nas_snapshot))
    _sess(snap, ERRDEMO)["status"] = "working"
    await _push(hass, snap)
    assert _sess(await _get(client, 3), ERRDEMO)["status"] == "working"
    # the flag is cleared, so going idle again doesn't hide it
    _sess(snap, ERRDEMO)["status"] = "idle"
    await _push(hass, snap)
    assert _sess(await _get(client, 4), ERRDEMO) is not None
    assert hass.data[DATA_STORE].dismissed_data() == {}

    # a working session can't be hidden at all
    await _dismiss(client, 5, session=JAMFIN, session_dismissed=True)
    assert _sess(await _get(client, 6), JAMFIN) is not None


async def test_non_admin_refused(hass, mqtt_mock, hass_ws_client, hass_read_only_access_token, hass_storage,
                                 nas_snapshot) -> None:
    await _setup(hass, nas_snapshot)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    msg = await _dismiss(client, 1, session=ERRDEMO, session_dismissed=True)
    assert not msg["success"] and msg["error"]["code"] == "unauthorized"
    assert hass.data[DATA_STORE].dismissed_data() == {}
    assert KEY not in hass_storage


async def test_refusals(hass, mqtt_mock, hass_ws_client, nas_snapshot) -> None:
    await _setup(hass, nas_snapshot)
    client = await hass_ws_client(hass)
    assert (await _dismiss(client, 1, session=ERRDEMO))["result"] == {"ok": False, "error": "invalid_args"}
    msg = await _dismiss(client, 2, session=ERRDEMO, agents="nope")
    assert not msg["success"] and msg["error"]["code"] == "invalid_format"
    await client.send_json({"id": 3, "type": "crew_hub/dismiss", "machine": "mac", "session": ERRDEMO, "session_dismissed": True})
    assert (await client.receive_json())["result"] == {"ok": False, "error": "unknown_machine"}


async def test_pruned_after_24h(hass, mqtt_mock, hass_ws_client, hass_storage, nas_snapshot) -> None:
    await _setup(hass, nas_snapshot)
    store = hass.data[DATA_STORE]
    now = [10 * DAY]
    store._clock = lambda: now[0]
    client = await hass_ws_client(hass)
    await _dismiss(client, 1, session=JAMFIN, agents=FINISHED)
    await _dismiss(client, 2, session="session-that-ended", agents=["agent-gone"])
    assert set(hass_storage[KEY]["data"]["nas"]) == {JAMFIN, "session-that-ended"}

    snap = json.loads(json.dumps(nas_snapshot))
    jam = _sess(snap, JAMFIN)
    jam["agents"] = [a for a in jam["agents"] if a["id"] != FINISHED[0]]  # the daemon dropped one agent
    now[0] += DAY + 1
    await _push(hass, snap)
    assert store.dismissed_data()["nas"].keys() == {JAMFIN}
    assert store.dismissed_data()["nas"][JAMFIN]["agents"] == FINISHED[1:]
    # pruning is saved (delayed save, flushed here)
    await hass.data[DATA_DISMISSED].async_save()
    assert set(hass_storage[KEY]["data"]["nas"]) == {JAMFIN}
