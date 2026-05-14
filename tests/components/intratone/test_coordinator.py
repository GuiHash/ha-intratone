"""Coordinator unit tests — ring sequencing and open_door wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.intratone.coordinator import IntratoneCoordinator


@pytest.fixture
def coordinator(hass, mock_entry):
    api = MagicMock()
    api.answer_call = AsyncMock(return_value=True)
    return IntratoneCoordinator(hass, mock_entry, api)


@pytest.fixture
def coordinator_with_cm(coordinator):
    cm = MagicMock()
    cm.start_call = AsyncMock(return_value="fake-call-id")
    cm.hang_up = AsyncMock()
    coordinator.attach_call_manager(cm)
    return coordinator, cm


_FULL_PUSH = {
    "call_id": "300705065",
    "message": "PORTE RUE",
    "LOGIN_TO_CALL": "2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36",
    "LOGIN": "cogelecTest",
    "PASS": "CogeleC",
    "ip_adress": "178.32.84.135",
}


async def test_handle_push_increments_ring_seq(coordinator) -> None:
    await coordinator.async_handle_push(
        {"call_id": "1", "message": "PORTE RUE", "LOGIN_TO_CALL": "ABC"}
    )
    assert coordinator.data is not None
    assert coordinator.data.ring_seq == 1
    assert coordinator.data.door_name == "PORTE RUE"
    assert coordinator.data.call_id == "1"

    await coordinator.async_handle_push({"call_id": "2", "message": "PORTE COUR"})
    assert coordinator.data.ring_seq == 2
    assert coordinator.data.call_id == "2"


async def test_handle_push_without_call_id_is_ignored(coordinator) -> None:
    await coordinator.async_handle_push({"message": "no call id"})
    assert coordinator.data is None


async def test_open_door_with_no_call_returns_false(coordinator) -> None:
    assert await coordinator.async_open_door() is False
    coordinator.api.answer_call.assert_not_awaited()


async def test_open_door_calls_api_with_current_call_id(coordinator) -> None:
    await coordinator.async_handle_push({"call_id": "42", "message": "X"})
    assert await coordinator.async_open_door() is True
    coordinator.api.answer_call.assert_awaited_once_with("42")


# --- Phase 2 SIP / audio wiring --------------------------------------------


async def test_push_with_sip_creds_triggers_call_manager(
    coordinator_with_cm,
) -> None:
    coordinator, cm = coordinator_with_cm
    await coordinator.async_handle_push(_FULL_PUSH)
    cm.start_call.assert_awaited_once_with(
        target_uri="sip:2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36@178.32.84.135",
        target_host="178.32.84.135",
        sip_username="cogelecTest",
        sip_password="CogeleC",
    )


async def test_push_without_sip_creds_does_not_call_sip(
    coordinator_with_cm,
) -> None:
    coordinator, cm = coordinator_with_cm
    await coordinator.async_handle_push({"call_id": "1", "message": "X"})
    cm.start_call.assert_not_awaited()


async def test_set_stream_url_updates_call_state(coordinator) -> None:
    await coordinator.async_handle_push({"call_id": "1", "message": "X"})
    assert coordinator.data.stream_url is None

    coordinator.set_stream_url("1", "rtsp://127.0.0.1:8556/intratone")
    assert coordinator.data.stream_url == "rtsp://127.0.0.1:8556/intratone"

    coordinator.set_stream_url("1", None)
    assert coordinator.data.stream_url is None


async def test_set_stream_url_ignored_for_stale_call_id(coordinator) -> None:
    await coordinator.async_handle_push({"call_id": "current", "message": "X"})
    coordinator.set_stream_url("stale", "rtsp://x")
    assert coordinator.data.stream_url is None


async def test_open_door_hangs_up_call_manager(coordinator_with_cm) -> None:
    coordinator, cm = coordinator_with_cm
    await coordinator.async_handle_push({"call_id": "42", "message": "X"})
    await coordinator.async_open_door()
    cm.hang_up.assert_awaited_once()


async def test_open_door_skips_hangup_when_api_fails(coordinator_with_cm) -> None:
    coordinator, cm = coordinator_with_cm
    coordinator.api.answer_call.return_value = False
    await coordinator.async_handle_push({"call_id": "42", "message": "X"})
    await coordinator.async_open_door()
    cm.hang_up.assert_not_awaited()
