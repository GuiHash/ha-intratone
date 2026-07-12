"""Diagnostic button — go2rtc relay self-test.

Publishes a short synthetic stream to go2rtc (the exact ANNOUNCE+RECORD a
real call performs) then reads it back with DESCRIBE, so a broken relay is
caught on demand instead of at the next doorbell ring. The outcome raises
or clears the same repair issue as a live-call push failure.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntratoneConfigEntry, report_relay_status
from .const import CONF_VIDEO_ENABLED, DOMAIN
from .entity import IntratoneEntity, async_remove_stale_entity
from .go2rtc import async_selftest_go2rtc

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntratoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # The relay is only consumed on the video path — same gate as the camera.
    if not entry.options.get(CONF_VIDEO_ENABLED, False):
        async_remove_stale_entity(hass, "button", f"{entry.entry_id}_go2rtc_test")
        return
    async_add_entities([IntratoneGo2rtcTestButton(entry.runtime_data.coordinator)])


class IntratoneGo2rtcTestButton(IntratoneEntity, ButtonEntity):
    """Runs the go2rtc publish/read-back self-test on demand."""

    _attr_translation_key = "go2rtc_test"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_go2rtc_test"

    async def async_press(self) -> None:
        entry = self.coordinator.entry
        call_manager = entry.runtime_data.call_manager
        if call_manager.active_call_id is not None:
            # The self-test would fight the call's ffmpeg for the same
            # go2rtc slot — and a live call already exercises the real path.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="go2rtc_selftest_busy",
            )
        url = call_manager.relay_rtsp_url
        err = await async_selftest_go2rtc(url)
        report_relay_status(self.hass, entry, err is None)
        if err is not None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="go2rtc_selftest_failed",
                translation_placeholders={"url": url, "reason": err},
            )
        _LOGGER.info("go2rtc self-test passed for %s", url)
