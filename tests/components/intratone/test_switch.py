"""Backlight switch — momentary trigger that forwards to the coordinator."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.intratone.switch import IntratoneBacklightSwitch


@pytest.fixture
def fake_coordinator(hass, mock_entry):
    coord = MagicMock()
    coord.entry = mock_entry
    coord.async_toggle_backlight = AsyncMock(return_value=True)
    coord.last_update_success = True
    return coord


async def test_turn_on_invokes_coordinator_and_flips_state(hass, fake_coordinator):
    """Tapping the switch ON in HomeKit forwards to the coordinator,
    momentarily flips the local state, then reverts after the auto-off
    window."""
    from custom_components.intratone import switch as switch_mod

    switch = IntratoneBacklightSwitch(fake_coordinator)
    switch.hass = hass
    switch.async_write_ha_state = MagicMock()

    # Short revert delay so the test doesn't hang.
    switch_mod._VISIBLE_ON_S = 0.01

    await switch.async_turn_on()

    fake_coordinator.async_toggle_backlight.assert_awaited_once()
    assert switch.is_on is True
    await asyncio.sleep(0.05)
    assert switch.is_on is False


async def test_turn_on_skipped_when_no_call(hass, fake_coordinator):
    """If the coordinator can't send the signal (no active SIP), the
    switch stays off — no fake UI feedback."""
    fake_coordinator.async_toggle_backlight = AsyncMock(return_value=False)
    switch = IntratoneBacklightSwitch(fake_coordinator)
    switch.hass = hass
    switch.async_write_ha_state = MagicMock()

    await switch.async_turn_on()

    assert switch.is_on is False


async def test_turn_off_is_idempotent_local_only(hass, fake_coordinator):
    """async_turn_off is local-only — server resets the backlight on BYE."""
    switch = IntratoneBacklightSwitch(fake_coordinator)
    switch.hass = hass
    switch.async_write_ha_state = MagicMock()
    switch._attr_is_on = True

    await switch.async_turn_off()

    assert switch.is_on is False
    # No call to the coordinator — purely UI feedback.
    fake_coordinator.async_toggle_backlight.assert_not_called()
