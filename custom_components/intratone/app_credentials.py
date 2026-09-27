"""Cogelec app credentials: Intratone API app id/token + Firebase project.

Entered by the user (config flow / options, stored in `entry.options`). An
optional `defaults.json` next to this module provides fallback values.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .const import (
    APP_CREDENTIAL_KEYS,
    CONF_APP_ID,
    CONF_APP_TOKEN,
    CONF_FCM_API_KEY,
    CONF_FCM_APP_ID,
    CONF_FCM_PROJECT_ID,
    CONF_FCM_SENDER_ID,
)

# Read at import time (HA imports integrations in an executor thread).
try:
    DEFAULTS: dict[str, str] = json.loads(
        (Path(__file__).parent / "defaults.json").read_text()
    )
except (OSError, ValueError):
    DEFAULTS = {}


class AppCredentialsMissing(Exception):
    """Raised when a credential has neither a user value nor a default."""


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    app_token: str
    fcm_project_id: str
    fcm_app_id: str
    fcm_api_key: str
    fcm_sender_id: str


def effective_values(overrides: Mapping[str, Any]) -> dict[str, str]:
    """Per-key user value, else default, else "" (missing)."""
    return {
        key: str(overrides.get(key) or DEFAULTS.get(key) or "")
        for key in APP_CREDENTIAL_KEYS
    }


def resolve_app_credentials(overrides: Mapping[str, Any]) -> AppCredentials:
    """Resolve the credentials for an entry's options (or a flow's input)."""
    values = effective_values(overrides)
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise AppCredentialsMissing(f"Missing app credentials: {', '.join(missing)}")
    return AppCredentials(
        app_id=values[CONF_APP_ID],
        app_token=values[CONF_APP_TOKEN],
        fcm_project_id=values[CONF_FCM_PROJECT_ID],
        fcm_app_id=values[CONF_FCM_APP_ID],
        fcm_api_key=values[CONF_FCM_API_KEY],
        fcm_sender_id=values[CONF_FCM_SENDER_ID],
    )


def credential_overrides(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Keep only the submitted values that differ from the defaults.

    A value equal to its default is not stored, so an updated default still
    applies; a cleared field falls back to the default.
    """
    overrides: dict[str, str] = {}
    for key in APP_CREDENTIAL_KEYS:
        value = str(user_input.get(key) or "").strip()
        if value and value != DEFAULTS.get(key):
            overrides[key] = value
    return overrides
