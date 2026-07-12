"""Config flow tests — invite parsing, happy path, error paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.intratone.config_flow import _normalize_phone
from custom_components.intratone.const import (
    API_BASE,
    CONF_INDICATIF,
    CONF_INVITE_CODE,
    CONF_JWT,
    CONF_NUMERIC_ID,
    CONF_REGISTER_METHOD,
    CONF_TEL,
    DOMAIN,
    PATH_ACCESS_LIST,
    PATH_MOBIPASS_ACTIVATE,
    PATH_MOBIPASS_VERIFY,
    REGISTER_METHOD_SMS,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture
def aiomock():
    with aioresponses() as m:
        yield m


@pytest.fixture
def patched_fcm_register():
    with patch(
        "custom_components.intratone.config_flow.fcm_register_standalone",
        new=AsyncMock(return_value=("fake-fcm-token", {"gcm": {"android_id": 1}})),
    ) as p:
        yield p


async def _pick_invite_step(hass) -> dict:
    """Init the flow and navigate the menu to the invite-code form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "invite"}
    )


async def _pick_phone_step(hass) -> dict:
    """Init the flow and navigate the menu to the SMS phone form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "phone"}
    )


async def test_invalid_format_shows_error(hass) -> None:
    result = await _pick_invite_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INVITE_CODE: "not-a-code"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_format"}


async def test_happy_path_creates_entry(
    hass, aiomock, patched_fcm_register
) -> None:
    aiomock.post(
        f"{API_BASE}api/auth/registercodes",
        payload={"state": "ok", "data": {"id": "3844428", "tel": "0671124546"}},
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={
            "state": "ok",
            "data": {"jwt": "fresh.jwt", "id": "3844428"},
        },
    )

    result = await _pick_invite_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INVITE_CODE: "448789-1206"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TEL] == "0671124546"
    assert result["data"][CONF_NUMERIC_ID] == "3844428"
    # JWT lives in the per-account credentials Store, not in entry.data.
    assert CONF_JWT not in result["data"]
    patched_fcm_register.assert_awaited_once()
    # The Store was pre-written under the unique_id.
    from custom_components.intratone.store import IntratoneCredentialsStore

    store = IntratoneCredentialsStore(hass, "3844428")
    await store.async_load()
    assert store.jwt == "fresh.jwt"
    assert store.fcm_token == "fake-fcm-token"


async def test_rejected_invite_shows_error(
    hass, aiomock, patched_fcm_register
) -> None:
    aiomock.post(
        f"{API_BASE}api/auth/registercodes",
        payload={"state": "error", "message": "code expired"},
    )
    result = await _pick_invite_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INVITE_CODE: "448789-1206"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_code"}


async def test_duplicate_invite_aborts_without_store_write(
    hass, hass_storage, mock_entry: MockConfigEntry, aiomock, patched_fcm_register
) -> None:
    """Re-entering a valid invite code for a configured account aborts BEFORE
    the credentials Store is overwritten — otherwise the running FcmListener
    would be desynced from the newly stored fcm_token."""
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/registercodes",
        payload={"state": "ok", "data": {"id": "3844428", "tel": "0671124546"}},
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "new.jwt", "id": "3844428"}},
    )

    result = await _pick_invite_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INVITE_CODE: "448789-1206"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # The Store file was never written.
    assert "intratone.3844428.creds" not in hass_storage


def test_normalize_phone_handles_00_prefix() -> None:
    """`00` international prefix is stripped like `+` before the indicatif."""
    assert _normalize_phone("0033671124546", "33") == "671124546"
    assert _normalize_phone("00 33 6 71 12 45 46", "33") == "671124546"
    assert _normalize_phone("+33671124546", "33") == "671124546"
    assert _normalize_phone("0671124546", "33") == "671124546"
    assert _normalize_phone("0041791234567", "41") == "791234567"


async def test_phone_invalid_shows_invalid_phone_error(hass) -> None:
    """An unusable phone number gets its own error key, not the invite-code
    `invalid_format` wording."""
    result = await _pick_phone_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "0", "indicatif": "33"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"phone": "invalid_phone"}


async def test_sms_happy_path_creates_entry(
    hass, aiomock, patched_fcm_register
) -> None:
    aiomock.post(
        f"{API_BASE}api/auth/verify",
        payload={
            "state": "ok",
            "data": {
                "compatible": "1",
                "openingaccess": "1",
                "inuse": "0",
                "total": 1,
            },
        },
    )
    aiomock.post(
        f"{API_BASE}api/auth/register",
        payload={"state": "ok", "data": {"id": "3844428"}},
    )
    aiomock.post(f"{API_BASE}api/auth/validate", payload={"state": "ok"})
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "sms.jwt", "id": "3844428"}},
    )

    result = await _pick_phone_step(hass)
    assert result["step_id"] == "phone"
    # Phone normalization peels the French national 0 off.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "0671124546", "indicatif": "33"}
    )
    assert result["step_id"] == "sms"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "1234"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_NUMERIC_ID] == "3844428"
    # The phone stored is the normalized national number (no leading 0).
    assert result["data"][CONF_TEL] == "671124546"
    # The collected indicatif is persisted for downstream auth calls.
    assert result["data"][CONF_INDICATIF] == "33"
    assert CONF_JWT not in result["data"]

    from custom_components.intratone.store import IntratoneCredentialsStore

    store = IntratoneCredentialsStore(hass, "3844428")
    await store.async_load()
    assert store.jwt == "sms.jwt"
    assert store.fcm_token == "fake-fcm-token"


async def test_sms_invalid_code_shows_error(
    hass, aiomock, patched_fcm_register
) -> None:
    aiomock.post(
        f"{API_BASE}api/auth/verify",
        payload={
            "state": "ok",
            "data": {
                "compatible": "1",
                "openingaccess": "1",
                "inuse": "0",
                "total": 1,
            },
        },
    )
    aiomock.post(
        f"{API_BASE}api/auth/register",
        payload={"state": "ok", "data": {"id": "3844428"}},
    )
    aiomock.post(
        f"{API_BASE}api/auth/validate",
        payload={"state": "error", "message": "wrong code"},
    )

    result = await _pick_phone_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "0671124546", "indicatif": "33"}
    )
    assert result["step_id"] == "sms"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "0000"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_sms_code"}


async def test_reauth_silent_refresh_succeeds(
    hass, mock_entry: MockConfigEntry, aiomock, mock_fcm_client, mock_call_manager
) -> None:
    """Stored phone+device_id can still mint a JWT → no prompt to the user."""
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "renewed.jwt", "id": "3844428"}},
        repeat=True,
    )
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    result = await mock_entry.start_reauth_flow(hass)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    # JWT was refreshed into the Store, not into entry.data.
    assert CONF_JWT not in mock_entry.data
    assert mock_entry.runtime_data.store.jwt == "renewed.jwt"


async def _setup_loaded_entry(
    hass, mock_entry, aiomock, *, compatible="1", mobipass="0"
) -> None:
    """Load an entry so its runtime_data.api is available to the flow.

    The auth/device mobipass flags drive the reconfigure precheck:
    `compatible="1", mobipass="0"` (default) → transfer needed → the flow
    proceeds; other combinations make it abort (issue #61).
    """
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={
            "state": "ok",
            "data": {
                "jwt": "j",
                "id": "3844428",
                "mobipass_compatible": compatible,
                "mobipass": mobipass,
            },
        },
        repeat=True,
    )
    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        payload={"state": "ok", "data": {"list": []}},
        repeat=True,
    )
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()


async def test_mobipass_transfer_happy_path(
    hass, mock_entry: MockConfigEntry, aiomock, mock_fcm_client, mock_call_manager
) -> None:
    """Reconfigure → activate (SMS) → enter code → transfer succeeds and reloads.

    The invite-paired entry is eligible with the key held elsewhere
    (compatible=1, mobipass=0), so the precheck lets the transfer proceed.
    """
    await _setup_loaded_entry(hass, mock_entry, aiomock)
    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}", payload={"state": "ok", "error": 0}
    )
    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_VERIFY}", payload={"state": "ok", "error": 0}
    )

    result = await mock_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    # Confirm the warning → triggers the SMS → advances to the OTP form.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "mobipass_otp"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mobipass_transfer_successful"


async def test_mobipass_transfer_invalid_code_shows_error(
    hass, mock_entry: MockConfigEntry, aiomock, mock_fcm_client, mock_call_manager
) -> None:
    """A rejected OTP keeps the user on the code form with a mapped error."""
    await _setup_loaded_entry(hass, mock_entry, aiomock)
    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}", payload={"state": "ok", "error": 0}
    )
    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_VERIFY}",
        payload={
            "state": "ok",
            "error": 1,
            "code": "MOBIPASS_OTP_INVALID",
            "message": "bad",
        },
    )

    result = await mock_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "mobipass_otp"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "000000"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "mobipass_code_invalid"}


async def test_mobipass_reconfigure_aborts_when_key_already_here(
    hass, mock_entry: MockConfigEntry, aiomock, mock_fcm_client, mock_call_manager
) -> None:
    """Reconfigure short-circuits (no SMS) when the key is already on HA."""
    await _setup_loaded_entry(hass, mock_entry, aiomock, mobipass="1")

    result = await mock_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mobipass_already_active"


async def test_mobipass_reconfigure_not_eligible_for_invite_account(
    hass, mock_entry: MockConfigEntry, aiomock, mock_fcm_client, mock_call_manager
) -> None:
    """An invite-paired account the server reports as non-eligible aborts."""
    await _setup_loaded_entry(hass, mock_entry, aiomock, compatible="0")

    result = await mock_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mobipass_not_eligible"


async def test_mobipass_reconfigure_sms_paired_points_to_invite_code(
    hass, mock_entry_data, aiomock, mock_fcm_client, mock_call_manager
) -> None:
    """An SMS-paired device can't get the CléMobil → steer to invitation code."""
    sms_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="3844428",
        title="Intratone (SMS)",
        data={**mock_entry_data, CONF_REGISTER_METHOD: REGISTER_METHOD_SMS},
    )
    await _setup_loaded_entry(hass, sms_entry, aiomock, compatible="0")

    result = await sms_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mobipass_use_invite_code"


