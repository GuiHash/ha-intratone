"""Diagnostics dump redacts sensitive fields and exposes coordinator state."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from custom_components.intratone.const import API_BASE
from custom_components.intratone.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.fixture
def aiomock():
    with aioresponses() as m:
        yield m


async def test_diagnostics_redacts_credentials_and_dumps_state(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    await mock_entry.runtime_data.coordinator.async_handle_push(
        {
            "call_id": "300705065",
            "message": "PORTE RUE",
            "LOGIN_TO_CALL": "SECRET_SIP_LOGIN",
            "LOGIN": "cogelecTest",
            "PASS": "CogeleC",
            "ip_adress": "178.32.84.135",
        }
    )
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, mock_entry)

    # Sensitive credentials are scrubbed.
    entry_data = diag["entry"]["data"]
    for key in ("jwt", "fcm_token", "fcm_creds", "device_id", "numeric_id", "tel"):
        assert entry_data[key] == "**REDACTED**", key

    # Coordinator state is exposed, but caller_login (PII-ish) is scrubbed.
    last_call = diag["coordinator"]["last_call"]
    assert last_call["call_id"] == "300705065"
    assert last_call["door_name"] == "PORTE RUE"
    assert last_call["caller_login"] == "**REDACTED**"
    assert last_call["ring_seq"] == 1
