"""REST client tests — happy path + error path with aioresponses."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.intratone.const import (
    API_BASE,
)
from custom_components.intratone.rest_api import (
    IntratoneAPI,
    IntratoneApiError,
    IntratoneAuthError,
    authenticate_for_invite,
    register_with_invite,
)
from custom_components.intratone.store import IntratoneCredentialsStore


@pytest.fixture
def aiomock():
    with aioresponses() as m:
        yield m


async def _seeded_store(hass, jwt: str = "fake.jwt.token") -> IntratoneCredentialsStore:
    """Standalone Store fixture for unit tests that don't go through setup."""
    store = IntratoneCredentialsStore(hass, "3844428")
    await store.async_load()
    await store.async_update(jwt=jwt)
    return store


async def test_answer_call_success(hass, mock_entry, aiomock) -> None:
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}api/calls/123/answer",
        payload={"error": 0, "state": "ok"},
    )
    assert await api.answer_call("123") is True


async def test_answer_call_retries_after_jwt_refresh(hass, mock_entry, aiomock) -> None:
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}api/calls/123/answer",
        payload={"state": "error", "message": "expired"},
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "newjwt", "id": "3844428"}},
    )
    aiomock.post(
        f"{API_BASE}api/calls/123/answer",
        payload={"error": 0, "state": "ok"},
    )

    assert await api.answer_call("123") is True
    # Refreshed JWT now lives in the Store, not in entry.data.
    assert store.jwt == "newjwt"


async def test_register_with_invite_returns_data(hass, aiomock) -> None:
    aiomock.post(
        f"{API_BASE}api/auth/registercodes",
        payload={"state": "ok", "data": {"id": "999", "tel": "0612345678"}},
    )
    data = await register_with_invite(
        async_get_clientsession(hass),
        device_id="ha-test",
        fcm_token="tok",
        code="123456",
        codepass="7890",
    )
    assert data["id"] == "999"
    assert data["tel"] == "0612345678"


async def test_register_with_invite_rejected(hass, aiomock) -> None:
    aiomock.post(
        f"{API_BASE}api/auth/registercodes",
        payload={"state": "error", "message": "code expired"},
    )
    with pytest.raises(IntratoneAuthError):
        await register_with_invite(
            async_get_clientsession(hass),
            device_id="ha-test",
            fcm_token="tok",
            code="000000",
            codepass="0000",
        )


async def test_authenticate_for_invite_tries_normalized_phone(hass, aiomock) -> None:
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {}},
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "ok", "id": "9"}},
    )
    data = await authenticate_for_invite(
        async_get_clientsession(hass), tel="0612345678", device_id="ha-test"
    )
    assert data["jwt"] == "ok"
