"""Async REST client for the Intratone API.

Reverse-engineered from `com.cogelec.notificationpush` v4.6.3.
All endpoints use form-urlencoded POST and Bearer JWT auth.
See INTRATONE_API.md for the full reference.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    API_BASE,
    APP_ID,
    APP_TOKEN,
    APP_VERSION,
    CONF_DEVICE_ID,
    CONF_NUMERIC_ID,
    CONF_TEL,
    DEVICE_BUNDLE_ID,
    JWT_REFRESH_INTERVAL_HOURS,
)
from .store import IntratoneCredentialsStore

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class IntratoneAuthError(Exception):
    """Raised when authentication or registration fails."""


class IntratoneApiError(Exception):
    """Raised on unexpected API errors."""


class IntratoneAPI:
    """Async client for the Intratone REST API."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        entry: ConfigEntry,
        store: IntratoneCredentialsStore,
    ) -> None:
        self._hass = hass
        self._session = session
        self._entry = entry
        self._store = store
        self._refresh_unsub = None

    @property
    def jwt(self) -> str | None:
        return self._store.jwt

    @property
    def numeric_id(self) -> str | None:
        return self._entry.data.get(CONF_NUMERIC_ID)

    @property
    def tel(self) -> str | None:
        return self._entry.data.get(CONF_TEL)

    @property
    def device_id(self) -> str | None:
        return self._entry.data.get(CONF_DEVICE_ID)

    async def _post_form(
        self,
        path: str,
        form: dict[str, str],
        *,
        jwt: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"

        async with self._session.post(
            API_BASE + path, data=form, headers=headers, timeout=REQUEST_TIMEOUT
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except aiohttp.ContentTypeError as err:
                raise IntratoneApiError(f"Non-JSON response from {path}: {err}") from err

            if not isinstance(body, dict):
                raise IntratoneApiError(f"Unexpected response shape from {path}: {body!r}")

            if body.get("state") == "error":
                msg = body.get("message") or body.get("code") or "unknown"
                raise IntratoneApiError(f"API error from {path}: {msg}")

            return body

    async def authenticate_device(self) -> dict[str, Any]:
        """POST /api/auth/device — also serves as JWT refresh.

        Tries the stored phone number with several normalizations.
        Returns the `data` block (containing `jwt`, `id`, `tel`, etc.).
        """
        if not self.tel or not self.device_id:
            raise IntratoneAuthError("Cannot authenticate without tel and device_id")

        candidates = [self.tel]
        stripped = self.tel.lstrip("0")
        if stripped and stripped != self.tel:
            candidates.append(f"33{stripped}")
            candidates.append(stripped)

        last_error: Exception | None = None
        for tel in candidates:
            try:
                body = await self._post_form(
                    "api/auth/device",
                    {
                        "app_id": APP_ID,
                        "app_token": APP_TOKEN,
                        "tel": tel,
                        "device_id": self.device_id,
                        "appversion": APP_VERSION,
                    },
                )
            except IntratoneApiError as err:
                last_error = err
                continue

            data = body.get("data") or {}
            if data.get("jwt"):
                return data

        raise IntratoneAuthError(
            f"Authentication failed for tel={self.tel}: {last_error}"
        )

    async def answer_call(self, call_id: str) -> bool:
        """POST /api/calls/{call_id}/answer — opens the door."""
        if not self.jwt:
            raise IntratoneAuthError("No JWT available")
        if not self.numeric_id:
            raise IntratoneAuthError("No numeric_id available")

        try:
            body = await self._post_form(
                f"api/calls/{call_id}/answer",
                {"smartphone_id": self.numeric_id},
                jwt=self.jwt,
            )
        except IntratoneApiError:
            # Try a JWT refresh once on failure (likely 401).
            await self.refresh_jwt()
            body = await self._post_form(
                f"api/calls/{call_id}/answer",
                {"smartphone_id": self.numeric_id},
                jwt=self.jwt,
            )

        return body.get("error") == 0

    async def refresh_jwt(self) -> None:
        """Refresh the JWT and persist it via the credentials Store."""
        data = await self.authenticate_device()
        new_jwt = data.get("jwt")
        if not new_jwt:
            raise IntratoneAuthError("Refresh succeeded but no JWT in response")

        await self._store.async_update(jwt=new_jwt)
        if data.get("id"):
            new_id = str(data["id"])
            if new_id != self._entry.data.get(CONF_NUMERIC_ID):
                self._hass.config_entries.async_update_entry(
                    self._entry,
                    data={**self._entry.data, CONF_NUMERIC_ID: new_id},
                )
        _LOGGER.debug("Intratone JWT refreshed")

    def async_start_jwt_refresh(self) -> None:
        """Schedule periodic JWT refresh."""

        async def _tick(_now) -> None:
            try:
                await self.refresh_jwt()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("JWT refresh failed: %s", err)

        self._refresh_unsub = async_track_time_interval(
            self._hass, _tick, timedelta(hours=JWT_REFRESH_INTERVAL_HOURS)
        )

    def async_stop(self) -> None:
        """Cancel scheduled refresh."""
        if self._refresh_unsub is not None:
            self._refresh_unsub()
            self._refresh_unsub = None


async def register_with_invite(
    session: aiohttp.ClientSession,
    *,
    device_id: str,
    fcm_token: str,
    code: str,
    codepass: str,
) -> dict[str, Any]:
    """POST /api/auth/registercodes — invite-code device registration.

    Returns the `data` block: {id, tel, ...}. Used during config_flow.
    """
    form = {
        "app_id": APP_ID,
        "app_token": APP_TOKEN,
        "code": code,
        "codepass": codepass,
        "os": "android",
        "osv": "29",
        "model": "HA-Bridge",
        "manufacturer": "HomeAssistant",
        "device_id": device_id,
        "description": "Home Assistant",
        "appversion": APP_VERSION,
        "id_fcm": fcm_token,
        "id_wonderpush": "",
        "pushkit_id": "",
        "bundleid": DEVICE_BUNDLE_ID,
        "device_country": "FRA",
        "device_language": "fr",
        "carrier_name": "Orange",
        "carrier_countrycode": "FR",
        "carrier_countryiso": "fr",
        "carrier_networkcode": "20801",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    async with session.post(
        API_BASE + "api/auth/registercodes",
        data=form,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    ) as resp:
        try:
            body = await resp.json(content_type=None)
        except aiohttp.ContentTypeError as err:
            raise IntratoneApiError(f"Non-JSON response: {err}") from err

    if not isinstance(body, dict) or body.get("state") == "error":
        msg = body.get("message") or body.get("code") if isinstance(body, dict) else body
        raise IntratoneAuthError(f"registercodes rejected: {msg}")

    data = body.get("data") or {}
    if not data.get("id") or not data.get("tel"):
        raise IntratoneAuthError(f"registercodes incomplete response: {body}")
    return data


async def authenticate_for_invite(
    session: aiohttp.ClientSession,
    *,
    tel: str,
    device_id: str,
) -> dict[str, Any]:
    """One-shot auth used by config_flow (no entry yet)."""
    candidates = [tel]
    stripped = tel.lstrip("0")
    if stripped and stripped != tel:
        candidates.append(f"33{stripped}")
        candidates.append(stripped)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    last: dict[str, Any] = {}
    for candidate in candidates:
        async with session.post(
            API_BASE + "api/auth/device",
            data={
                "app_id": APP_ID,
                "app_token": APP_TOKEN,
                "tel": candidate,
                "device_id": device_id,
                "appversion": APP_VERSION,
            },
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            try:
                last = await resp.json(content_type=None)
            except aiohttp.ContentTypeError:
                continue

        data = (last.get("data") or {}) if isinstance(last, dict) else {}
        if data.get("jwt"):
            return data
        await asyncio.sleep(0)

    raise IntratoneAuthError(f"auth/device returned no JWT: {last}")
