"""Diagnostic binary_sensor reflecting the FCM listener's connection state.

Surfaces "is the push channel up?" in the HA UI so users don't have to
inspect logs to know whether they'd receive a doorbell push right now.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntratoneConfigEntry
from .entity import IntratoneEntity
from .fcm_listener import FcmListener


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntratoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    async_add_entities(
        [IntratoneFcmConnectedSensor(runtime.coordinator, runtime.fcm)]
    )


class IntratoneFcmConnectedSensor(IntratoneEntity, BinarySensorEntity):
    """`on` while the FCM push channel is up. Driven by FcmListener state."""

    _attr_translation_key = "fcm_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, coordinator, fcm: FcmListener) -> None:
        super().__init__(coordinator)
        self._fcm = fcm
        self._attr_unique_id = f"{coordinator.entry.entry_id}_fcm_connected"
        self._attr_is_on = fcm.connected
        self._unsub: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._attr_is_on = self._fcm.connected
        self._unsub = self._fcm.add_state_listener(self._on_state)

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_state(self, connected: bool) -> None:
        self._attr_is_on = connected
        self.async_write_ha_state()
