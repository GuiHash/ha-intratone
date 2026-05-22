"""Per-entry persistence for rotated credentials.

Why a dedicated `Store` rather than `entry.data`: the JWT refreshes every 12 h
and the FCM credentials rotate occasionally on Google's MCS side. Writing
either to `entry.data` churns `.storage/core.config_entries.json` and bumps
the visible "modified" timestamp on the config entry. Keeping rotated state
in its own Store file leaves the config entry clean (device id, phone,
numeric id only) and reduces I/O noise.

Keyed by `entry.unique_id` (= numeric Intratone account id) so the config
flow can pre-write credentials before `async_create_entry` returns.
"""

from __future__ import annotations

from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

STORAGE_VERSION = 1
_STORAGE_KEY_TPL = "intratone.{key}.creds"


class CredsData(TypedDict, total=False):
    jwt: str | None
    fcm_token: str | None
    fcm_creds: dict[str, Any] | None


class IntratoneCredentialsStore:
    """Holds the rotated credentials for one Intratone account."""

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        self._store: Store[CredsData] = Store(
            hass, STORAGE_VERSION, _STORAGE_KEY_TPL.format(key=key)
        )
        self._cache: CredsData = {}
        self._loaded = False

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        self._cache = dict(loaded) if loaded else {}
        self._loaded = True

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def jwt(self) -> str | None:
        return self._cache.get("jwt")

    @property
    def fcm_token(self) -> str | None:
        return self._cache.get("fcm_token")

    @property
    def fcm_creds(self) -> dict[str, Any] | None:
        return self._cache.get("fcm_creds")

    def snapshot(self) -> CredsData:
        """Return a shallow copy of the cached values for diagnostics."""
        return dict(self._cache)

    async def async_update(
        self,
        *,
        jwt: str | None = None,
        fcm_token: str | None = None,
        fcm_creds: dict[str, Any] | None = None,
    ) -> None:
        """Persist any field the caller passed explicitly.

        Fields left as `None` (the default) are unchanged. To clear a field
        on purpose, store an empty string / dict.
        """
        if jwt is not None:
            self._cache["jwt"] = jwt
        if fcm_token is not None:
            self._cache["fcm_token"] = fcm_token
        if fcm_creds is not None:
            self._cache["fcm_creds"] = fcm_creds
        await self._store.async_save(self._cache)

    async def async_remove(self) -> None:
        await self._store.async_remove()
        self._cache = {}
