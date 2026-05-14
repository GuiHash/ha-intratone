"""Static-image camera for the Intratone intercom (Phase 1).

Phase 2 will add `stream_source` for one-way audio. Phase 3 will enable
two-way audio via `support_audio: true` in the HomeKit Bridge config.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.camera import Camera
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
    """Image-only camera placeholder.

    No `CameraEntityFeature.STREAM` — exposing a `stream_source` that
    points at a non-existent RTSP would break the HomeKit Bridge tile.
    """

    _attr_translation_key = "intercom"

    def __init__(self, coordinator) -> None:
        IntratoneEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_camera"
        self._cached_image: bytes | None = None

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
