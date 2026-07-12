"""Base entity for the Intratone integration."""

from __future__ import annotations

import asyncio

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import IntratoneCoordinator


class IntratoneEntity(CoordinatorEntity[IntratoneCoordinator]):
    """Base class binding all Intratone entities to one device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: IntratoneCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=entry.title,
        )


class MomentaryRevertMixin:
    """Shared behaviour for write-only "momentary" entities.

    These entities briefly show their actuated state, then revert to the
    resting state after a short delay so HomeKit's UI animates back.
    Subclasses provide `_revert_delay_s` (read at revert time) and
    `_revert_state()` (reset the `_attr_*` to the resting value).
    """

    _revert_task: asyncio.Task | None = None

    @property
    def _revert_delay_s(self) -> float:
        """Seconds the actuated state stays visible."""
        raise NotImplementedError

    def _revert_state(self) -> None:
        """Reset entity attributes to the resting state."""
        raise NotImplementedError

    def _schedule_revert(self) -> None:
        """(Re)start the revert timer after a successful actuation."""
        if self._revert_task is not None and not self._revert_task.done():
            self._revert_task.cancel()
        self._revert_task = self.hass.async_create_task(self._async_revert_later())

    async def _async_revert_later(self) -> None:
        try:
            await asyncio.sleep(self._revert_delay_s)
        except asyncio.CancelledError:
            return
        self._revert_state()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._revert_task is not None and not self._revert_task.done():
            self._revert_task.cancel()
