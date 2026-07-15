"""Camera entity tests — stream_source reflects coordinator state."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.intratone.camera import IntratoneCamera


_RELAY_URL = "rtsp://127.0.0.1:8554/intratone"


@pytest.fixture
def coordinator(hass, mock_entry):
    from custom_components.intratone.coordinator import IntratoneCoordinator

    api = MagicMock()
    api.answer_call = AsyncMock(return_value=True)
    coord = IntratoneCoordinator(hass, mock_entry, api)
    cm = MagicMock()
    cm.start_call = AsyncMock(return_value="sip-call-test")
    cm.hang_up = AsyncMock()
    cm.abort_active_call = AsyncMock()
    cm.active_call_id = None
    cm.relay_rtsp_url = _RELAY_URL
    coord.attach_call_manager(cm)
    return coord


class _RecordingProvider:
    """Fake WebRTC provider capturing what stream_source it was offered."""

    domain = "fake_go2rtc"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def async_is_supported(self, stream_source: str) -> bool:
        self.seen.append(stream_source)
        return True


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
    """If the user opens live view but the audio bridge never comes up
    (network failure, server BYE during setup), `stream_source` returns
    None after the wait timeout — HomeKit falls back to the placeholder."""
    from custom_components.intratone import coordinator as coord_mod
    from unittest.mock import patch

    cam = IntratoneCamera(coordinator)
    await coordinator.async_handle_push(_PUSH_WITH_SIP)
    # Bridge never fires set_stream_url → wait_for_stream returns None.
    with patch.object(coord_mod, "_STREAM_READY_TIMEOUT_S", 0.05):
        assert await cam.stream_source() is None


async def test_stream_source_returns_url_when_audio_active(coordinator) -> None:
    """A bare RTSP URL is returned — consumed by HomeKit (ffmpeg falls back
    to TCP itself after go2rtc's 461 on the UDP SETUP), HA's go2rtc WebRTC
    provider, and the stream/HLS component (which forces prefer_tcp for
    rtsp:// sources).

    With deferred INVITE, `stream_source()` is itself the trigger for the
    SIP call. We simulate the bridge firing `set_stream_url` shortly after
    `ensure_call_started` returns (which is what production does)."""
    import asyncio as _asyncio

    cam = IntratoneCamera(coordinator)
    await coordinator.async_handle_push(_PUSH_WITH_SIP)

    async def fire_ready_after_start() -> None:
        # Wait one tick so ensure_call_started has set _active_sip_call_id.
        await _asyncio.sleep(0.01)
        coordinator.set_stream_url("sip-call-test", _RELAY_URL)

    _asyncio.create_task(fire_ready_after_start())
    result = await cam.stream_source()
    assert result == _RELAY_URL
    # A real request during a ring IS the pick-up signal → SIP dial.
    coordinator._call_manager.start_call.assert_awaited_once()


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


async def test_provider_refresh_gets_static_url_at_idle(hass, coordinator) -> None:
    """Provider selection (entity added / go2rtc entry loaded) must see the
    static relay URL even with no call — otherwise the camera never acquires
    the WEB_RTC capability. It must NOT dial SIP."""
    from homeassistant.components.camera.webrtc import DATA_WEBRTC_PROVIDERS

    provider = _RecordingProvider()
    hass.data[DATA_WEBRTC_PROVIDERS] = {provider}
    cam = IntratoneCamera(coordinator)
    cam.hass = hass

    await cam.async_refresh_providers(write_state=False)

    assert provider.seen == [_RELAY_URL]
    assert cam._webrtc_provider is provider
    coordinator._call_manager.start_call.assert_not_called()


async def test_provider_refresh_during_ring_does_not_dial(hass, coordinator) -> None:
    """A provider refresh can fire mid-ring (go2rtc entry reload). It must
    never auto-answer: no SIP INVITE, no REST /answer."""
    from homeassistant.components.camera.webrtc import DATA_WEBRTC_PROVIDERS

    provider = _RecordingProvider()
    hass.data[DATA_WEBRTC_PROVIDERS] = {provider}
    cam = IntratoneCamera(coordinator)
    cam.hass = hass
    await coordinator.async_handle_push(_PUSH_WITH_SIP)

    await cam.async_refresh_providers(write_state=False)

    assert provider.seen == [_RELAY_URL]
    coordinator._call_manager.start_call.assert_not_called()
    coordinator.api.answer_call.assert_not_called()
