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
