"""Repair-flow tests — the CléMobil / Mobipass transfer fix flow (issue #61)."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intratone.const import (
    API_BASE,
    DOMAIN,
    PATH_ACCESS_LIST,
    PATH_MOBIPASS_ACTIVATE,
    PATH_MOBIPASS_VERIFY,
)


@pytest.fixture
def aiomock():
    # Let the repairs HTTP test client (loopback) through; only mock the
    # Intratone API on sip.intratone.info.
    with aioresponses(passthrough=["http://127.0.0.1", "http://localhost"]) as m:
        yield m


async def _setup_entry_needing_transfer(hass, mock_entry, aiomock) -> str:
    """Load an entry whose flags say the CléMobil is held elsewhere → issue.

    Returns the expected issue_id.
    """
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={
            "state": "ok",
            "data": {
                "jwt": "j",
                "id": "3844428",
                "mobipass_compatible": "1",
                "mobipass": "0",
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
    return f"mobipass_transfer_{mock_entry.entry_id}"


async def test_mobipass_repair_fix_flow_happy_path(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """Fix the repair: confirm → SMS → code → issue clears."""
    assert await async_setup_component(hass, "repairs", {})

    # First auth/device (setup detection) says the key is elsewhere → issue.
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={
            "state": "ok",
            "data": {
                "jwt": "j",
                "id": "3844428",
                "mobipass_compatible": "1",
                "mobipass": "0",
            },
        },
    )
    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        payload={"state": "ok", "data": {"list": []}},
        repeat=True,
    )
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    issue_id = f"mobipass_transfer_{mock_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}", payload={"state": "ok", "error": 0}
    )
    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_VERIFY}", payload={"state": "ok", "error": 0}
    )
    # After a successful transfer the flow reloads the entry; the post-reload
    # detection sees the key is now held here (mobipass=1) → issue stays cleared.
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={
            "state": "ok",
            "data": {
                "jwt": "j",
                "id": "3844428",
                "mobipass_compatible": "1",
                "mobipass": "1",
            },
        },
        repeat=True,
    )

    client = await hass_client()

    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    assert resp.status == 200
    data = await resp.json()
    flow_id = data["flow_id"]
    assert data["step_id"] == "confirm"

    # Confirm the warning → triggers the SMS → OTP form.
    resp = await client.post(f"/api/repairs/issues/fix/{flow_id}", json={})
    data = await resp.json()
    assert data["step_id"] == "otp"

    # Enter the code → transfer completes → flow done + issue cleared.
    resp = await client.post(
        f"/api/repairs/issues/fix/{flow_id}", json={"code": "123456"}
    )
    data = await resp.json()
    assert data["type"] == "create_entry"

    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_mobipass_repair_fix_flow_invalid_code(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A rejected code keeps the user on the OTP form and leaves the issue up."""
    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_needing_transfer(hass, mock_entry, aiomock)

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

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]
    resp = await client.post(f"/api/repairs/issues/fix/{flow_id}", json={})
    assert (await resp.json())["step_id"] == "otp"

    resp = await client.post(
        f"/api/repairs/issues/fix/{flow_id}", json={"code": "000000"}
    )
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {"base": "mobipass_code_invalid"}
    # Issue is still present until the transfer actually succeeds.
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def _setup_entry_with_stale_token(hass, mock_entry, mock_fcm_client, aiomock):
    """Load an entry whose live token differs from the stored one → repair.

    Returns the expected issue_id.
    """
    from unittest.mock import AsyncMock

    # Store keeps "fake-fcm-token"; the client reports a rotated token.
    mock_fcm_client.instance.checkin_or_register = AsyncMock(
        return_value="rotated-token"
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "j", "id": "3844428"}},
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
    return f"fcm_token_stale_{mock_entry.entry_id}"


