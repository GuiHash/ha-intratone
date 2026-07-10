"""REST client tests — happy path + error path with aioresponses."""

from __future__ import annotations

import logging

import pytest
from aioresponses import aioresponses
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.intratone.const import (
    API_BASE,
    PATH_ACCESS_LIST,
    PATH_ACCESS_OPEN,
    PATH_MOBIPASS_ACTIVATE,
    PATH_MOBIPASS_VERIFY,
)
from custom_components.intratone.rest_api import (
    IntratoneAccess,
    IntratoneAPI,
    IntratoneApiError,
    IntratoneAuthError,
    IntratoneMobipassError,
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


async def test_open_access_success(hass, mock_entry, aiomock) -> None:
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}{PATH_ACCESS_OPEN}",
        payload={"error": 0, "state": "ok"},
    )
    access = IntratoneAccess(
        access_id="42",
        phonenumber="0612345678",
        name="Portail",
        residence="Rés",
        openmode="data",
    )
    assert await api.open_access(access) is True

    open_url = f"{API_BASE}{PATH_ACCESS_OPEN}"
    sent = next(
        c for key, calls in aiomock.requests.items()
        if str(key[1]) == open_url
        for c in calls
    )
    assert sent.kwargs["data"] == {
        "phonenumber": "0612345678",
        "access_id": "42",
    }


async def test_open_access_refused_returns_false(hass, mock_entry, aiomock) -> None:
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}{PATH_ACCESS_OPEN}",
        payload={"error": 1, "state": "ok", "message": "not allowed"},
    )
    access = IntratoneAccess(
        access_id="42",
        phonenumber="0612345678",
        name="Portail",
        residence="Rés",
        openmode="data",
    )
    assert await api.open_access(access) is False


async def test_open_access_server_error_raises_with_body_and_status(
    hass, mock_entry, aiomock
) -> None:
    """A `state:error` open (e.g. ACCESS_OPENING_CLEMOBIL_FAILED) is retried
    once after a JWT refresh; if it fails again the raised error carries the
    HTTP status and full body so diagnostics can tell a rate-limit (429) from a
    plain refusal (see issue #39)."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    error_body = {"state": "error", "code": "ACCESS_OPENING_CLEMOBIL_FAILED"}
    # First open fails → JWT refresh → second open fails too (HTTP 429).
    aiomock.post(f"{API_BASE}{PATH_ACCESS_OPEN}", payload=error_body, status=429)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "newjwt", "id": "3844428"}},
    )
    aiomock.post(f"{API_BASE}{PATH_ACCESS_OPEN}", payload=error_body, status=429)

    access = IntratoneAccess(
        access_id="42",
        phonenumber="0612345678",
        name="Portail",
        residence="Rés",
        openmode="data",
    )
    with pytest.raises(IntratoneApiError) as exc:
        await api.open_access(access)
    assert exc.value.status == 429
    assert exc.value.body == error_body


async def test_list_access_parses_and_filters(hass, mock_entry, aiomock) -> None:
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        payload={
            "state": "ok",
            "data": {
                "list": [
                    # Real key is `openmode` (lowercase); value as a single
                    # string — the form confirmed in the decoders.
                    {
                        "id": 11,
                        "residence": "Résidence A",
                        "name": "Portail véhicule",
                        "phonenumber": "0612345678",
                        "openmode": "data",
                    },
                    # BLE accesses open via the REST API too (app routes ble →
                    # openAccessByApiUseCase) — kept.
                    {
                        "id": 14,
                        "residence": "Résidence A",
                        "name": "Portail BLE",
                        "phonenumber": "0600000002",
                        "openmode": ["ble"],
                    },
                    # clemobil opens by a GSM phone call, not the API — dropped.
                    {
                        "id": 12,
                        "residence": "Résidence A",
                        "name": "Hall piéton",
                        "phonenumber": "0698765432",
                        "openmode": "clemobil",
                    },
                    # Unknown primary mode — dropped.
                    {
                        "id": 13,
                        "residence": "Résidence A",
                        "name": "Vieux portail",
                        "phonenumber": "0600000000",
                        "openmode": ["unknown"],
                    },
                    # No id — dropped.
                    {
                        "id": 0,
                        "residence": "Résidence A",
                        "name": "Sans id",
                        "phonenumber": "0600000001",
                        "openmode": "data",
                    },
                ]
            },
        },
    )

    accesses = await api.list_access()
    assert [a.name for a in accesses] == ["Portail véhicule", "Portail BLE"]
    assert accesses[0].openmode == "data"
    assert accesses[1].openmode == "ble"


async def test_authenticate_device_parses_mobipass_state(
    hass, mock_entry, aiomock
) -> None:
    """The CléMobil/Mobipass flags are read off the auth/device response."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={
            "state": "ok",
            "data": {
                "jwt": "j",
                "id": "3844428",
                "openingaccess": "0",
                "access_refresh": "1",
                "mobipass_compatible": "1",
                "mobipass": "0",
            },
        },
    )

    await api.authenticate_device()
    state = api.mobipass_state
    assert state is not None
    assert state.mobipass_compatible is True
    assert state.mobipass is False
    assert state.opening_access is False
    assert state.refresh_access is True
    # Eligible for Mobipass but key held elsewhere → transfer needed (issue #61).
    assert state.needs_transfer is True


