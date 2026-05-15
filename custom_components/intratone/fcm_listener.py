"""Background FCM listener for Intratone doorbell pushes.

Wraps `firebase-messaging`'s `FcmPushClient`. Credentials are stored on the
config entry under `CONF_FCM_CREDS` and refreshed in-place when the upstream
client rotates them.

Note: `firebase-messaging` is a community-maintained reverse-engineering of
Google's MCS protocol. Pin the version and watch for upstream changes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_FCM_CREDS,
    DEVICE_BUNDLE_ID,
    FCM_API_KEY,
    FCM_APP_ID,
    FCM_PROJECT_ID,
    FCM_SENDER_ID,
)

_LOGGER = logging.getLogger(__name__)

BACKOFF_INITIAL_S = 5
BACKOFF_MAX_S = 300
# How often the supervisor polls the underlying FcmPushClient run state.
# The library shuts itself down after 3 sequential connection errors and
# does NOT raise — we have to inspect run_state to notice and reconnect.
HEALTHCHECK_INTERVAL_S = 30


def _fcm_config():
    """Build the FcmRegisterConfig (lazy import to keep top-level light)."""
    from firebase_messaging import FcmRegisterConfig

    return FcmRegisterConfig(
        project_id=FCM_PROJECT_ID,
        app_id=FCM_APP_ID,
        api_key=FCM_API_KEY,
        messaging_sender_id=FCM_SENDER_ID,
        bundle_id=DEVICE_BUNDLE_ID,
    )


async def fcm_register_standalone(
    existing_creds: dict | None = None,
) -> tuple[str, dict | None]:
    """Register with FCM and return (token, creds).

    Used by config_flow before any entry/runtime exists.
    """
    from firebase_messaging import FcmPushClient

    holder: dict[str, Any] = {"creds": existing_creds}

    def _on_creds_updated(new_creds: dict) -> None:
        holder["creds"] = new_creds

    client = FcmPushClient(
        callback=lambda *_: None,
        fcm_config=_fcm_config(),
        credentials=existing_creds,
        credentials_updated_callback=_on_creds_updated,
    )
    token = await client.checkin_or_register()
    return token, holder["creds"]


class FcmListener:
    """Supervised FCM push listener feeding the coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._task: asyncio.Task | None = None
        self._client = None
        self._stopping = False

    async def async_start(self) -> None:
        self._stopping = False
        self._task = self._hass.async_create_background_task(
            self._supervisor(), name="intratone_fcm_supervisor"
        )

    async def async_stop(self) -> None:
        self._stopping = True
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Error stopping FCM client: %s", err)
            self._client = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _supervisor(self) -> None:
        """Restart the FCM client with exponential backoff on failure."""
        backoff = BACKOFF_INITIAL_S
        while not self._stopping:
            try:
                await self._run_once()
                return  # graceful stop
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "FCM listener crashed: %s — reconnecting in %ds", err, backoff
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _run_once(self) -> None:
        from firebase_messaging import FcmPushClient
        from firebase_messaging.fcmpushclient import FcmPushClientRunState

        creds = self._entry.data.get(CONF_FCM_CREDS)

        def _on_push(notification: dict, persistent_id: str, _ctx) -> None:
            data = (notification or {}).get("data") or {}
            self._hass.loop.call_soon_threadsafe(self._dispatch_push, data)

        def _on_creds_updated(new_creds: dict) -> None:
            self._hass.loop.call_soon_threadsafe(self._persist_creds, new_creds)

        self._client = FcmPushClient(
            callback=_on_push,
            fcm_config=_fcm_config(),
            credentials=creds,
            credentials_updated_callback=_on_creds_updated,
        )
        await self._client.checkin_or_register()
        await self._client.start()

        # Poll the client's run_state. The library's _listen task swallows
        # connection errors and after 3 sequential errors calls _terminate()
        # which sets state to STOPPING (NOT STOPPED — STOPPED is reserved for
        # an explicit stop() call). Without this check the supervisor would
        # sleep forever and pushes would be silently dropped.
        dead_states = (
            FcmPushClientRunState.STOPPING,
            FcmPushClientRunState.STOPPED,
        )
        while not self._stopping:
            await asyncio.sleep(HEALTHCHECK_INTERVAL_S)
            state = getattr(self._client, "run_state", None)
            if state in dead_states:
                raise RuntimeError(
                    f"FcmPushClient stopped itself (state={state})"
                )

    @callback
    def _persist_creds(self, new_creds: dict) -> None:
        new_data = {**self._entry.data, CONF_FCM_CREDS: new_creds}
        self._hass.config_entries.async_update_entry(self._entry, data=new_data)

    @callback
    def _dispatch_push(self, data: dict) -> None:
        if not data:
            _LOGGER.debug("FCM push with empty data, ignoring")
            return
        self._hass.async_create_task(self._coordinator.async_handle_push(data))
