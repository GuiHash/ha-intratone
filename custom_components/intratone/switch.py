"""Backlight switch — momentary trigger for the doorbell's illuminator.

The Intratone server documents this in `strings.xml` as:
"In poor lighting situations, you can enable the backlight mode to better see
your visitor. This is reset after each call."

Implementation mirrors the lock pattern: write-only momentary entity that
flips visible state for a short window so HomeKit's tile animates back to off.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntratoneConfigEntry
from .entity import IntratoneEntity

_LOGGER = logging.getLogger(__name__)

_VISIBLE_ON_S = 3.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntratoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([IntratoneBacklightSwitch(entry.runtime_data.coordinator)])


class IntratoneBacklightSwitch(IntratoneEntity, SwitchEntity):
    """One-shot toggle that asks the intercom hardware for extra
    illumination during the current call. The state is local-only — there
    is no way to read the actual hardware state back."""

    _attr_translation_key = "backlight"
    _attr_assumed_state = True

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backlight"
        self._attr_is_on = False
        self._revert_task: asyncio.Task | None = None

    async def async_turn_on(self, **_kwargs) -> None:
        sent = await self.coordinator.async_toggle_backlight()
        if not sent:
            _LOGGER.warning(
                "Backlight requested but no active call — server only acts on "
                "the signal during a confirmed SIP dialog"
            )
            return

        self._attr_is_on = True
        self.async_write_ha_state()

        if self._revert_task is not None and not self._revert_task.done():
            self._revert_task.cancel()
        self._revert_task = self.hass.async_create_task(self._revert_to_off())

    async def async_turn_off(self, **_kwargs) -> None:
        # No-op: backlight state is server-managed and reset on BYE. Just
        # flip the UI back so the user gets feedback if they tap off.
        self._attr_is_on = False
        self.async_write_ha_state()

    async def _revert_to_off(self) -> None:
        try:
            await asyncio.sleep(_VISIBLE_ON_S)
        except asyncio.CancelledError:
            return
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._revert_task is not None and not self._revert_task.done():
            self._revert_task.cancel()
