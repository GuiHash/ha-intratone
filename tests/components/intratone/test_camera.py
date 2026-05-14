"""Camera entity tests — stream_source reflects coordinator state."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.intratone.camera import IntratoneCamera


@pytest.fixture
def coordinator(hass, mock_entry):
    from custom_components.intratone.coordinator import IntratoneCoordinator

    api = MagicMock()
    api.answer_call = AsyncMock(return_value=True)
    return IntratoneCoordinator(hass, mock_entry, api)


async def test_stream_source_none_when_no_call(coordinator) -> None:
    cam = IntratoneCamera(coordinator)
    assert await cam.stream_source() is None


async def test_stream_source_none_during_ringing_before_audio_up(coordinator) -> None:
    cam = IntratoneCamera(coordinator)
    await coordinator.async_handle_push({"call_id": "1", "message": "X"})
    # Ring received but audio bridge not yet up.
    assert await cam.stream_source() is None


async def test_stream_source_returns_url_when_audio_active(coordinator) -> None:
    cam = IntratoneCamera(coordinator)
    await coordinator.async_handle_push({"call_id": "1", "message": "X"})
    coordinator.set_stream_url("1", "rtsp://127.0.0.1:8556/intratone")
    assert await cam.stream_source() == "rtsp://127.0.0.1:8556/intratone"


async def test_stream_source_clears_after_call_ends(coordinator) -> None:
    cam = IntratoneCamera(coordinator)
    await coordinator.async_handle_push({"call_id": "1", "message": "X"})
    coordinator.set_stream_url("1", "rtsp://127.0.0.1:8556/intratone")
    coordinator.set_stream_url("1", None)
    assert await cam.stream_source() is None


async def test_stream_feature_advertised(coordinator) -> None:
    from homeassistant.components.camera import CameraEntityFeature

    cam = IntratoneCamera(coordinator)
    assert cam.supported_features & CameraEntityFeature.STREAM