async def test_mobipass_reconfigure_legacy_entry_is_uncertain(
    hass, mock_entry_data, aiomock, mock_fcm_client, mock_call_manager
) -> None:
    """A legacy entry with no recorded pairing method gets the uncertain hint."""
    data = {k: v for k, v in mock_entry_data.items() if k != CONF_REGISTER_METHOD}
    legacy_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="3844428",
        title="Intratone (legacy)",
        data=data,
    )
    await _setup_loaded_entry(hass, legacy_entry, aiomock, compatible="0")

    result = await legacy_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "mobipass_unknown_method"


async def test_reauth_falls_back_to_form_when_silent_refresh_rejected(
    hass, mock_entry: MockConfigEntry, aiomock, mock_fcm_client, mock_call_manager
) -> None:
    """When the backend refuses the stored credentials, the user is asked
    for a fresh invitation code."""
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    # Silent reauth tries /api/auth/device — return an error so we fall
    # through to the invite-code form.
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "error", "message": "device unknown"},
        repeat=True,
    )

    result = await mock_entry.start_reauth_flow(hass)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"


async def test_reauth_with_other_accounts_invite_aborts_without_store_write(
    hass,
    hass_storage,
    mock_entry: MockConfigEntry,
    aiomock,
    patched_fcm_register,
) -> None:
    """A reauth invite code that resolves to ANOTHER account aborts with
    `unique_id_mismatch` BEFORE that account's credentials Store is clobbered."""
    mock_entry.add_to_hass(hass)

    # Silent reauth tries api/auth/device first — reject it so the flow
    # falls through to the invite-code form. authenticate_for_invite tries
    # up to 3 tel candidates, so register one rejection per candidate.
    for _ in range(3):
        aiomock.post(
            f"{API_BASE}api/auth/device",
            payload={"state": "error", "message": "device unknown"},
        )
    # The invite code belongs to a different account (id 9999999).
    aiomock.post(
        f"{API_BASE}api/auth/registercodes",
        payload={"state": "ok", "data": {"id": "9999999", "tel": "0699999999"}},
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "other.jwt", "id": "9999999"}},
    )

    result = await mock_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INVITE_CODE: "448789-1206"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    # The other account's Store file was never written.
    assert "intratone.9999999.creds" not in hass_storage
