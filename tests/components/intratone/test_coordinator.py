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
