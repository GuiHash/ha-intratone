"""FCM-connected diagnostic binary_sensor tracks FcmListener state."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from homeassistant.helpers import entity_registry as er

from custom_components.intratone.const import API_BASE, DOMAIN


@pytest.fixture
def aiomock():
    with aioresponses() as m:
        yield m


async def test_fcm_connected_sensor_reflects_listener_state(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    sensor_eid = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{mock_entry.entry_id}_fcm_connected"
    )
    assert sensor_eid is not None

    # mock_fcm_client makes start() succeed, so the supervisor reaches the
    # `_set_connected(True)` line in `_run_once` and the sensor is `on`.
    assert hass.states.get(sensor_eid).state == "on"

    # Fanned-out state transitions update the sensor.
    fcm = mock_entry.runtime_data.fcm
    fcm._set_connected(False)
    await hass.async_block_till_done()
    assert hass.states.get(sensor_eid).state == "off"

    fcm._set_connected(True)
    await hass.async_block_till_done()
    assert hass.states.get(sensor_eid).state == "on"


async def test_fcm_state_listener_unsubscribe_is_clean(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    """async_will_remove_from_hass must drop the entity's callback so it
    doesn't fire on a stale ref after the entity is gone."""
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    fcm = mock_entry.runtime_data.fcm
    assert len(fcm._state_listeners) == 1

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert fcm._state_listeners == []
