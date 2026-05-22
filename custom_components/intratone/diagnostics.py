"""Diagnostics for the Intratone integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import IntratoneConfigEntry
from .const import (
    CONF_DEVICE_ID,
    CONF_FCM_CREDS,
    CONF_FCM_TOKEN,
    CONF_INVITE_CODE,
    CONF_JWT,
    CONF_NUMERIC_ID,
    CONF_TEL,
)

REDACT_ENTRY = {
    CONF_DEVICE_ID,
    CONF_FCM_CREDS,
    CONF_FCM_TOKEN,
    CONF_INVITE_CODE,
    CONF_JWT,
    CONF_NUMERIC_ID,
    CONF_TEL,
}

REDACT_STORE = {"jwt", "fcm_token", "fcm_creds"}

REDACT_STATE = {"caller_login"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IntratoneConfigEntry
) -> dict[str, Any]:
    runtime = getattr(entry, "runtime_data", None)
    coordinator = runtime.coordinator if runtime else None
    store = runtime.store if runtime else None

    state: dict[str, Any] | None = None
    if coordinator is not None and coordinator.data is not None:
        data = coordinator.data
        state = async_redact_data(
            {
                "call_id": data.call_id,
                "door_name": data.door_name,
                "caller_login": data.caller_login,
                "received_at": data.received_at.isoformat(),
                "ring_seq": data.ring_seq,
                "has_stream_url": data.stream_url is not None,
                "door_code": data.door_code,
            },
            REDACT_STATE,
        )

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), REDACT_ENTRY),
            "options": dict(entry.options),
        },
        "store": async_redact_data(store.snapshot(), REDACT_STORE)
        if store is not None
        else None,
        "coordinator": {"last_call": state} if coordinator is not None else None,
    }