async def test_fcm_token_stale_fix_flow_happy_path(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """Re-pair via the push-token repair: invite code re-registers the token."""
    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_with_stale_token(
        hass, mock_entry, mock_fcm_client, aiomock
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    aiomock.post(
        f"{API_BASE}api/auth/registercodes",
        payload={"state": "ok", "data": {"id": "3844428", "tel": "0671124546"}},
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    assert resp.status == 200
    data = await resp.json()
    flow_id = data["flow_id"]
    assert data["step_id"] == "confirm"

    resp = await client.post(
        f"/api/repairs/issues/fix/{flow_id}", json={"invite_code": "448789-1206"}
    )
    data = await resp.json()
    assert data["type"] == "create_entry"

    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    # The rotated token is now recorded as the one registered with Intratone.
    assert mock_entry.runtime_data.store.fcm_token == "rotated-token"


async def test_fcm_token_stale_fix_flow_invalid_code(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A rejected invite code keeps the form up and leaves the issue in place."""
    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_with_stale_token(
        hass, mock_entry, mock_fcm_client, aiomock
    )

    aiomock.post(
        f"{API_BASE}api/auth/registercodes",
        payload={"state": "error", "message": "bad code"},
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]

    resp = await client.post(
        f"/api/repairs/issues/fix/{flow_id}", json={"invite_code": "448789-1206"}
    )
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {"base": "invalid_code"}
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_fcm_token_stale_fix_flow_fcm_registration_failure(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """Google refusing the new push token shows `fcm_failed`, not `unknown`."""
    from unittest.mock import AsyncMock, patch

    from custom_components.intratone.fcm_listener import FcmRegistrationError

    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_with_stale_token(
        hass, mock_entry, mock_fcm_client, aiomock
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]

    with patch(
        "custom_components.intratone.repairs.fcm_register_standalone",
        new=AsyncMock(side_effect=FcmRegistrationError("PHONE_REGISTRATION_ERROR")),
    ):
        resp = await client.post(
            f"/api/repairs/issues/fix/{flow_id}", json={"invite_code": "448789-1206"}
        )
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {"base": "fcm_failed"}
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_mobipass_repair_confirm_not_loaded_aborts(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A fix flow for an issue whose entry_id is missing (or gone) aborts
    instead of crashing — e.g. a stale issue surviving entry removal.

    A sibling entry is loaded so the `intratone` repairs platform is
    registered at all (HA only discovers it for a set-up domain); the
    orphan issue itself is intentionally unlinked from any entry.
    """
    assert await async_setup_component(hass, "repairs", {})
    await _setup_entry_needing_transfer(hass, mock_entry, aiomock)

    issue_id = "mobipass_transfer_orphan"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="mobipass_transfer",
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    data = await resp.json()
    assert data["type"] == "abort"
    assert data["reason"] == "not_loaded"


async def test_mobipass_repair_confirm_activate_mobipass_error_shows_mapped_message(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A Mobipass-specific refusal on activate maps to its own error key."""
    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_needing_transfer(hass, mock_entry, aiomock)

    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}",
        payload={
            "state": "ok",
            "error": 1,
            "code": "MOBIPASS_NOT_AVAILABLE",
            "message": "not eligible",
        },
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]

    resp = await client.post(f"/api/repairs/issues/fix/{flow_id}", json={})
    data = await resp.json()
    assert data["type"] == "form"
    assert data["step_id"] == "confirm"
    assert data["errors"] == {"base": "mobipass_not_available"}
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_mobipass_repair_confirm_activate_generic_api_error_shows_mobipass_failed(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A non-Mobipass API failure on activate (e.g. a server hiccup) still
    keeps the user on the confirm form, mapped to the generic key."""
    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_needing_transfer(hass, mock_entry, aiomock)

    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}",
        payload={"state": "error", "message": "server hiccup"},
        repeat=True,
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]

    resp = await client.post(f"/api/repairs/issues/fix/{flow_id}", json={})
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {"base": "mobipass_failed"}


async def test_mobipass_repair_confirm_activate_unexpected_error_shows_unknown(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A bug/crash during activate is caught and mapped to `unknown`."""
    from unittest.mock import AsyncMock

    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_needing_transfer(hass, mock_entry, aiomock)
    mock_entry.runtime_data.api.mobipass_activate = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]

    resp = await client.post(f"/api/repairs/issues/fix/{flow_id}", json={})
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {"base": "unknown"}


async def test_mobipass_repair_otp_not_loaded_aborts_if_entry_unloads(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """The entry can be unloaded while the OTP form is still open (e.g. the
    user removes the integration mid-transfer) — the next submit aborts
    instead of crashing on a missing API client."""
    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_needing_transfer(hass, mock_entry, aiomock)
    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}", payload={"state": "ok", "error": 0}
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]
    resp = await client.post(f"/api/repairs/issues/fix/{flow_id}", json={})
    assert (await resp.json())["step_id"] == "otp"

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()

    resp = await client.post(
        f"/api/repairs/issues/fix/{flow_id}", json={"code": "123456"}
    )
    data = await resp.json()
    assert data["type"] == "abort"
    assert data["reason"] == "not_loaded"


async def test_mobipass_repair_otp_verify_generic_api_error_shows_mobipass_failed(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A non-Mobipass API failure on verify keeps the user on the OTP form,
    mapped to the generic key (not the OTP-specific one)."""
    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_needing_transfer(hass, mock_entry, aiomock)
    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}", payload={"state": "ok", "error": 0}
    )
    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_VERIFY}",
        payload={"state": "error", "message": "server hiccup"},
        repeat=True,
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]
    resp = await client.post(f"/api/repairs/issues/fix/{flow_id}", json={})
    assert (await resp.json())["step_id"] == "otp"

    resp = await client.post(
        f"/api/repairs/issues/fix/{flow_id}", json={"code": "123456"}
    )
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {"base": "mobipass_failed"}


