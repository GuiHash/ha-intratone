"""REST client tests — happy path + error path with aioresponses."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intratone.const import (
    API_BASE,
    CONF_DEVICE_ID,
    CONF_INDICATIF,
    CONF_NUMERIC_ID,
    CONF_TEL,
    DOMAIN,
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
    IntratoneConnectionError,
    IntratoneMobipassError,
    authenticate_for_invite,
    register_phone_for_sms,
    register_with_invite,
    validate_sms_code,
    verify_user,
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


async def test_mobipass_sms_already_sent_is_not_an_error(
    hass, mock_entry, aiomock
) -> None:
    """MOBIPASS_SMS_SENT means the code was already sent (e.g. a retried
    activate); the app moves straight to code entry, so we must NOT raise."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}",
        payload={"state": "error", "code": "MOBIPASS_SMS_SENT"},
    )
    # No raise = caller proceeds to the OTP-entry step.
    await api.mobipass_activate()


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


async def test_verify_user_returns_account_flags(hass, aiomock) -> None:
    """api/auth/verify surfaces the account-level CléMobil flags."""
    aiomock.post(
        f"{API_BASE}api/auth/verify",
        payload={
            "state": "ok",
            "data": {"compatible": "1", "openingaccess": "1", "inuse": "0"},
        },
    )
    data = await verify_user(
        async_get_clientsession(hass), tel="612345678", indicatif="33"
    )
    assert data["compatible"] == "1"
    assert data["openingaccess"] == "1"


async def test_verify_user_best_effort_on_failure(hass, aiomock) -> None:
    """verify is best-effort: a server error must yield {} (never raise), so it
    can't block onboarding."""
    aiomock.post(f"{API_BASE}api/auth/verify", status=500)
    data = await verify_user(
        async_get_clientsession(hass), tel="612345678", indicatif="33"
    )
    assert data == {}


# --- Finding 1: non-JSON bodies must surface as IntratoneApiError -----------


async def test_answer_call_non_json_body_raises_api_error(
    hass, mock_entry, aiomock
) -> None:
    """A non-JSON body (HTML 502 page from a proxy) must be a typed
    IntratoneApiError — so answer_call's refresh-retry triggers — and must not
    embed the raw body in the message."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(f"{API_BASE}api/calls/123/answer", body="<html>bad gateway</html>")
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "newjwt", "id": "3844428"}},
    )
    aiomock.post(f"{API_BASE}api/calls/123/answer", body="<html>bad gateway</html>")

    with pytest.raises(IntratoneApiError) as exc:
        await api.answer_call("123")
    assert "<html>" not in str(exc.value)
    # The refresh-retry DID happen (typed error → retry path).
    assert store.jwt == "newjwt"


async def test_register_with_invite_non_json_raises_api_error(hass, aiomock) -> None:
    aiomock.post(f"{API_BASE}api/auth/registercodes", body="<html>oops</html>")
    with pytest.raises(IntratoneApiError):
        await register_with_invite(
            async_get_clientsession(hass),
            device_id="ha-test",
            fcm_token="tok",
            code="123456",
            codepass="7890",
        )


async def test_authenticate_for_invite_skips_non_json_candidate(hass, aiomock) -> None:
    """A non-JSON body for one candidate moves on to the next (the
    per-candidate `continue` must actually be reachable)."""
    aiomock.post(f"{API_BASE}api/auth/device", body="<html>oops</html>")
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "ok", "id": "9"}},
    )
    data = await authenticate_for_invite(
        async_get_clientsession(hass), tel="0612345678", device_id="ha-test"
    )
    assert data["jwt"] == "ok"


# --- Finding 2: network errors wrapped into IntratoneApiError ---------------


async def test_answer_call_network_error_does_not_refresh(
    hass, mock_entry, aiomock
) -> None:
    """A transient connection reset is wrapped into IntratoneConnectionError
    (typed, so lock.py surfaces a proper HomeAssistantError) but must NOT
    trigger the JWT-refresh retry: the token was never the problem, and a
    pointless auth/device round-trip on a dead network blocks the ring hot
    path and hammers the rate-limited auth endpoint (issue #39)."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}api/calls/123/answer",
        exception=aiohttp.ClientConnectionError("reset"),
    )

    with pytest.raises(IntratoneConnectionError):
        await api.answer_call("123")
    # No auth/device call — the refresh path must not run.
    assert not any(
        str(key[1]).endswith("api/auth/device") for key in aiomock.requests
    )


async def test_register_phone_for_sms_network_error_wrapped(hass, aiomock) -> None:
    aiomock.post(
        f"{API_BASE}api/auth/register",
        exception=aiohttp.ClientConnectionError("reset"),
    )
    with pytest.raises(IntratoneApiError):
        await register_phone_for_sms(
            async_get_clientsession(hass),
            device_id="ha-test",
            fcm_token="tok",
            tel="612345678",
            indicatif="33",
        )


