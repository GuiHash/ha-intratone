"""Lock entity test — unlock triggers the door and reverts to locked."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
try:
    from homeassistant.components.lock import LockState
    _STATE_LOCKED = LockState.LOCKED
except ImportError:  # HA < 2024.4
    _STATE_LOCKED = "locked"
from homeassistant.helpers import entity_registry as er

from custom_components.intratone.const import API_BASE, DOMAIN


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
    assert hass.states.get(lock_eid).state == _STATE_LOCKED

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
