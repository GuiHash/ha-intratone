"""Background FCM listener for Intratone doorbell pushes.

Wraps `firebase-messaging`'s `FcmPushClient`. Credentials live in the
per-account `IntratoneCredentialsStore` (not on `entry.data`); the upstream
client rotates them via its `credentials_updated_callback`, which we route
through the Store.

Note: `firebase-messaging` is a community-maintained reverse-engineering of
Google's MCS protocol. Pin the version and watch for upstream changes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    DEVICE_BUNDLE_ID,
    DOMAIN,
    FCM_API_KEY,
    FCM_APP_ID,
    FCM_PROJECT_ID,
    FCM_SENDER_ID,
    FCM_TOKEN_ISSUE_PREFIX,
)
from .store import IntratoneCredentialsStore

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
        store: IntratoneCredentialsStore,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._store = store
        self._task: asyncio.Task | None = None
        self._client = None
        self._stopping = False
        self._connected = False
        self._state_listeners: list[Callable[[bool], None]] = []

    @property
    def connected(self) -> bool:
        return self._connected

    def add_state_listener(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Subscribe to FCM connection state transitions.

        The listener is called with `True` once the upstream client is up,
        and `False` when it dies or is stopped. Returns an unsubscribe.
        """
        self._state_listeners.append(listener)
        return lambda: self._state_listeners.remove(listener)

    def _set_connected(self, connected: bool) -> None:
        if self._connected == connected:
            return
        self._connected = connected
        for listener in list(self._state_listeners):
            try:
                listener(connected)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("FCM state listener raised")

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
        self._set_connected(False)

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

        creds = self._store.fcm_creds

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
        token = await self._client.checkin_or_register()
        await self._client.start()
        self._set_connected(True)
        self._check_token_registration(token)

        # Poll the client's run_state. The library's _listen task swallows
        # connection errors and after 3 sequential errors calls _terminate()
        # which sets state to STOPPING (NOT STOPPED — STOPPED is reserved for
        # an explicit stop() call). Without this check the supervisor would
        # sleep forever and pushes would be silently dropped.
        dead_states = (
            FcmPushClientRunState.STOPPING,
            FcmPushClientRunState.STOPPED,
        )
        try:
            while not self._stopping:
                await asyncio.sleep(HEALTHCHECK_INTERVAL_S)
                state = getattr(self._client, "run_state", None)
                if state in dead_states:
                    raise RuntimeError(
                        f"FcmPushClient stopped itself (state={state})"
                    )
        finally:
            self._set_connected(False)

    @callback
    def _check_token_registration(self, token: str | None) -> None:
        """Raise a repair when the live FCM token no longer matches Intratone's.

        Intratone only records our push token (`id_fcm`) during onboarding /
        re-pair. `firebase-messaging` keeps the token stable while its
        credentials stay valid, but if Google ever rotates it, Intratone keeps
        pushing to the dead token: doorbell notifications stop silently while
        the MCS socket (and the connectivity sensor) stays up.

        `store.fcm_token` is the last token we registered — a mismatch means a
        re-pair is required. We deliberately do NOT overwrite it here, so the
        mismatch keeps signalling until the re-pair actually re-registers the
        new token (`repairs.FcmTokenStaleRepairFlow`).
        """
        issue_id = f"{FCM_TOKEN_ISSUE_PREFIX}{self._entry.entry_id}"
        registered = self._store.fcm_token
        if registered and token and token != registered:
            _LOGGER.warning(
                "FCM token rotated since pairing — Intratone still targets the "
                "old token; push notifications need a re-pair to be restored"
            )
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="fcm_token_stale",
                data={"entry_id": self._entry.entry_id},
            )
        else:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)

    @callback
    def _persist_creds(self, new_creds: dict) -> None:
        self._hass.async_create_task(self._store.async_update(fcm_creds=new_creds))

    @callback
    def _dispatch_push(self, data: dict) -> None:
        if not data:
            _LOGGER.debug("FCM push with empty data, ignoring")
            return
        self._hass.async_create_task(self._coordinator.async_handle_push(data))
