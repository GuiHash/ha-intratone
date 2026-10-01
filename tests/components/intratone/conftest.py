"""Common fixtures for Intratone tests."""

from __future__ import annotations

import sys
import threading
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry


if sys.version_info[:2] == (3, 12):
    # Python 3.12 ThreadPoolExecutor creates a _run_safe_shutdown_loop daemon
    # thread that persists for the interpreter lifetime. pytest-homeassistant-
    # custom-component <=0.13.205 (the last versions supporting Py3.12) don't
    # whitelist it; the fix landed in 0.13.210 which requires Python >=3.13.
    _orig_enumerate = threading.enumerate
    threading.enumerate = lambda: [
        t for t in _orig_enumerate() if "_run_safe_shutdown_loop" not in t.name
    ]

from custom_components.intratone.app_credentials import (
    AppCredentials,
    resolve_app_credentials,
)
from custom_components.intratone.call_manager import CallManager
from custom_components.intratone.const import (
    CONF_DEVICE_ID,
    CONF_FCM_CREDS,
    CONF_FCM_TOKEN,
    CONF_JWT,
    CONF_NUMERIC_ID,
    CONF_REGISTER_METHOD,
    CONF_TEL,
    DOMAIN,
    REGISTER_METHOD_INVITE,
)


# Fake app credential defaults (never the real ones).
FAKE_DEFAULT_CREDENTIALS = {
    "app_id": "test_app_id",
    "app_token": "test-app-token",
    "fcm_project_id": "test-project",
    "fcm_app_id": "1:000000000000:android:0000000000000000",
    "fcm_api_key": "test-fcm-api-key",
    "fcm_sender_id": "000000000000",
}


@pytest.fixture(autouse=True)
def default_credentials():
    """Provide app credential defaults so tests don't have to enter them.

    Tests of the "nothing provided" case clear the returned dict.
    """
    defaults = dict(FAKE_DEFAULT_CREDENTIALS)
    with patch("custom_components.intratone.app_credentials.DEFAULTS", defaults):
        yield defaults


@pytest.fixture
def app_creds(default_credentials) -> AppCredentials:
    return resolve_app_credentials({})
@pytest.fixture
def mock_entry_data() -> dict:
    return {
        CONF_DEVICE_ID: "ha-intratone-test",
        CONF_NUMERIC_ID: "3844428",
        CONF_TEL: "0671124546",
        CONF_JWT: "fake.jwt.token",
        CONF_FCM_TOKEN: "fake-fcm-token",
        CONF_FCM_CREDS: {"gcm": {"android_id": 1, "security_token": 2}},
        CONF_REGISTER_METHOD: REGISTER_METHOD_INVITE,
    }


@pytest.fixture
def mock_entry(mock_entry_data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="3844428",
        title="Intratone (0671124546)",
        data=mock_entry_data,
    )


@pytest.fixture
def mock_entry_data_modern() -> dict:
    """Current-shape entry.data: no rotated credentials.

    Current installs never write JWT / FCM token / FCM creds to entry.data —
    they live in the Store from the start (see `mock_entry_data` for the
    legacy shape, still covered by the migration test)."""
    return {
        CONF_DEVICE_ID: "ha-intratone-test",
        CONF_NUMERIC_ID: "3844428",
        CONF_TEL: "0671124546",
        CONF_REGISTER_METHOD: REGISTER_METHOD_INVITE,
    }


@pytest.fixture
def mock_entry_modern(mock_entry_data_modern) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="3844428",
        title="Intratone (0671124546)",
        data=mock_entry_data_modern,
    )


@pytest.fixture
def mock_fcm_client():
    """Patch FcmPushClient so no MCS connection is opened during setup.

    `FcmPushClient` is imported lazily inside the listener, so we patch
    the source module instead of the consumer. Autospec'd against the real
    class so a call to a method it doesn't have fails the test instead of
    silently succeeding.
    """
    from firebase_messaging import FcmPushClient
    from firebase_messaging.fcmpushclient import FcmPushClientRunState

    client = create_autospec(FcmPushClient, instance=True)
    client.checkin_or_register = AsyncMock(return_value="fake-fcm-token")
    client.start = AsyncMock()
    client.stop = AsyncMock()
    # `run_state`/`tasks` are set on the instance inside __init__, so autospec
    # (which only inspects the class) doesn't know about them. Set them to the
    # values a successfully-started client would have — fcm_listener.py reads
    # `run_state` via getattr() to detect the client silently stopping itself.
    client.run_state = FcmPushClientRunState.STARTED
    client.tasks = []
    with patch("firebase_messaging.FcmPushClient", return_value=client) as cls:
        cls.instance = client
        yield cls


@pytest.fixture
def mock_call_manager():
    """Patch CallManager so async_setup_entry can run without binding a UDP socket.

    pytest-socket blocks real socket creation in the suite. Returns the
    MagicMock so tests can assert on start_call / hang_up if they want.
    Autospec'd against the real class so a call to a method it doesn't have
    fails the test instead of silently succeeding.
    """
    cm = create_autospec(CallManager, instance=True)
    cm.async_start = AsyncMock()
    cm.async_stop = AsyncMock()
    cm.start_call = AsyncMock(return_value="fake-call-id")
    cm.hang_up = AsyncMock()
    cm.abort_active_call = AsyncMock()
    # No previous call by default — the coordinator's new-push handler only
    # invokes abort_active_call when this is truthy.
    cm.active_call_id = None
    with (
        patch(
            "custom_components.intratone.CallManager", return_value=cm
        ) as cm_cls,
        patch(
            "custom_components.intratone.async_get_source_ip",
            new=AsyncMock(return_value="192.0.2.10"),
        ),
    ):
        cm_cls.instance = cm
        yield cm_cls
