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
    coord = IntratoneCoordinator(hass, mock_entry, api)
    cm = MagicMock()
    cm.start_call = AsyncMock(return_value="sip-call-test")
    cm.hang_up = AsyncMock()
    coord.attach_call_manager(cm)
    return coord


_PUSH_WITH_SIP = {
    "call_id": "1",
    "message": "X",
    "LOGIN_TO_CALL": "U",
    "LOGIN": "u",
    "PASS": "p",
    "ip_adress": "1.2.3.4",
}


async def test_stream_source_none_when_no_call(coordinator) -> None:
    cam = IntratoneCamera(coordinator)
    assert await cam.stream_source() is None


async def test_stream_source_none_during_ringing_before_audio_up(coordinator) -> None:
    cam = IntratoneCamera(coordinator)
    await coordinator.async_handle_push(_PUSH_WITH_SIP)
    # Ring received but audio bridge not yet up.
    assert await cam.stream_source() is None


async def test_stream_source_returns_url_when_audio_active(coordinator) -> None:
    """The returned string includes `-rtsp_transport tcp -i …` so HomeKit
    Bridge's ffmpeg uses TCP for the SETUP (go2rtc rejects UDP with 461)."""
    cam = IntratoneCamera(coordinator)
    await coordinator.async_handle_push(_PUSH_WITH_SIP)
    coordinator.set_stream_url("sip-call-test", "rtsp://127.0.0.1:8554/intratone")
    result = await cam.stream_source()
    assert result == "-rtsp_transport tcp -i rtsp://127.0.0.1:8554/intratone"


async def test_stream_source_clears_after_call_ends(coordinator) -> None:
    cam = IntratoneCamera(coordinator)
    await coordinator.async_handle_push(_PUSH_WITH_SIP)
    coordinator.set_stream_url("sip-call-test", "rtsp://127.0.0.1:8554/intratone")
    coordinator.set_stream_url("sip-call-test", None)
    assert await cam.stream_source() is None


async def test_stream_feature_advertised(coordinator) -> None:
    from homeassistant.components.camera import CameraEntityFeature

    cam = IntratoneCamera(coordinator)
    assert cam.supported_features & CameraEntityFeature.STREAM
