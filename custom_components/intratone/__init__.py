"""The Intratone Doorbell integration."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TypeAlias

import voluptuous as vol
from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .call_manager import CallManager
from .const import (
    CONF_FCM_CREDS,
    CONF_FCM_TOKEN,
    CONF_GO2RTC_URL,
    CONF_JWT,
    CONF_VIDEO_ENABLED,
    DEFAULT_GO2RTC_URL,
    DOMAIN,
    FCM_TOKEN_ISSUE_PREFIX,
)
from .coordinator import IntratoneCoordinator
from .fcm_listener import FcmListener
from .rest_api import IntratoneAPI
from .store import IntratoneCredentialsStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.LOCK,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Repair issue surfaced when the CléMobil/Mobipass key must be transferred to
# this device (issue #61). One per config entry.
MOBIPASS_ISSUE_PREFIX = "mobipass_transfer_"

SERVICE_SIMULATE_RING = "simulate_ring"
SIMULATE_RING_SCHEMA = vol.Schema(
    {
        vol.Optional("door_name", default="PORTE TEST"): cv.string,
        vol.Optional("call_id"): cv.string,
        vol.Optional("entry_id"): cv.string,
        # Optional SIP creds — when provided, the full INVITE flow fires (lets us
        # test against `dev/mock_asterisk.py` or any other SIP server without
        # waiting for a real intercom ring).
        vol.Optional("sip_server_ip"): cv.string,
        vol.Optional("sip_target_user", default="MOCK"): cv.string,
        vol.Optional("sip_user", default="cogelecTest"): cv.string,
        vol.Optional("sip_pass", default="CogeleC"): cv.string,
    }
)


@dataclass
class IntratoneRuntime:
    """Runtime objects for a config entry."""

    api: IntratoneAPI
    coordinator: IntratoneCoordinator
    fcm: FcmListener
    call_manager: CallManager
    store: IntratoneCredentialsStore


IntratoneConfigEntry: TypeAlias = ConfigEntry[IntratoneRuntime]


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
            "LOGIN_TO_CALL": call.data.get("sip_target_user", "SIMULATED"),
            "TYPE": "24",
        }
        sip_server = call.data.get("sip_server_ip")
        if sip_server:
            # Full SIP flow against an arbitrary server (typically the mock).
            payload["ip_adress"] = sip_server
            payload["LOGIN"] = call.data.get("sip_user", "cogelecTest")
            payload["PASS"] = call.data.get("sip_pass", "CogeleC")
        for entry in entries:
            await entry.runtime_data.coordinator.async_handle_push(payload)

    hass.services.async_register(
        DOMAIN, SERVICE_SIMULATE_RING, _simulate_ring, schema=SIMULATE_RING_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: IntratoneConfigEntry) -> bool:
    """Set up Intratone from a config entry."""
    session = async_get_clientsession(hass)

    store = IntratoneCredentialsStore(hass, entry.unique_id or entry.entry_id)
    await store.async_load()
    await _async_migrate_legacy_creds(hass, entry, store)

    api = IntratoneAPI(hass, session, entry, store)

    coordinator = IntratoneCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    # CallManager owns the SIP UDP endpoint + audio bridge. We need a real LAN
    # IP to advertise in SIP From/Contact and the SDP `c=` line — falling back
    # to 127.0.0.1 here would make Asterisk send RTP into its own loopback and
    # the call would set up cleanly with zero audio, no error logged.
    local_ip = await async_get_source_ip(hass)
    if not local_ip or local_ip.startswith("127."):
        raise ConfigEntryNotReady(
            f"Could not determine a routable LAN IP for SIP advertisements "
            f"(async_get_source_ip returned {local_ip!r})."
        )
    call_manager = CallManager(
        local_host=local_ip,
        on_call_active=lambda call_id, url: coordinator.set_stream_url(call_id, url),
        on_call_ended=lambda call_id: coordinator.set_stream_url(call_id, None),
        video_enabled=entry.options.get(CONF_VIDEO_ENABLED, False),
        go2rtc_url=entry.options.get(CONF_GO2RTC_URL, DEFAULT_GO2RTC_URL),
    )
    await call_manager.async_start()
    coordinator.attach_call_manager(call_manager)

    fcm = FcmListener(hass, entry, coordinator, store)
    await fcm.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    entry.runtime_data = IntratoneRuntime(
        api=api,
        coordinator=coordinator,
        fcm=fcm,
        call_manager=call_manager,
        store=store,
    )

    api.async_start_jwt_refresh(
        on_refresh=lambda: _evaluate_mobipass_issue(hass, entry)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Detect whether the CléMobil/Mobipass key must be transferred to this
    # device (issue #61). Best-effort and in the background so a slow/failing
    # auth never delays the doorbell entities; re-checked on every JWT refresh.
    entry.async_create_background_task(
        hass, _async_initial_mobipass_check(hass, entry), "intratone_mobipass_check"
    )
    return True


def _mobipass_issue_id(entry: IntratoneConfigEntry) -> str:
    return f"{MOBIPASS_ISSUE_PREFIX}{entry.entry_id}"


@callback
def _evaluate_mobipass_issue(
    hass: HomeAssistant, entry: IntratoneConfigEntry
) -> None:
    """Create or clear the CléMobil-transfer repair from the latest flags.

    Reads `api.mobipass_state` (populated by the last `authenticate_device`);
    does no network I/O of its own. `needs_transfer` is true when the account
    supports Mobipass but the key is held by another device (the user's phone),
    so `list_access()` returns nothing until it is transferred here.
    """
    api: IntratoneAPI = entry.runtime_data.api
    state = api.mobipass_state
    issue_id = _mobipass_issue_id(entry)
    # Logged so a persistent/absent repair report (issue #61) can be correlated
    # with the flags that drove the decision.
    _LOGGER.debug(
        "Mobipass repair check: state=%s → %s",
        state,
        "raising issue" if (state is not None and state.needs_transfer) else "clearing issue",
    )
    if state is not None and state.needs_transfer:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="mobipass_transfer",
            data={"entry_id": entry.entry_id},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


async def _async_initial_mobipass_check(
    hass: HomeAssistant, entry: IntratoneConfigEntry
) -> None:
    """Fetch fresh Mobipass flags once at setup, then (re)evaluate the repair."""
    api: IntratoneAPI = entry.runtime_data.api
    try:
        # authenticate_device() populates api.mobipass_state (flags live only on
        # the auth/device response). It doesn't persist the JWT, so this is a
        # cheap read of the current ownership state.
        await api.authenticate_device()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Mobipass check skipped (auth/device failed)", exc_info=True)
        return
    _evaluate_mobipass_issue(hass, entry)


async def _async_migrate_legacy_creds(
    hass: HomeAssistant,
    entry: IntratoneConfigEntry,
    store: IntratoneCredentialsStore,
) -> None:
    """Move rotated credentials out of `entry.data` into the dedicated Store.

    Older versions of the integration kept JWT, FCM token and FCM credentials
    on `entry.data`, which churned `core.config_entries.json` on every JWT
    refresh (12 h) and on every Google MCS credential rotation. This shifts
    them once into the per-entry Store; subsequent setups are a no-op.
    """
    legacy = {}
    if CONF_JWT in entry.data:
        legacy["jwt"] = entry.data[CONF_JWT]
    if CONF_FCM_TOKEN in entry.data:
        legacy["fcm_token"] = entry.data[CONF_FCM_TOKEN]
    if CONF_FCM_CREDS in entry.data:
        legacy["fcm_creds"] = entry.data[CONF_FCM_CREDS]
    if not legacy:
        return

    await store.async_update(**legacy)
    clean = {
        k: v
        for k, v in entry.data.items()
        if k not in (CONF_JWT, CONF_FCM_TOKEN, CONF_FCM_CREDS)
    }
    hass.config_entries.async_update_entry(entry, data=clean)
    _LOGGER.info(
        "Intratone: migrated %d credential field(s) from entry.data to Store",
        len(legacy),
    )


async def _async_options_updated(
    hass: HomeAssistant, entry: IntratoneConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: IntratoneConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime: IntratoneRuntime = entry.runtime_data
        await runtime.fcm.async_stop()
        await runtime.call_manager.async_stop()
        runtime.api.async_stop()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: IntratoneConfigEntry) -> None:
    """Clean up per-entry repairs when the entry is removed."""
    ir.async_delete_issue(hass, DOMAIN, _mobipass_issue_id(entry))
    ir.async_delete_issue(hass, DOMAIN, f"{FCM_TOKEN_ISSUE_PREFIX}{entry.entry_id}")
