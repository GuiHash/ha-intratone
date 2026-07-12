"""go2rtc self-test button — runs the check and drives the repair issue."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.intratone.button import IntratoneGo2rtcTestButton


@pytest.fixture
def fake_coordinator(hass, mock_entry):
    coord = MagicMock()
    coord.entry = mock_entry
    coord.last_update_success = True
    runtime = MagicMock()
    runtime.call_manager.active_call_id = None
    runtime.call_manager.relay_rtsp_url = "rtsp://127.0.0.1:8554/intratone"
    mock_entry.runtime_data = runtime
    return coord


def _button(hass, coordinator) -> IntratoneGo2rtcTestButton:
    button = IntratoneGo2rtcTestButton(coordinator)
    button.hass = hass
    return button


async def test_press_success_clears_issue(hass, fake_coordinator) -> None:
    button = _button(hass, fake_coordinator)
    with (
        patch(
            "custom_components.intratone.button.async_selftest_go2rtc",
            new=AsyncMock(return_value=None),
        ) as selftest,
        patch(
            "custom_components.intratone.button.report_relay_status"
        ) as report,
    ):
        await button.async_press()

    selftest.assert_awaited_once_with("rtsp://127.0.0.1:8554/intratone")
    report.assert_called_once_with(hass, fake_coordinator.entry, True)


async def test_press_failure_raises_and_reports(hass, fake_coordinator) -> None:
    button = _button(hass, fake_coordinator)
    with (
        patch(
            "custom_components.intratone.button.async_selftest_go2rtc",
            new=AsyncMock(return_value="go2rtc_unreachable"),
        ),
        patch(
            "custom_components.intratone.button.report_relay_status"
        ) as report,
        pytest.raises(HomeAssistantError),
    ):
        await button.async_press()

    report.assert_called_once_with(hass, fake_coordinator.entry, False)


async def test_press_refused_during_active_call(hass, fake_coordinator) -> None:
    """A live call already exercises the real path — and the self-test would
    fight it for the go2rtc slot. No self-test, no issue change."""
    fake_coordinator.entry.runtime_data.call_manager.active_call_id = "call-1"
    button = _button(hass, fake_coordinator)
    with (
        patch(
            "custom_components.intratone.button.async_selftest_go2rtc",
            new=AsyncMock(),
        ) as selftest,
        patch(
            "custom_components.intratone.button.report_relay_status"
        ) as report,
        pytest.raises(HomeAssistantError),
    ):
        await button.async_press()

    selftest.assert_not_awaited()
    report.assert_not_called()
