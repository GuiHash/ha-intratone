"""Doorbell EventEntity — the primary HomeKit trigger."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntratoneConfigEntry
from .entity import IntratoneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntratoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([IntratoneDoorbellEvent(entry.runtime_data.coordinator)])


class IntratoneDoorbellEvent(IntratoneEntity, EventEntity):
    """Fires `pressed` whenever the coordinator sees a new ring."""

    _attr_translation_key = "doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_doorbell"
        self._last_seq = 0

    @callback
    def _handle_coordinator_update(self) -> None:
        state = self.coordinator.data
        if state is None:
            return
        if state.ring_seq != self._last_seq:
            self._last_seq = state.ring_seq
            self._trigger_event(
                "ring",
                {
                    "door_name": state.door_name,
                    "caller": state.caller_login,
                    "call_id": state.call_id,
                },
            )
            self.async_write_ha_state()
