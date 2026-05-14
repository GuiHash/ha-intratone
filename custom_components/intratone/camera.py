"""Camera entity for the Intratone intercom.

Phase 2: returns the audio-only RTSP URL exposed by `AudioBridge` when a
SIP call is active, so HomeKit Bridge (with `support_audio: true`) plays
visitor audio. Outside of an active call, `stream_source` returns None so
HomeKit falls back to the still-image placeholder.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntratoneConfigEntry
from .entity import IntratoneEntity

_LOGGER = logging.getLogger(__name__)

_PLACEHOLDER = Path(__file__).parent / "assets" / "doorbell_idle.jpg"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntratoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
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
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.stream_url

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