async def test_validate_sms_code_timeout_wrapped(hass, aiomock) -> None:
    aiomock.post(f"{API_BASE}api/auth/validate", exception=asyncio.TimeoutError())
    with pytest.raises(IntratoneApiError):
        await validate_sms_code(
            async_get_clientsession(hass),
            tel="612345678",
            indicatif="33",
            device_id="ha-test",
            code="1234",
        )


async def test_authenticate_for_invite_network_error_wrapped(hass, aiomock) -> None:
    aiomock.post(
        f"{API_BASE}api/auth/device",
        exception=aiohttp.ClientConnectionError("reset"),
        repeat=True,
    )
    with pytest.raises(IntratoneApiError):
        await authenticate_for_invite(
            async_get_clientsession(hass), tel="0612345678", device_id="ha-test"
        )


# --- Finding 3: HTTP >= 400 without a `state` envelope is an error ----------


async def test_answer_call_http_401_without_envelope_triggers_refresh(
    hass, mock_entry, aiomock
) -> None:
    """A 401 JSON body without the `state` envelope must NOT be treated as
    success ("not picked up") — it must raise so the JWT-refresh retry runs."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}api/calls/123/answer",
        status=401,
        payload={"message": "Unauthenticated."},
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "newjwt", "id": "3844428"}},
    )
    aiomock.post(
        f"{API_BASE}api/calls/123/answer", payload={"error": 0, "state": "ok"}
    )

    assert await api.answer_call("123") is True
    assert store.jwt == "newjwt"


async def test_list_access_http_503_raises_api_error(
    hass, mock_entry, aiomock
) -> None:
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        status=503,
        payload={"message": "unavailable"},
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "newjwt", "id": "3844428"}},
    )
    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        status=503,
        payload={"message": "unavailable"},
    )

    with pytest.raises(IntratoneApiError) as exc:
        await api.list_access()
    assert exc.value.status == 503


# --- Finding 4: concurrent refresh_jwt shares one in-flight auth ------------


async def test_concurrent_refresh_jwt_runs_authenticate_device_once(
    hass, mock_entry
) -> None:
    """Two concurrent 401s (multi-lock scene) must not stampede auth/device."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    calls = 0

    async def fake_auth():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"jwt": "newjwt", "id": "3844428"}

    with patch.object(api, "authenticate_device", fake_auth):
        await asyncio.gather(api.refresh_jwt(), api.refresh_jwt())
        assert calls == 1
        assert store.jwt == "newjwt"

        # A later, non-concurrent refresh must run a fresh auth.
        await api.refresh_jwt()
        assert calls == 2


# --- Finding 5: call_id is URL-quoted ----------------------------------------


async def test_answer_call_quotes_call_id(hass, mock_entry, aiomock) -> None:
    """call_id comes from an FCM push / service call — it must be URL-quoted."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}api/calls/ab%3Fcd/answer", payload={"error": 0, "state": "ok"}
    )
    assert await api.answer_call("ab?cd") is True


# --- Finding 6: no credentials in auth failure messages ----------------------


async def test_authenticate_device_failure_message_masks_tel(
    hass, mock_entry, aiomock
) -> None:
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "error", "message": "AUTH_FAILED"},
        repeat=True,
    )
    with pytest.raises(IntratoneAuthError) as exc:
        await api.authenticate_device()
    assert "0671124546" not in str(exc.value)
    # The server-side reason is still surfaced.
    assert "AUTH_FAILED" in str(exc.value)


async def test_authenticate_device_no_jwt_message_is_helpful(
    hass, mock_entry, aiomock
) -> None:
    """All candidates accepted but no JWT returned → message must say so, not
    end in an unhelpful ': None'."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}api/auth/device", payload={"state": "ok", "data": {}}, repeat=True
    )
    with pytest.raises(IntratoneAuthError) as exc:
        await api.authenticate_device()
    assert "None" not in str(exc.value)


async def test_authenticate_for_invite_failure_masks_credentials(
    hass, aiomock
) -> None:
    """The no-JWT error must not dump tel/device_id from the response body."""
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={
            "state": "ok",
            "data": {"tel": "0612345678", "device_id": "devsecret123"},
        },
        repeat=True,
    )
    with pytest.raises(IntratoneAuthError) as exc:
        await authenticate_for_invite(
            async_get_clientsession(hass), tel="0612345678", device_id="devsecret123"
        )
    assert "0612345678" not in str(exc.value)
    assert "devsecret123" not in str(exc.value)


# --- Finding 7: envelope flags may be int or string ---------------------------


