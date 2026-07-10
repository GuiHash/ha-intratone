"""Repair flows for the Intratone integration.

Currently one repair: transferring the CléMobil / Mobipass remote-open key to
this Home Assistant device (issue #61). The flow mirrors the config-flow
`reconfigure` steps — it reuses the same `IntratoneAPI` methods — but is
surfaced automatically when `__init__._evaluate_mobipass_issue` detects the key
is held by another device.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .config_flow import (
    _MOBIPASS_ERRORS,
    INVITE_RE,
    MOBIPASS_OTP_SCHEMA,
    USER_SCHEMA,
)
from .const import CONF_DEVICE_ID, CONF_INVITE_CODE, DOMAIN, FCM_TOKEN_ISSUE_PREFIX
from .fcm_listener import fcm_register_standalone
from .rest_api import (
    IntratoneApiError,
    IntratoneAuthError,
    IntratoneMobipassError,
    authenticate_for_invite,
    register_with_invite,
)
from .store import IntratoneCredentialsStore

_LOGGER = logging.getLogger(__name__)


class MobipassTransferRepairFlow(RepairsFlow):
    """Guided CléMobil transfer: warn → SMS code → verify → clear the issue."""

    def __init__(self, issue_id: str, entry_id: str | None) -> None:
        self._issue_id = issue_id
        self._entry_id = entry_id

    def _api(self):
        """The loaded entry's API client, or None if the entry isn't loaded."""
        if not self._entry_id:
            return None
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        return getattr(entry, "runtime_data", None) and entry.runtime_data.api

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Warn about the single-owner handover, then trigger the SMS code."""
        api = self._api()
        if api is None:
            return self.async_abort(reason="not_loaded")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await api.mobipass_activate()
            except IntratoneMobipassError as err:
                _LOGGER.warning("mobipass activate refused: %s", err)
                errors["base"] = _MOBIPASS_ERRORS.get(err.code, "mobipass_failed")
            except (IntratoneAuthError, IntratoneApiError) as err:
                _LOGGER.warning("mobipass activate failed: %s", err)
                errors["base"] = "mobipass_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected mobipass activate error")
                errors["base"] = "unknown"
            else:
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="confirm", data_schema=vol.Schema({}), errors=errors
        )

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Enter the SMS code to complete the transfer, then clear the issue."""
        api = self._api()
        if api is None:
            return self.async_abort(reason="not_loaded")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await api.mobipass_verify(user_input["code"].strip())
            except IntratoneMobipassError as err:
                _LOGGER.warning("mobipass verify refused: %s", err)
                errors["base"] = _MOBIPASS_ERRORS.get(err.code, "mobipass_failed")
            except (IntratoneAuthError, IntratoneApiError) as err:
                _LOGGER.warning("mobipass verify failed: %s", err)
                errors["base"] = "mobipass_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected mobipass verify error")
                errors["base"] = "unknown"
            else:
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
                # Reload so lock.py re-runs list_access() and the transferred
                # accesses appear as lock entities.
                if self._entry_id:
                    self.hass.config_entries.async_schedule_reload(self._entry_id)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="otp", data_schema=MOBIPASS_OTP_SCHEMA, errors=errors
        )


class FcmTokenStaleRepairFlow(RepairsFlow):
    """Re-register a rotated FCM push token with Intratone via a re-pair.

    Raised by `FcmListener._check_token_registration` when Google has rotated
    our push token: Intratone still targets the old one, so pushes stop
    silently. There is no silent re-register endpoint (`id_fcm` is only carried
    by the registercodes call), so we re-run the invite pairing — mirroring
    `config_flow._pair` — to re-send the current token under the same
    `device_id`.
    """

    def __init__(self, issue_id: str, entry_id: str | None) -> None:
        self._issue_id = issue_id
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for a fresh invite code, then re-register the current token."""
        entry = (
            self.hass.config_entries.async_get_entry(self._entry_id)
            if self._entry_id
            else None
        )
        if entry is None:
            return self.async_abort(reason="not_loaded")

        errors: dict[str, str] = {}
        if user_input is not None:
            match = INVITE_RE.match(user_input[CONF_INVITE_CODE])
            if not match:
                errors["base"] = "invalid_format"
            else:
                code, codepass = match.group(1), match.group(2)
                try:
                    await self._reregister(entry, code, codepass)
                except IntratoneAuthError as err:
                    _LOGGER.warning("FCM re-pair rejected: %s", err)
                    errors["base"] = "invalid_code"
                except IntratoneApiError as err:
                    _LOGGER.warning("FCM re-pair API error: %s", err)
                    errors["base"] = "auth_failed"
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected FCM re-pair error")
                    errors["base"] = "unknown"
                else:
                    ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
                    # Reload so the listener reconnects and confirms the
                    # re-registered token matches (clearing any residual state).
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                    return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="confirm", data_schema=USER_SCHEMA, errors=errors
        )

    async def _reregister(self, entry, code: str, codepass: str) -> None:
        """Mirror config_flow._pair: fresh token → registercodes → auth."""
        session = async_get_clientsession(self.hass)
        device_id = entry.data[CONF_DEVICE_ID]
        store = IntratoneCredentialsStore(
            self.hass, entry.unique_id or entry.entry_id
        )
        await store.async_load()
        fcm_token, fcm_creds = await fcm_register_standalone(store.fcm_creds)
        register_data = await register_with_invite(
            session,
            device_id=device_id,
            fcm_token=fcm_token,
            code=code,
            codepass=codepass,
        )
        tel = str(register_data["tel"])
        auth_data = await authenticate_for_invite(
            session, tel=tel, device_id=device_id
        )
        await store.async_update(
            jwt=auth_data["jwt"], fcm_token=fcm_token, fcm_creds=fcm_creds
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the fix flow for an Intratone repair issue."""
    entry_id = (data or {}).get("entry_id")
    if issue_id.startswith(FCM_TOKEN_ISSUE_PREFIX):
        return FcmTokenStaleRepairFlow(issue_id, entry_id)
    return MobipassTransferRepairFlow(issue_id, entry_id)
