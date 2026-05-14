"""Momentary Lock — exposed primarily so HomeKit shows an Unlock action.

The Intratone door has no real "locked" state we can read; this entity is a
write-only trigger. We report `locked` at rest, briefly flip to `unlocked` on
`async_unlock`, and revert after a short delay so HomeKit's UI animates back.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntratoneConfigEntry
from .entity import IntratoneEntity

_LOGGER = logging.getLogger(__name__)

UNLOCK_VISIBLE_S = 3.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntratoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([IntratoneDoorLock(entry.runtime_data.coordinator)])


class IntratoneDoorLock(IntratoneEntity, LockEntity):
    """Door-relay trigger exposed as a HomeKit Lock."""

    _attr_translation_key = "door"
    _attr_assumed_state = True

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_door_lock"
        self._attr_is_locked = True
        self._revert_task: asyncio.Task | None = None

    async def async_unlock(self, **_kwargs) -> None:
        ok = await self.coordinator.async_open_door()
        if not ok:
            raise HomeAssistantError(
                "Open door failed — no active call or API error (see logs)"
            )

        self._attr_is_locked = False
        self.async_write_ha_state()

        if self._revert_task is not None and not self._revert_task.done():
            self._revert_task.cancel()
        self._revert_task = self.hass.async_create_task(self._revert_to_locked())

    async def async_lock(self, **_kwargs) -> None:
        # No-op: the relay self-closes; we just flip the visible state.
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def _revert_to_locked(self) -> None:
        try:
            await asyncio.sleep(UNLOCK_VISIBLE_S)
        except asyncio.CancelledError:
            return
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._revert_task is not None and not self._revert_task.done():
            self._revert_task.cancel()