async def test_answer_call_accepts_string_error_flag(
    hass, mock_entry, aiomock
) -> None:
    """§4.2: numeric envelope flags are sent as int OR string."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}api/calls/123/answer", payload={"error": "0", "state": "ok"}
    )
    assert await api.answer_call("123") is True


async def test_open_access_accepts_string_error_flag(
    hass, mock_entry, aiomock
) -> None:
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}{PATH_ACCESS_OPEN}", payload={"error": "0", "state": "ok"}
    )
    access = IntratoneAccess(
        access_id="42",
        phonenumber="0612345678",
        name="Portail",
        residence="Rés",
        openmode="data",
    )
    assert await api.open_access(access) is True


async def test_mobipass_activate_accepts_string_error_flag(
    hass, mock_entry, aiomock
) -> None:
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    aiomock.post(
        f"{API_BASE}{PATH_MOBIPASS_ACTIVATE}",
        payload={"state": "ok", "error": "0"},
    )
    # No raise = OTP request accepted.
    await api.mobipass_activate()


# --- Finding 8: periodic refresh failure paths -------------------------------


async def test_periodic_refresh_starts_reauth_on_auth_error(
    hass, mock_entry
) -> None:
    """A definitive server rejection (IntratoneAuthError) during the periodic
    refresh must start a reauth flow (the FCM unregister push may be dead)."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    with patch(
        "custom_components.intratone.rest_api.async_track_time_interval"
    ) as track:
        api.async_start_jwt_refresh()
    tick = track.call_args[0][1]

    with (
        patch.object(
            api, "refresh_jwt", AsyncMock(side_effect=IntratoneAuthError("revoked"))
        ),
        patch.object(mock_entry, "async_start_reauth") as reauth,
    ):
        await tick(None)
    reauth.assert_called_once_with(hass)


async def test_periodic_refresh_network_error_does_not_reauth(
    hass, mock_entry, aiomock
) -> None:
    """End-to-end: a network outage at the refresh tick must NOT start reauth.

    Regression guard: authenticate_device wraps per-candidate failures into
    IntratoneAuthError — a connection error reaching that path would be
    misread as "credentials revoked" and force a re-pair for a WiFi blip."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    with patch(
        "custom_components.intratone.rest_api.async_track_time_interval"
    ) as track:
        api.async_start_jwt_refresh()
    tick = track.call_args[0][1]

    for _ in range(3):  # one per tel candidate, in case all are tried
        aiomock.post(
            f"{API_BASE}api/auth/device",
            exception=aiohttp.ClientConnectionError("dns blip"),
        )
    with patch.object(mock_entry, "async_start_reauth") as reauth:
        await tick(None)  # must not raise
    reauth.assert_not_called()


async def test_periodic_refresh_server_5xx_does_not_reauth(
    hass, mock_entry, aiomock
) -> None:
    """End-to-end: an Intratone outage (HTTP 503) at the refresh tick is not
    a credential verdict — it must NOT start reauth."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    with patch(
        "custom_components.intratone.rest_api.async_track_time_interval"
    ) as track:
        api.async_start_jwt_refresh()
    tick = track.call_args[0][1]

    for _ in range(3):  # one per tel candidate, in case all are tried
        aiomock.post(
            f"{API_BASE}api/auth/device",
            status=503,
            payload={"message": "maintenance"},
        )
    with patch.object(mock_entry, "async_start_reauth") as reauth:
        await tick(None)  # must not raise
    reauth.assert_not_called()


async def test_periodic_refresh_transient_error_does_not_reauth(
    hass, mock_entry
) -> None:
    """A transient failure (IntratoneApiError) keeps the warning-only path."""
    mock_entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), mock_entry, store)

    with patch(
        "custom_components.intratone.rest_api.async_track_time_interval"
    ) as track:
        api.async_start_jwt_refresh()
    tick = track.call_args[0][1]

    with (
        patch.object(
            api, "refresh_jwt", AsyncMock(side_effect=IntratoneApiError("boom"))
        ),
        patch.object(mock_entry, "async_start_reauth") as reauth,
    ):
        await tick(None)  # must not raise
    reauth.assert_not_called()


# --- Finding 9: indicatif not hardcoded to "33" -------------------------------


def _auth_device_calls(aiomock):
    return next(
        calls
        for key, calls in aiomock.requests.items()
        if str(key[1]).endswith("api/auth/device")
    )


async def test_authenticate_for_invite_uses_indicatif_kwarg(hass, aiomock) -> None:
    aiomock.post(f"{API_BASE}api/auth/device", payload={"state": "ok", "data": {}})
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "ok", "id": "9"}},
    )
    data = await authenticate_for_invite(
        async_get_clientsession(hass),
        tel="0612345678",
        device_id="ha-test",
        indicatif="32",
    )
    assert data["jwt"] == "ok"
    calls = _auth_device_calls(aiomock)
    assert calls[1].kwargs["data"]["tel"] == "32612345678"


async def test_authenticate_device_uses_entry_indicatif(hass, aiomock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1",
        data={
            CONF_DEVICE_ID: "ha-test",
            CONF_NUMERIC_ID: "1",
            CONF_TEL: "0671124546",
            CONF_INDICATIF: "352",
        },
    )
    entry.add_to_hass(hass)
    store = await _seeded_store(hass)
    api = IntratoneAPI(hass, async_get_clientsession(hass), entry, store)

    aiomock.post(f"{API_BASE}api/auth/device", payload={"state": "ok", "data": {}})
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "j", "id": "1"}},
    )
    data = await api.authenticate_device()
    assert data["jwt"] == "j"
    calls = _auth_device_calls(aiomock)
    assert calls[1].kwargs["data"]["tel"] == "352671124546"
