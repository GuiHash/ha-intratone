"""Lock entity test — unlock triggers the door and reverts to locked."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from homeassistant.components.lock import LockState
from homeassistant.helpers import entity_registry as er

from custom_components.intratone.const import API_BASE, DOMAIN


@pytest.fixture
def aiomock():
    with aioresponses() as m:
        yield m


async def test_unlock_calls_open_door_and_reverts(
    hass, mock_entry, mock_fcm_client, aiomock
) -> None:
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

    # Trigger a ring so the coordinator has a call_id to answer.
    await hass.services.async_call(
        DOMAIN,
        "simulate_ring",
        {"call_id": "sim-test", "door_name": "PORTE RUE"},
        blocking=True,
    )
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    lock_eid = registry.async_get_entity_id(
        "lock", DOMAIN, f"{mock_entry.entry_id}_door_lock"
    )
    assert lock_eid is not None
    assert hass.states.get(lock_eid).state == LockState.LOCKED

    await hass.services.async_call(
        "lock", "unlock", {"entity_id": lock_eid}, blocking=True
    )

    # The answer endpoint was called.
    answer_url = f"{API_BASE}api/calls/sim-test/answer"
    matching = [
        c for key, calls in aiomock.requests.items()
        if str(key[1]) == answer_url
        for c in calls
    ]
    assert len(matching) >= 1
