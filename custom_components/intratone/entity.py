"""Base entity for the Intratone integration."""

from __future__ import annotations

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