async def test_mobipass_repair_otp_verify_unexpected_error_shows_unknown(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A bug/crash during verify is caught and mapped to `unknown`."""
    from unittest.mock import AsyncMock

    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_needing_transfer(hass, mock_entry, aiomock)
    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}", payload={"state": "ok", "error": 0}
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]
    resp = await client.post(f"/api/repairs/issues/fix/{flow_id}", json={})
    assert (await resp.json())["step_id"] == "otp"

    mock_entry.runtime_data.api.mobipass_verify = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    resp = await client.post(
        f"/api/repairs/issues/fix/{flow_id}", json={"code": "123456"}
    )
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {"base": "unknown"}


async def test_fcm_repair_confirm_not_loaded_aborts(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A fix flow for an issue whose entry_id is missing (or gone) aborts
    instead of crashing.

    A sibling entry is loaded so the `intratone` repairs platform is
    registered at all (HA only discovers it for a set-up domain); the
    orphan issue itself is intentionally unlinked from any entry.
    """
    assert await async_setup_component(hass, "repairs", {})
    await _setup_entry_with_stale_token(hass, mock_entry, mock_fcm_client, aiomock)

    issue_id = "fcm_token_stale_orphan"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="fcm_token_stale",
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    data = await resp.json()
    assert data["type"] == "abort"
    assert data["reason"] == "not_loaded"


async def test_fcm_repair_confirm_invalid_invite_format_shows_error(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A malformed invite code keeps the user on the form with its own key."""
    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_with_stale_token(
        hass, mock_entry, mock_fcm_client, aiomock
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]

    resp = await client.post(
        f"/api/repairs/issues/fix/{flow_id}", json={"invite_code": "not-a-code"}
    )
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {"base": "invalid_format"}
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_fcm_repair_confirm_api_error_shows_auth_failed(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A non-JSON registercodes response is a generic API error, distinct
    from a rejected code (`invalid_code`)."""
    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_with_stale_token(
        hass, mock_entry, mock_fcm_client, aiomock
    )

    aiomock.post(f"{API_BASE}api/auth/registercodes", body="not json")

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]

    resp = await client.post(
        f"/api/repairs/issues/fix/{flow_id}", json={"invite_code": "448789-1206"}
    )
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {"base": "auth_failed"}


async def test_fcm_repair_confirm_unexpected_error_shows_unknown(
    hass,
    hass_client,
    mock_entry: MockConfigEntry,
    mock_fcm_client,
    mock_call_manager,
    aiomock,
) -> None:
    """A bug/crash during the re-pair is caught and mapped to `unknown`."""
    from unittest.mock import AsyncMock, patch

    assert await async_setup_component(hass, "repairs", {})
    issue_id = await _setup_entry_with_stale_token(
        hass, mock_entry, mock_fcm_client, aiomock
    )

    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": issue_id},
    )
    flow_id = (await resp.json())["flow_id"]

    with patch(
        "custom_components.intratone.repairs.fcm_register_standalone",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        resp = await client.post(
            f"/api/repairs/issues/fix/{flow_id}", json={"invite_code": "448789-1206"}
        )
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {"base": "unknown"}
