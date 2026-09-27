"""App credentials resolution — per-entry value, else default."""

from __future__ import annotations

import pytest

from custom_components.intratone.app_credentials import (
    AppCredentialsMissing,
    credential_overrides,
    resolve_app_credentials,
)
from custom_components.intratone.const import CONF_APP_ID, CONF_APP_TOKEN


def test_resolve_uses_default_when_no_override(default_credentials) -> None:
    creds = resolve_app_credentials({})
    assert creds.app_token == default_credentials[CONF_APP_TOKEN]


def test_resolve_prefers_override(default_credentials) -> None:
    creds = resolve_app_credentials({CONF_APP_TOKEN: "user-token"})
    assert creds.app_token == "user-token"
    assert creds.app_id == default_credentials[CONF_APP_ID]


def test_resolve_raises_without_value_nor_default(default_credentials) -> None:
    """No defaults: every key must be entered."""
    default_credentials.clear()
    with pytest.raises(AppCredentialsMissing, match="app_token"):
        resolve_app_credentials({CONF_APP_ID: "x"})


def test_overrides_drop_default_and_empty_values(default_credentials) -> None:
    """Only real overrides are stored, so an updated default still reaches
    the entry; an empty field falls back to the default."""
    overrides = credential_overrides(
        {
            CONF_APP_ID: default_credentials[CONF_APP_ID],
            CONF_APP_TOKEN: "  user-token  ",
            "fcm_api_key": "",
        }
    )
    assert overrides == {CONF_APP_TOKEN: "user-token"}