async def test_authenticate_device_logs_redacted_data(
    hass, mock_entry, aiomock, caplog
) -> None:
    """The debug dump of auth/device masks JWT + phone but keeps signal fields."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={
            "state": "ok",
            "data": {
                "jwt": "secret.jwt.value",
                "id": "3844428",
                "tel": "0671124546",
                "mobipass_compatible": "1",
                "mobipass": "0",
            },
        },
    )

    with caplog.at_level(
        logging.DEBUG, logger="custom_components.intratone.rest_api"
    ):
        await api.authenticate_device()

    assert "auth/device data (redacted)" in caplog.text
    # Secrets are masked …
    assert "secret.jwt.value" not in caplog.text
    assert "0671124546" not in caplog.text
    assert "***" in caplog.text
    # … but the fields we need for diagnosis are kept.
    assert "mobipass_compatible" in caplog.text


async def test_mobipass_activate_success(hass, mock_entry, aiomock) -> None:
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}",
        payload={"state": "ok", "error": 0},
    )
    # No raise = OTP request accepted.
    await api.mobipass_activate()


async def test_mobipass_verify_invalid_code_raises_with_code(
    hass, mock_entry, aiomock
) -> None:
    """A wrong OTP comes back as error!=0 + MOBIPASS_OTP_INVALID and is surfaced
    as an IntratoneMobipassError carrying that code (no JWT-refresh retry)."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_VERIFY}",
        payload={
            "state": "ok",
            "error": 1,
            "code": "MOBIPASS_OTP_INVALID",
            "message": "bad code",
        },
    )
    with pytest.raises(IntratoneMobipassError) as exc:
        await api.mobipass_verify("000000")
    assert exc.value.code == "MOBIPASS_OTP_INVALID"


async def test_mobipass_activate_retries_after_jwt_refresh(
    hass, mock_entry, aiomock
) -> None:
    """A first non-Mobipass failure (expired JWT) triggers one refresh + retry."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}",
        payload={"state": "error", "message": "expired"},
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "newjwt", "id": "3844428"}},
    )
    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}",
        payload={"state": "ok", "error": 0},
    )

    await api.mobipass_activate()
    assert store.jwt == "newjwt"


async def test_mobipass_not_available_is_not_retried(
    hass, mock_entry, aiomock
) -> None:
    """A genuine MOBIPASS_* rejection is raised immediately, not retried."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}",
        payload={"state": "error", "code": "MOBIPASS_NOT_AVAILABLE"},
    )
    with pytest.raises(IntratoneMobipassError) as exc:
        await api.mobipass_activate()
    assert exc.value.code == "MOBIPASS_NOT_AVAILABLE"


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
