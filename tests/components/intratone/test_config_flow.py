"""Config flow tests — invite parsing, happy path, error paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import aioresponses
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.intratone.const import (
    API_BASE,
    CONF_INVITE_CODE,
    CONF_JWT,
    CONF_NUMERIC_ID,
    CONF_TEL,
    DOMAIN,
)


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


async def test_invalid_format_shows_error(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
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

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INVITE_CODE: "448789-1206"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TEL] == "0671124546"
    assert result["data"][CONF_NUMERIC_ID] == "3844428"
    assert result["data"][CONF_JWT] == "fresh.jwt"
    patched_fcm_register.assert_awaited_once()


async def test_rejected_invite_shows_error(
    hass, aiomock, patched_fcm_register
) -> None:
    aiomock.post(
        f"{API_BASE}api/auth/registercodes",
        payload={"state": "error", "message": "code expired"},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INVITE_CODE: "448789-1206"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_code"}
