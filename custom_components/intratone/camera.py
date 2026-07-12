"""Camera entity for the Intratone intercom.

Only created when the `video_enabled` option is on — without VP8 video the
stream would just show the synthetic placeholder frame (a black screen in
HomeKit). Returns the RTSP URL exposed by `AudioBridge` when a SIP call is
active. Outside of an active call, `stream_source` returns None so HomeKit
falls back to the still-image placeholder.
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

    async def stream_source(self) -> str | None:
        t0 = time.monotonic()
        _LOGGER.debug("HOMEKIT_PULL: stream_source() called by HomeKit")
        if self.coordinator.data is None:
            _LOGGER.debug("HOMEKIT_PULL: coordinator has no data — refusing")
            return None
        # Lazy SIP: HomeKit asking for the stream IS the "user picked up"
        # signal. Trigger the INVITE now (if not already), then wait for
        # the audio bridge to come up before handing HomeKit the URL.
        await self.coordinator.async_ensure_call_started()
        url = await self.coordinator.async_wait_for_stream()
        elapsed_ms = (time.monotonic() - t0) * 1000
        if not url:
            _LOGGER.warning(
                "HOMEKIT_PULL: stream URL not available after %.0fms — HomeKit will see infinite loading",
                elapsed_ms,
            )
            return None
        _LOGGER.debug(
            "HOMEKIT_PULL: handing URL %s to HomeKit (waited %.0fms for bridge)",
            url,
            elapsed_ms,
        )
        # HomeKit Bridge spawns ffmpeg with our URL as the input. go2rtc's
        # RTSP server rejects UDP transport on SETUP (461 Unsupported
        # transport), so we force TCP by returning a fully-formed `-i` arg
        # — homekit/type_cameras.py preserves it verbatim when it starts
        # with `-i `.
        return f"-rtsp_transport tcp -i {url}"

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
