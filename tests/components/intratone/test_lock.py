"""Lock entity test — unlock triggers the door and reverts to locked."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aioresponses import aioresponses
from homeassistant.components.lock import LockState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intratone.const import (
    API_BASE,
    CONF_JWT,
    DOMAIN,
    PATH_ACCESS_LIST,
    PATH_ACCESS_OPEN,
)


@pytest.fixture
def aiomock():
    with aioresponses() as m:
        yield m


async def test_unlock_calls_open_door_and_reverts(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    """End-to-end: ring → tap Unlock → /answer fires (lazy SIP) → SIP MESSAGE.

    With the deferred-INVITE refactor, `/answer` is no longer called at
    push time. It fires only when the user actually interacts — either by
    opening live view OR by tapping Unlock. This test exercises the
    Unlock-first path."""
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    aiomock.post(
        f"{API_BASE}api/calls/sim-test/answer",
        payload={"error": 0, "state": "ok"},
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    # Trigger a ring WITH SIP creds so the coordinator parks a pending
    # invite (the only path that exercises /answer via lazy SIP).
    await hass.services.async_call(
        DOMAIN,
        "simulate_ring",
        {
            "call_id": "sim-test",
            "door_name": "PORTE RUE",
            "sip_server_ip": "1.2.3.4",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    lock_eid = registry.async_get_entity_id(
        "lock", DOMAIN, f"{mock_entry.entry_id}_door_lock"
    )
    assert lock_eid is not None
    assert hass.states.get(lock_eid).state == LockState.LOCKED

    # Patch the bridge readiness timeout down to a tick so the test doesn't
    # spend 5s waiting for the mocked CallManager that never fires
    # `set_stream_url`.
    from custom_components.intratone import coordinator as coord_mod
    from unittest.mock import patch

    with patch.object(coord_mod, "_STREAM_READY_TIMEOUT_S", 0.05):
        await hass.services.async_call(
            "lock", "unlock", {"entity_id": lock_eid}, blocking=True
        )

    # /answer fired as part of the lazy-INVITE path.
    answer_url = f"{API_BASE}api/calls/sim-test/answer"
    matching = [
        c for key, calls in aiomock.requests.items()
        if str(key[1]) == answer_url
        for c in calls
    ]
    assert len(matching) >= 1


async def test_access_lock_unlock_opens_remote_access(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    """A remote-open access ("Clé mobile" / mobipass) is exposed as a Lock,
    and unlocking it POSTs to /access/open/clemobil."""
    mock_entry.add_to_hass(hass)
    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        payload={
            "state": "ok",
            "data": {
                "list": [
                    {
                        "id": 77,
                        "residence": "Ma résidence",
                        "name": "Portail",
                        "phonenumber": "0612345678",
                        "openmode": "data",
                    }
                ]
            },
        },
    )
    aiomock.post(
        f"{API_BASE}{PATH_ACCESS_OPEN}",
        payload={"error": 0, "state": "ok"},
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    access_eid = registry.async_get_entity_id(
        "lock", DOMAIN, f"{mock_entry.entry_id}_access_77"
    )
    assert access_eid is not None
    assert hass.states.get(access_eid).state == LockState.LOCKED

    await hass.services.async_call(
        "lock", "unlock", {"entity_id": access_eid}, blocking=True
    )

    open_url = f"{API_BASE}{PATH_ACCESS_OPEN}"
    matching = [
        c for key, calls in aiomock.requests.items()
        if str(key[1]) == open_url
        for c in calls
    ]
    assert len(matching) == 1
    assert matching[0].kwargs["data"] == {
        "phonenumber": "0612345678",
        "access_id": "77",
    }


async def test_access_locks_retry_after_transient_connection_error(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock, monkeypatch
) -> None:
    """A network blip on the first fetch is retried and eventually succeeds —
    access lock entities are still created once startup recovers."""
    from custom_components.intratone import lock as lock_mod

    monkeypatch.setattr(lock_mod, "ACCESS_LOCKS_RETRY_DELAYS_S", (0.01,))

    mock_entry.add_to_hass(hass)
    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        exception=aiohttp.ClientConnectionError("reset"),
    )
    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        payload={
            "state": "ok",
            "data": {
                "list": [
                    {
                        "id": 77,
                        "residence": "Ma résidence",
                        "name": "Portail",
                        "phonenumber": "0612345678",
                        "openmode": "data",
                    }
                ]
            },
        },
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    # The retry delay is monkeypatched short but still real (not
    # time-travelled) — give the background task a beat to come back around.
    # FCM's own supervisor background task runs for the entry's lifetime, so
    # `wait_background_tasks=True` would hang forever; a short real sleep
    # plus a plain block_till_done catches the (finite) access-locks task.
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    access_eid = registry.async_get_entity_id(
        "lock", DOMAIN, f"{mock_entry.entry_id}_access_77"
    )
    assert access_eid is not None
    assert hass.states.get(access_eid).state == LockState.LOCKED


async def test_access_locks_retry_after_5xx_api_error(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock, monkeypatch
) -> None:
    """A server 5xx is treated as transient and retried, same as a network
    blip. `list_access` internally refreshes the JWT and retries once on any
    API error (see rest_api.py) before the 5xx ever reaches lock.py, so both
    the first attempt and its internal retry are mocked here."""
    from custom_components.intratone import lock as lock_mod

    monkeypatch.setattr(lock_mod, "ACCESS_LOCKS_RETRY_DELAYS_S", (0.01,))

    mock_entry.add_to_hass(hass)
    error_body = {"state": "error", "message": "server error"}
    aiomock.get(f"{API_BASE}{PATH_ACCESS_LIST}", payload=error_body, status=503)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "newjwt", "id": "3844428"}},
    )
    aiomock.get(f"{API_BASE}{PATH_ACCESS_LIST}", payload=error_body, status=503)
    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        payload={
            "state": "ok",
            "data": {
                "list": [
                    {
                        "id": 77,
                        "residence": "Ma résidence",
                        "name": "Portail",
                        "phonenumber": "0612345678",
                        "openmode": "data",
                    }
                ]
            },
        },
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    access_eid = registry.async_get_entity_id(
        "lock", DOMAIN, f"{mock_entry.entry_id}_access_77"
    )
    assert access_eid is not None


async def test_access_locks_auth_error_is_not_retried(
    hass, mock_entry_data, mock_fcm_client, mock_call_manager, aiomock, caplog
) -> None:
    """An `IntratoneAuthError` (no JWT / credentials revoked) is definitive,
    not a hiccup — no retry, and the same warning as before the fix."""
    entry_data = dict(mock_entry_data)
    del entry_data[CONF_JWT]
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="3844428",
        title="Intratone (0671124546)",
        data=entry_data,
    )
    entry.add_to_hass(hass)

    with caplog.at_level("WARNING", logger="custom_components.intratone.lock"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert "Could not fetch remote-open accesses" in caplog.text
    # No HTTP call at all — `_get_json` raises IntratoneAuthError before ever
    # reaching the network when there's no JWT.
    assert not any(
        str(key[1]) == f"{API_BASE}{PATH_ACCESS_LIST}" for key in aiomock.requests
    )
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "lock", DOMAIN, f"{entry.entry_id}_access_77"
        )
        is None
    )


async def test_unload_during_access_locks_retry_cancels_cleanly(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock, monkeypatch, caplog
) -> None:
    """Unloading the entry while the background task is waiting to retry must
    cancel it cleanly — no warning, no entities, no leaked task."""
    from custom_components.intratone import lock as lock_mod

    monkeypatch.setattr(lock_mod, "ACCESS_LOCKS_RETRY_DELAYS_S", (30.0,))

    mock_entry.add_to_hass(hass)
    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        exception=aiohttp.ClientConnectionError("reset"),
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    # Give the background task a beat to hit the first failure and start its
    # (long) retry wait.
    await asyncio.sleep(0.1)

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert "Could not fetch remote-open accesses" not in caplog.text
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "lock", DOMAIN, f"{mock_entry.entry_id}_access_77"
        )
        is None
    )
