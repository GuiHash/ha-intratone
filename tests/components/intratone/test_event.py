"""Doorbell event entity tests — firing, dedup and ghost-ring protection."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from aioresponses import aioresponses
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er

from custom_components.intratone.const import API_BASE, DOMAIN


@pytest.fixture
def aiomock():
    with aioresponses() as m:
        yield m


_RING = {
    "call_id": "300705065",
    "message": "PORTE RUE",
    "NBPORTE": "1",
    "LOGIN_TO_CALL": "SIPLOGIN",
}


async def _setup_entry(hass, mock_entry, aiomock) -> str:
    """Set up the config entry and return the doorbell event entity_id."""
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    eid = er.async_get(hass).async_get_entity_id(
        "event", DOMAIN, f"{mock_entry.entry_id}_doorbell"
    )
    assert eid
    return eid


async def test_event_fires_on_new_ring(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    eid = await _setup_entry(hass, mock_entry, aiomock)
    assert hass.states.get(eid).state == "unknown"

    await mock_entry.runtime_data.coordinator.async_handle_push(_RING)
    await hass.async_block_till_done()

    state = hass.states.get(eid)
    assert state.state != "unknown"
    assert state.attributes["event_type"] == "ring"
    assert state.attributes["door_name"] == "PORTE RUE"
    assert state.attributes["door_number"] == "1"
    assert state.attributes["caller"] == "SIPLOGIN"
    assert state.attributes["call_id"] == "300705065"
    # hardware_* attributes are absent when the push doesn't carry them.
    for key in ("hardware_name", "hardware_type", "hardware_id"):
        assert key not in state.attributes


async def test_event_dedups_same_ring_seq(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock, freezer
) -> None:
    """A coordinator write without a new ring (what `set_stream_url` does
    when the call ends) must not re-fire the doorbell event."""
    eid = await _setup_entry(hass, mock_entry, aiomock)
    coordinator = mock_entry.runtime_data.coordinator
    await coordinator.async_handle_push(_RING)
    await hass.async_block_till_done()
    before = hass.states.get(eid).state

    freezer.tick(timedelta(minutes=5))
    coordinator.async_set_updated_data(
        dataclasses.replace(coordinator.data, stream_url=None)
    )
    await hass.async_block_till_done()

    assert hass.states.get(eid).state == before


async def test_fresh_entity_ignores_preexisting_ring(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock, freezer
) -> None:
    """An entity (re-)added while the coordinator still holds a past call
    must not fire a ghost ring on the next coordinator write."""
    eid = await _setup_entry(hass, mock_entry, aiomock)
    coordinator = mock_entry.runtime_data.coordinator
    await coordinator.async_handle_push(_RING)
    await hass.async_block_till_done()
    before = hass.states.get(eid).state

    # Recreate the entity against the same, still-populated coordinator —
    # what a disable/re-enable of the entity amounts to.
    assert await hass.config_entries.async_unload_platforms(
        mock_entry, [Platform.EVENT]
    )
    await hass.config_entries.async_forward_entry_setups(
        mock_entry, [Platform.EVENT]
    )
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == before

    freezer.tick(timedelta(minutes=5))
    coordinator.async_set_updated_data(
        dataclasses.replace(coordinator.data, stream_url=None)
    )
    await hass.async_block_till_done()

    assert hass.states.get(eid).state == before


async def test_hardware_attributes_surfaced_when_populated(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    eid = await _setup_entry(hass, mock_entry, aiomock)
    await mock_entry.runtime_data.coordinator.async_handle_push(
        {
            **_RING,
            "hardwareName": "Interphone Rue",
            "hardwareType": "V4",
            "hardwareId": "42",
        }
    )
    await hass.async_block_till_done()

    state = hass.states.get(eid)
    assert state.attributes["event_type"] == "ring"
    assert state.attributes["hardware_name"] == "Interphone Rue"
    assert state.attributes["hardware_type"] == "V4"
    assert state.attributes["hardware_id"] == "42"
