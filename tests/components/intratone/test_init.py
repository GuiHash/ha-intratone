"""End-to-end test: load entry → fake FCM push → event fires → button → API call."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from homeassistant.helpers import entity_registry as er

from custom_components.intratone.const import API_BASE, DOMAIN


@pytest.fixture
def aiomock():
    with aioresponses() as m:
        yield m


async def test_full_setup_push_open_door(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    mock_entry.add_to_hass(hass)

    # Allow JWT refresh tick on first setup if it happens.
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    # Door open call.
    aiomock.post(
        f"{API_BASE}api/calls/300705065/answer",
        payload={"error": 0, "state": "ok"},
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    event_eid = registry.async_get_entity_id(
        "event", DOMAIN, f"{mock_entry.entry_id}_doorbell"
    )
    lock_eid = registry.async_get_entity_id(
        "lock", DOMAIN, f"{mock_entry.entry_id}_door_lock"
    )
    camera_eid = registry.async_get_entity_id(
        "camera", DOMAIN, f"{mock_entry.entry_id}_camera"
    )
    assert event_eid and lock_eid and camera_eid

    # Inject a fake FCM push by calling the coordinator directly.
    runtime = mock_entry.runtime_data
    await runtime.coordinator.async_handle_push(
        {
            "call_id": "300705065",
            "message": "PORTE RUE",
            "LOGIN_TO_CALL": "2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36",
            "LOGIN": "cogelecTest",
            "PASS": "CogeleC",
            "ip_adress": "178.32.84.135",
        }
    )
    await hass.async_block_till_done()

    event_state = hass.states.get(event_eid)
    assert event_state.attributes.get("event_type") == "ring"
    assert event_state.attributes.get("door_name") == "PORTE RUE"

    # Unlock the door via the lock entity.
    await hass.services.async_call(
        "lock",
        "unlock",
        {"entity_id": lock_eid},
        blocking=True,
    )

    answer_url = f"{API_BASE}api/calls/300705065/answer"
    matching_calls = [
        call for key, calls in aiomock.requests.items()
        if str(key[1]) == answer_url
        for call in calls
    ]
    assert len(matching_calls) >= 1

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    mock_fcm_client.instance.stop.assert_awaited()


async def test_simulate_ring_service(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    from custom_components.intratone.const import DOMAIN

    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        "simulate_ring",
        {"door_name": "PORTE COUR", "call_id": "sim-test"},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = mock_entry.runtime_data.coordinator.data
    assert state is not None
    assert state.call_id == "sim-test"
    assert state.door_name == "PORTE COUR"
