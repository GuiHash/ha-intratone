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
from .entity import IntratoneEntity, MomentaryRevertMixin
from .rest_api import (
    IntratoneAccess,
    IntratoneApiError,
    IntratoneAuthError,
    IntratoneConnectionError,
)

_LOGGER = logging.getLogger(__name__)

UNLOCK_VISIBLE_S = 3.0

# Backoff between retries when the initial remote-open accesses fetch fails
# transiently (network blip, server outage) — bounded and not aggressive.
# Read at call time (not captured as a default arg) so tests can shrink it.
ACCESS_LOCKS_RETRY_DELAYS_S: tuple[float, ...] = (30.0, 60.0, 120.0, 300.0, 600.0)


def _is_transient_access_error(err: Exception) -> bool:
    """True for a network blip or a 5xx — worth a retry.

    An `IntratoneAuthError` (credentials revoked) or a 4xx `IntratoneApiError`
    is a definitive answer, not a hiccup — never retry those.
    """
    if isinstance(err, IntratoneConnectionError):
        return True
    return isinstance(err, IntratoneApiError) and (
        err.status is None or err.status >= 500
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntratoneConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    # The door lock is available immediately. Remote-open accesses ("Clé
    # mobile" / mobipass) need a REST round-trip to list — fetch them in the
    # background so a slow/failing API never delays the doorbell entities.
    async_add_entities([IntratoneDoorLock(coordinator)])

    async def _add_access_locks() -> None:
        last_err: Exception | None = None
        delays = (0.0, *ACCESS_LOCKS_RETRY_DELAYS_S)
        for attempt, delay in enumerate(delays):
            if delay:
                _LOGGER.debug(
                    "Remote-open accesses fetch failed transiently, retrying "
                    "in %ss (attempt %d/%d): %s",
                    delay,
                    attempt + 1,
                    len(delays),
                    last_err,
                )
                await asyncio.sleep(delay)
            try:
                accesses = await entry.runtime_data.api.list_access()
            except Exception as err:  # noqa: BLE001
                if not _is_transient_access_error(err):
                    _LOGGER.warning("Could not fetch remote-open accesses: %s", err)
                    return
                last_err = err
                continue
            break
        else:
            _LOGGER.warning("Could not fetch remote-open accesses: %s", last_err)
            return
        if accesses:
            _LOGGER.debug(
                "Creating %d remote-open access lock(s): %s",
                len(accesses),
                ", ".join(f"{a.residence}/{a.name} ({a.openmode})" for a in accesses),
            )
            async_add_entities(
                IntratoneAccessLock(coordinator, access) for access in accesses
            )
        else:
            _LOGGER.debug(
                "No remote-open access locks created "
                "(no data/ble accesses, or account not provisioned)"
            )

    entry.async_create_background_task(
        hass, _add_access_locks(), "intratone_access_locks"
    )


class IntratoneDoorLock(MomentaryRevertMixin, IntratoneEntity, LockEntity):
    """Door-relay trigger exposed as a HomeKit Lock."""

    _attr_translation_key = "door"
    _attr_assumed_state = True

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_door_lock"
        self._attr_is_locked = True

    async def async_unlock(self, **_kwargs) -> None:
        ok = await self.coordinator.async_open_door()
        if not ok:
            raise HomeAssistantError(
                "Open door failed — no active call or API error (see logs)"
            )

        self._attr_is_locked = False
        self.async_write_ha_state()
        self._schedule_revert()

    async def async_lock(self, **_kwargs) -> None:
        # No-op: the relay self-closes; we just flip the visible state.
        self._attr_is_locked = True
        self.async_write_ha_state()

    @property
    def _revert_delay_s(self) -> float:
        return UNLOCK_VISIBLE_S

    def _revert_state(self) -> None:
        self._attr_is_locked = True


class IntratoneAccessLock(MomentaryRevertMixin, IntratoneEntity, LockEntity):
    """Remote-open access ("Clé mobile" / mobipass) exposed as a Lock.

    Unlike `IntratoneDoorLock`, this opens a door/gate on demand via the REST
    API — no incoming call required. Same momentary, assumed-state behaviour so
    HomeKit / voice assistants get an "Unlock" action.
    """

    _attr_assumed_state = True

    def __init__(self, coordinator, access: IntratoneAccess) -> None:
        super().__init__(coordinator)
        self._access = access
        self._attr_unique_id = f"{coordinator.entry.entry_id}_access_{access.access_id}"
        label = " — ".join(p for p in (access.residence, access.name) if p)
        self._attr_name = label or "Accès"
        self._attr_is_locked = True

    async def async_unlock(self, **_kwargs) -> None:
        try:
            ok = await self.coordinator.api.open_access(self._access)
        except (IntratoneApiError, IntratoneAuthError) as err:
            raise HomeAssistantError(f"Open access failed: {err}") from err
        if not ok:
            raise HomeAssistantError("Open access rejected by Intratone")

        self._attr_is_locked = False
        self.async_write_ha_state()
        self._schedule_revert()

    async def async_lock(self, **_kwargs) -> None:
        # No-op: the relay self-closes; we just flip the visible state.
        self._attr_is_locked = True
        self.async_write_ha_state()

    @property
    def _revert_delay_s(self) -> float:
        return UNLOCK_VISIBLE_S

    def _revert_state(self) -> None:
        self._attr_is_locked = True
