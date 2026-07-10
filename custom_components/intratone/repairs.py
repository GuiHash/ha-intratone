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

from .config_flow import _MOBIPASS_ERRORS, MOBIPASS_OTP_SCHEMA
from .const import DOMAIN
from .rest_api import IntratoneApiError, IntratoneAuthError, IntratoneMobipassError

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


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the fix flow for an Intratone repair issue."""
    entry_id = (data or {}).get("entry_id")
    return MobipassTransferRepairFlow(issue_id, entry_id)
