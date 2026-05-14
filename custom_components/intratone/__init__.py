"""The Intratone Doorbell integration."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .coordinator import IntratoneCoordinator
from .fcm_listener import FcmListener
from .rest_api import IntratoneAPI

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CAMERA, Platform.EVENT, Platform.LOCK]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SIMULATE_RING = "simulate_ring"
SIMULATE_RING_SCHEMA = vol.Schema(
    {
        vol.Optional("door_name", default="PORTE TEST"): cv.string,
        vol.Optional("call_id"): cv.string,
        vol.Optional("entry_id"): cv.string,
    }
)


@dataclass
class IntratoneRuntime:
    """Runtime objects for a config entry."""

    api: IntratoneAPI
    coordinator: IntratoneCoordinator
    fcm: FcmListener


type IntratoneConfigEntry = ConfigEntry[IntratoneRuntime]


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Register dev-only services that target any loaded entry."""

    async def _simulate_ring(call: ServiceCall) -> None:
        entries = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state.recoverable and getattr(e, "runtime_data", None)
        ]
        if not entries:
            _LOGGER.warning("simulate_ring: no loaded Intratone entry")
            return

        target = call.data.get("entry_id")
        if target:
            entries = [e for e in entries if e.entry_id == target]
            if not entries:
                _LOGGER.warning("simulate_ring: entry_id %s not loaded", target)
                return

        payload = {
            "call_id": call.data.get("call_id") or f"sim-{int(time.time())}",
            "message": call.data["door_name"],
            "LOGIN_TO_CALL": "SIMULATED",
            "TYPE": "24",
        }
        for entry in entries:
            await entry.runtime_data.coordinator.async_handle_push(payload)

    hass.services.async_register(
        DOMAIN, SERVICE_SIMULATE_RING, _simulate_ring, schema=SIMULATE_RING_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IntratoneConfigEntry) -> bool:
    """Set up Intratone from a config entry."""
    session = async_get_clientsession(hass)
    api = IntratoneAPI(hass, session, entry)

    coordinator = IntratoneCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    fcm = FcmListener(hass, entry, coordinator)
    await fcm.async_start()

    entry.runtime_data = IntratoneRuntime(api=api, coordinator=coordinator, fcm=fcm)

    api.async_start_jwt_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IntratoneConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime: IntratoneRuntime = entry.runtime_data
        await runtime.fcm.async_stop()
        runtime.api.async_stop()
    return unload_ok
