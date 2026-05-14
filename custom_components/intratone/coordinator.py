"""Push-driven coordinator for Intratone doorbell calls."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

if TYPE_CHECKING:
    from .rest_api import IntratoneAPI

_LOGGER = logging.getLogger(__name__)


@dataclass
class CallState:
    """A single doorbell call event.

    `ring_seq` is a monotonic counter; entities use it to detect new rings
    rather than a transient flag (which would race with state writes).
    """

    call_id: str
    door_name: str
    caller_login: str
    received_at: datetime
    ring_seq: int


class IntratoneCoordinator(DataUpdateCoordinator[CallState | None]):
    """Coordinator that holds the latest call and notifies entities on push."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: IntratoneAPI,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=None,  # push-driven only
        )
        self.entry = entry
        self.api = api
        self._ring_seq = 0

    async def _async_update_data(self) -> CallState | None:
        # No periodic refresh; coordinator data is set by async_handle_push.
        return self.data

    async def async_handle_push(self, payload: dict) -> None:
        """Convert an FCM payload into a CallState and notify listeners."""
        call_id = payload.get("call_id")
        if not call_id:
            _LOGGER.debug("Push without call_id, ignoring: %s", _redact(payload))
            return

        self._ring_seq += 1
        state = CallState(
            call_id=str(call_id),
            door_name=payload.get("message") or "Doorbell",
            caller_login=payload.get("LOGIN_TO_CALL", ""),
            received_at=datetime.now(UTC),
            ring_seq=self._ring_seq,
        )
        _LOGGER.info(
            "Doorbell ring: door=%s call_id=%s seq=%d",
            state.door_name,
            state.call_id,
            state.ring_seq,
        )
        self.async_set_updated_data(state)

    async def async_open_door(self) -> bool:
        """Answer the current call to trigger the door relay."""
        if self.data is None:
            _LOGGER.warning("Open door requested but no active call known")
            return False
        try:
            ok = await self.api.answer_call(self.data.call_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Open door failed: %s", err)
            return False
        if ok:
            _LOGGER.info("Door opened for call %s", self.data.call_id)
        else:
            _LOGGER.warning("Open door returned non-ok for call %s", self.data.call_id)
        return ok


_REDACT_KEYS = {"LOGIN", "PASS", "ip_adress"}


def _redact(payload: dict) -> dict:
    """Mask sensitive fields when logging FCM payloads."""
    return {k: ("***" if k in _REDACT_KEYS else v) for k, v in payload.items()}
