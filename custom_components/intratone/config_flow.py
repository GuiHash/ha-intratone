"""Config flow for the Intratone integration."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_DEVICE_ID,
    CONF_FCM_CREDS,
    CONF_FCM_TOKEN,
    CONF_INVITE_CODE,
    CONF_JWT,
    CONF_NUMERIC_ID,
    CONF_TEL,
    DOMAIN,
)
from .fcm_listener import fcm_register_standalone
from .rest_api import (
    IntratoneApiError,
    IntratoneAuthError,
    authenticate_for_invite,
    register_with_invite,
)

_LOGGER = logging.getLogger(__name__)

INVITE_RE = re.compile(r"^\s*(\d{4,8})\s*[-\s]\s*(\d{3,6})\s*$")

USER_SCHEMA = vol.Schema({vol.Required(CONF_INVITE_CODE): str})


class IntratoneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Intratone pairing flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial pairing — ask for an invitation code."""
        return await self._async_invite_step(user_input, step_id="user")

    async def async_step_reauth(
        self, _entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Re-pair when credentials become irrecoverable.

        Tries `api/auth/device` silently with the stored phone + device_id
        first — the Cogelec app does the same to renew its JWT without
        bothering the user. Falls back to the invite-code form only if the
        silent refresh is rejected (phone unbound, device wiped server-side,
        etc.).
        """
        self._reauth_entry = self._get_reauth_entry()
        new_data = await self._async_try_silent_reauth()
        if new_data is not None:
            return self.async_update_reload_and_abort(
                self._reauth_entry, data=new_data
            )
        return await self.async_step_reauth_confirm()

    async def _async_try_silent_reauth(self) -> dict[str, Any] | None:
        """Best-effort JWT refresh using already-persisted credentials.

        Returns the updated entry data on success, None on any failure
        (caller falls back to the invite-code form).
        """
        entry = self._reauth_entry
        if entry is None:
            return None
        tel = entry.data.get(CONF_TEL)
        device_id = entry.data.get(CONF_DEVICE_ID)
        if not tel or not device_id:
            return None
        try:
            session = async_get_clientsession(self.hass)
            data = await authenticate_for_invite(
                session, tel=tel, device_id=device_id
            )
        except (IntratoneAuthError, IntratoneApiError) as err:
            _LOGGER.info(
                "Silent reauth rejected (%s) — prompting for invite code", err
            )
            return None
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Silent reauth crashed — prompting for invite code"
            )
            return None

        new_data = {**entry.data, CONF_JWT: data["jwt"]}
        if data.get("id"):
            new_data[CONF_NUMERIC_ID] = str(data["id"])
        return new_data

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_invite_step(user_input, step_id="reauth_confirm")

    async def _async_invite_step(
        self,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            match = INVITE_RE.match(user_input[CONF_INVITE_CODE])
            if not match:
                errors["base"] = "invalid_format"
            else:
                code, codepass = match.group(1), match.group(2)
                try:
                    entry_data = await self._pair(code, codepass)
                except IntratoneAuthError as err:
                    _LOGGER.warning("Pairing failed: %s", err)
                    errors["base"] = "invalid_code"
                except IntratoneApiError as err:
                    _LOGGER.warning("API error during pairing: %s", err)
                    errors["base"] = "auth_failed"
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception("Unexpected pairing error")
                    if "fcm" in str(err).lower() or "firebase" in str(err).lower():
                        errors["base"] = "fcm_failed"
                    else:
                        errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(entry_data[CONF_NUMERIC_ID])

                    if self._reauth_entry is not None:
                        self._abort_if_unique_id_mismatch()
                        return self.async_update_reload_and_abort(
                            self._reauth_entry, data=entry_data
                        )

                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Intratone ({entry_data[CONF_TEL]})",
                        data=entry_data,
                    )

        return self.async_show_form(
            step_id=step_id,
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    async def _pair(self, code: str, codepass: str) -> dict[str, Any]:
        """Run FCM register + Intratone registercodes + auth/device."""
        session = async_get_clientsession(self.hass)

        device_id = (
            self._reauth_entry.data.get(CONF_DEVICE_ID)
            if self._reauth_entry is not None
            else None
        ) or f"ha-intratone-{uuid.uuid4().hex[:12]}"

        existing_creds = (
            self._reauth_entry.data.get(CONF_FCM_CREDS)
            if self._reauth_entry is not None
            else None
        )

        fcm_token, fcm_creds = await fcm_register_standalone(existing_creds)

        register_data = await register_with_invite(
            session,
            device_id=device_id,
            fcm_token=fcm_token,
            code=code,
            codepass=codepass,
        )
        tel = str(register_data["tel"])
        numeric_id = str(register_data["id"])

        auth_data = await authenticate_for_invite(
            session, tel=tel, device_id=device_id
        )

        return {
            CONF_DEVICE_ID: device_id,
            CONF_NUMERIC_ID: numeric_id,
            CONF_TEL: tel,
            CONF_JWT: auth_data["jwt"],
            CONF_FCM_TOKEN: fcm_token,
            CONF_FCM_CREDS: fcm_creds,
        }
