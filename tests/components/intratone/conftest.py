"""Common fixtures for Intratone tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intratone.const import (
    CONF_DEVICE_ID,
    CONF_FCM_CREDS,
    CONF_FCM_TOKEN,
    CONF_JWT,
    CONF_NUMERIC_ID,
    CONF_TEL,
    DOMAIN,
)


@pytest.fixture
def mock_entry_data() -> dict:
    return {
        CONF_DEVICE_ID: "ha-intratone-test",
        CONF_NUMERIC_ID: "3844428",
        CONF_TEL: "0671124546",
        CONF_JWT: "fake.jwt.token",
        CONF_FCM_TOKEN: "fake-fcm-token",
        CONF_FCM_CREDS: {"gcm": {"android_id": 1, "security_token": 2}},
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
def mock_fcm_client():
    """Patch FcmPushClient so no MCS connection is opened during setup.

    `FcmPushClient` is imported lazily inside the listener, so we patch
    the source module instead of the consumer.
    """
    client = MagicMock()
    client.checkin_or_register = AsyncMock(return_value="fake-fcm-token")
    client.start = AsyncMock()
    client.stop = AsyncMock()
    with patch("firebase_messaging.FcmPushClient", return_value=client) as cls:
        cls.instance = client
        yield cls
