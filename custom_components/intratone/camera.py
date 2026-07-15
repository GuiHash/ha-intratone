"""Camera entity for the Intratone intercom.

Only created when the `video_enabled` option is on — without VP8 video the
stream would just show the synthetic placeholder frame (a black screen).

`stream_source()` serves three consumers with one bare RTSP URL:

- **HomeKit Bridge** (`homekit/type_cameras.py`) prepends `-i ` and hands it
  to ffmpeg. go2rtc's RTSP server rejects UDP SETUP with 461, but ffmpeg's
  rtsp demuxer silently retries the next transport (TCP) — verified against
  go2rtc 1.9.14 / ffmpeg 8.1.2.
- **HA's go2rtc WebRTC provider** validates the URL scheme, registers it in
  HA's go2rtc and serves WebRTC to the frontend / Companion apps.
- **The `stream` component** (HLS fallback) opens it with `prefer_tcp`.

Behavior by state:

- Provider refresh (entity added, go2rtc entry loaded — even mid-ring):
  return the static relay URL, NEVER dial. Selection must see a valid
  scheme URL at idle or the camera never acquires the WEB_RTC capability;
  and dialing here would auto-answer a ring without user intent.
- Real request during a ring: the request IS the "user picked up" signal —
  trigger the lazy SIP INVITE, wait for the audio bridge, return the URL.
- Real request with an active/grace-period call: return the live URL.
- Real request at idle: return None. HomeKit falls back to the still-image
  placeholder; HA's go2rtc provider raises immediately (clear frontend
  error instead of a dead-slot pull timeout). Side effect inherited from
  core: `go2rtc/__init__.py::_update_stream_source` runs a provider-wide
  teardown() before raising, momentarily re-signaling other go2rtc cameras.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntratoneConfigEntry
from .const import CONF_VIDEO_ENABLED
from .entity import IntratoneEntity, async_remove_stale_entity

_LOGGER = logging.getLogger(__name__)

_PLACEHOLDER = Path(__file__).parent / "assets" / "doorbell_idle.jpg"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntratoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if not entry.options.get(CONF_VIDEO_ENABLED, False):
        async_remove_stale_entity(hass, "camera", f"{entry.entry_id}_camera")
        return
    async_add_entities([IntratoneCamera(entry.runtime_data.coordinator)])


class IntratoneCamera(IntratoneEntity, Camera):
    """Doorbell camera. Audio stream only during active calls."""

    _attr_translation_key = "intercom"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator) -> None:
        IntratoneEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_camera"
        self._cached_image: bytes | None = None
        self._in_provider_refresh = False

    async def async_refresh_providers(self, *, write_state: bool = True) -> None:
        # Brackets core's stream_source() probe so it gets the static relay
        # URL without dialing SIP (see module docstring). Residual race: a
        # real tap landing inside this window also gets the static URL and
        # skips the dial — sub-second, refresh only fires on entity-add /
        # provider registration, the user retries.
        self._in_provider_refresh = True
        try:
            await super().async_refresh_providers(write_state=write_state)
        finally:
            self._in_provider_refresh = False

    async def stream_source(self) -> str | None:
        if self._in_provider_refresh:
            return self.coordinator.relay_rtsp_url
        t0 = time.monotonic()
        _LOGGER.debug("STREAM_PULL: stream_source() called by a consumer")
        if self.coordinator.data is None:
            _LOGGER.debug("STREAM_PULL: coordinator has no data — refusing")
            return None
        # Lazy SIP: a consumer asking for the stream IS the "user picked
        # up" signal (HomeKit pull, WebRTC offer or HLS start all trace
        # back to a tile tap). Trigger the INVITE now (if not already),
        # then wait for the audio bridge to come up before handing out the
        # URL. Note: `camera_view: live` cards / stream preload would land
        # here on every ring — documented as unsupported in the README.
        await self.coordinator.async_ensure_call_started()
        url = await self.coordinator.async_wait_for_stream()
        elapsed_ms = (time.monotonic() - t0) * 1000
        if not url:
            _LOGGER.warning(
                "STREAM_PULL: stream URL not available after %.0fms — viewer will see an error or infinite loading",
                elapsed_ms,
            )
            return None
        _LOGGER.debug(
            "STREAM_PULL: handing URL %s to the consumer (waited %.0fms for bridge)",
            url,
            elapsed_ms,
        )
        # Bare URL on purpose — three consumers, three transports handled
        # upstream: HomeKit's ffmpeg retries TCP after go2rtc's 461 on UDP
        # SETUP, HA's go2rtc pulls over TCP, and the stream component sets
        # prefer_tcp for rtsp:// sources itself.
        return url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if self._cached_image is None:
            try:
                self._cached_image = await self.hass.async_add_executor_job(
                    _PLACEHOLDER.read_bytes
                )
            except OSError as err:
                _LOGGER.warning("Could not read doorbell placeholder: %s", err)
                return None
        return self._cached_image
