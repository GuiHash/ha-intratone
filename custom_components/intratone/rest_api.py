"""Async REST client for the Intratone API.

Reverse-engineered from `com.cogelec.notificationpush` v4.6.4.
All endpoints use form-urlencoded POST and Bearer JWT auth.
See INTRATONE_API.md for the full reference.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    API_BASE,
    APP_ID,
    APP_TOKEN,
    APP_USER_AGENT,
    APP_VERSION,
    CONF_DEVICE_ID,
    CONF_INDICATIF,
    CONF_NUMERIC_ID,
    CONF_TEL,
    API_OPENABLE_MODES,
    DEFAULT_INDICATIF,
    DEVICE_BUNDLE_ID,
    DEVICE_DESCRIPTION,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_OS,
    DEVICE_OSV,
    JWT_REFRESH_INTERVAL_HOURS,
    MOBIPASS_CODE_SMS_SENT,
    PATH_ACCESS_LIST,
    PATH_ACCESS_OPEN,
    PATH_MOBIPASS_ACTIVATE,
    PATH_MOBIPASS_VERIFY,
)
from .store import IntratoneCredentialsStore

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class IntratoneAuthError(Exception):
    """Raised when authentication or registration fails."""


class IntratoneApiError(Exception):
    """Raised on unexpected API errors.

    Carries the HTTP `status` and parsed `body` (when available) so callers can
    log the full server response for diagnostics instead of just a message —
    e.g. to tell a transient open failure from a rate-limit (HTTP 429).
    """

    def __init__(
        self, message: str, *, status: int | None = None, body: Any = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class IntratoneConnectionError(IntratoneApiError):
    """Transport-level failure (connection error, timeout) — the server was
    never reached or never answered.

    Kept distinct from `IntratoneApiError` so callers don't misread an
    outage as a server decision: retry-after-JWT-refresh paths skip the
    pointless (rate-limited, issue #39) auth round-trip, and the periodic
    refresh doesn't escalate a WiFi blip into a reauth flow.
    """


class IntratoneMobipassError(IntratoneApiError):
    """A Mobipass ("CléMobil") transfer request was refused by the server.

    Carries the server `code` (e.g. `MOBIPASS_OTP_INVALID`) so the config flow
    can map it to a specific user-facing message.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message, status=status, body=body)
        self.code = code


@dataclass(frozen=True)
class IntratoneAccess:
    """One remote-openable access ("Clé mobile" / mobipass entry).

    Mirrors the `AccessItem` model (fields `id`, `residence`, `name`,
    `phonenumber`, `openmode`). `openmode` here is the access's *primary* mode
    — `data` (mobipass, 4G) or `ble` — kept for display/diagnostics only; it is
    NOT sent when opening (the open body is just `phonenumber` + `access_id`).
    """

    access_id: str
    phonenumber: str
    name: str
    residence: str
    openmode: str


@dataclass(frozen=True)
class MobipassState:
    """The four CléMobil/Mobipass flags from the `api/auth/device` response.

    Parsed from `data.{openingaccess,access_refresh,mobipass_compatible,mobipass}`
    (each sent by the server as the string `"1"`/`"0"`). Mirrors the Android
    `AuthDevice` model. `needs_transfer` is the issue-#61 condition: the account
    supports the single-owner Mobipass scheme (`mobipass_compatible`) but the key
    is currently held by another device (`not mobipass`) — typically the user's
    phone — so `list_access()` returns nothing until the key is transferred here.
    """

    opening_access: bool
    refresh_access: bool
    mobipass_compatible: bool
    mobipass: bool

    @property
    def needs_transfer(self) -> bool:
        return self.mobipass_compatible and not self.mobipass


def _parse_mobipass_state(data: dict[str, Any]) -> MobipassState:
    """Read the CléMobil/Mobipass flags out of an `api/auth/device` `data` block."""

    def flag(key: str) -> bool:
        return str(data.get(key, "0")) == "1"

    return MobipassState(
        opening_access=flag("openingaccess"),
        refresh_access=flag("access_refresh"),
        mobipass_compatible=flag("mobipass_compatible"),
        mobipass=flag("mobipass"),
    )


def _redact_auth_data(data: dict[str, Any]) -> dict[str, Any]:
    """A copy of an `auth/device` `data` block with secrets masked for logs.

    Lets testers paste the full raw block (issue #61 diagnosis) without leaking
    their JWT or phone number.
    """
    redacted = dict(data)
    for key in ("jwt", "tel", "device_id"):
        if redacted.get(key):
            redacted[key] = "***"
    return redacted


def _mobipass_error(body: dict[str, Any], status: int | None) -> IntratoneMobipassError:
    """Build an `IntratoneMobipassError` from a server error body."""
    code = str(body.get("code") or "") or None
    message = body.get("message") or code or "unknown"
    return IntratoneMobipassError(
        f"Mobipass request refused: {message}",
        code=code,
        status=status,
        body=body,
    )


def _error_flag_ok(value: Any) -> bool:
    """True when an envelope `error` flag equals 0 (sent as int OR string, §4.2).

    `value in (0, "0")` also accepts a JSON `false` (bool == 0 in Python),
    preserving the pre-helper `== 0` behavior.
    """
    return value in (0, "0")


def _tel_candidates(tel: str, indicatif: str) -> list[str]:
    """Phone-number forms to try against auth/device.

    Some accounts are keyed on the bare national number, others on the
    indicatif-prefixed form — try stored form first, then the variants.
    """
    candidates = [tel]
    stripped = tel.lstrip("0")
    if stripped and stripped != tel:
        candidates.append(f"{indicatif}{stripped}")
        candidates.append(stripped)
    return candidates


async def _parse_json_response(
    resp: aiohttp.ClientResponse, path: str
) -> dict[str, Any]:
    """Parse and validate a JSON envelope response.

    Raises `IntratoneApiError` on a non-JSON body, a non-dict shape, a
    `state: error` envelope, or an HTTP error status without one.
    """
    status = resp.status
    try:
        body = await resp.json(content_type=None)
    except ValueError as err:
        # With `content_type=None` aiohttp skips its content-type check, so a
        # non-JSON body (e.g. an HTML error page from a proxy) surfaces as
        # json.JSONDecodeError, a ValueError. Don't embed the raw body in the
        # message — it may carry credentials.
        raise IntratoneApiError(
            f"Non-JSON response from {path} (HTTP {status})",
            status=status,
        ) from err

    if not isinstance(body, dict):
        raise IntratoneApiError(
            f"Unexpected response shape from {path} (HTTP {status}): {body!r}",
            status=status,
            body=body,
        )

    if body.get("state") == "error" or status >= 400:
        msg = body.get("message") or body.get("code") or "unknown"
        # Full body + status at debug so a beta-tester's logs show
        # exactly what the server returned (rate-limit, gate offline…).
        _LOGGER.debug("API error from %s (HTTP %s): %s", path, status, body)
        raise IntratoneApiError(
            f"API error from {path} (HTTP {status}): {msg}",
            status=status,
            body=body,
        )

    return body


class IntratoneAPI:
    """Async client for the Intratone REST API."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        entry: ConfigEntry,
        store: IntratoneCredentialsStore,
    ) -> None:
        self._hass = hass
        self._session = session
        self._entry = entry
        self._store = store
        self._refresh_unsub = None
        # In-flight JWT refresh, shared by concurrent callers (see refresh_jwt).
        self._refresh_task: asyncio.Task | None = None
        # Latest CléMobil/Mobipass flags, refreshed on every `authenticate_device`
        # (i.e. on setup's first refresh and every periodic JWT refresh). `None`
        # until the first successful auth. See `MobipassState.needs_transfer`.
        self.mobipass_state: MobipassState | None = None

    @property
    def jwt(self) -> str | None:
        return self._store.jwt

    @property
    def numeric_id(self) -> str | None:
        return self._entry.data.get(CONF_NUMERIC_ID)

    @property
    def tel(self) -> str | None:
        return self._entry.data.get(CONF_TEL)

    @property
    def device_id(self) -> str | None:
        return self._entry.data.get(CONF_DEVICE_ID)

    async def _post_form(
        self,
        path: str,
        form: dict[str, str],
        *,
        jwt: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": APP_USER_AGENT,
        }
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"

        try:
            async with self._session.post(
                API_BASE + path, data=form, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                return await _parse_json_response(resp, path)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            # Wrap transport errors so callers' typed retry logic triggers.
            raise IntratoneConnectionError(f"Network error calling {path}: {err}") from err

    async def authenticate_device(self) -> dict[str, Any]:
        """POST /api/auth/device — also serves as JWT refresh.

        Tries the stored phone number with several normalizations.
        Returns the `data` block (containing `jwt`, `id`, `tel`, etc.).
        """
        if not self.tel or not self.device_id:
            raise IntratoneAuthError("Cannot authenticate without tel and device_id")

        indicatif = self._entry.data.get(CONF_INDICATIF) or DEFAULT_INDICATIF
        candidates = _tel_candidates(self.tel, indicatif)

        last_error: Exception | None = None
        for tel in candidates:
            try:
                body = await self._post_form(
                    "api/auth/device",
                    {
                        "app_id": APP_ID,
                        "app_token": APP_TOKEN,
                        "tel": tel,
                        "device_id": self.device_id,
                        "appversion": APP_VERSION,
                    },
                )
            except IntratoneConnectionError:
                # Not candidate-specific — the server is unreachable. Propagate
                # as-is so the refresh tick treats it as transient instead of
                # wrapping it into a "credentials revoked" IntratoneAuthError.
                raise
            except IntratoneApiError as err:
                last_error = err
                continue

            data = body.get("data") or {}
            if data.get("jwt"):
                self.mobipass_state = _parse_mobipass_state(data)
                # Log the raw data block (secrets masked) AND the parsed flags:
                # the flags we parse aren't a reliable signal for our client
                # (issue #61 — the app is fed them via FCM push), so the raw
                # block lets a tester's log reveal the real signal even if it
                # lives in a field we don't parse yet.
                _LOGGER.debug(
                    "auth/device data (redacted): %s | parsed mobipass flags: %s",
                    _redact_auth_data(data),
                    self.mobipass_state,
                )
                return data

        # A 5xx or non-JSON failure (body is None) is a server outage, not a
        # credential verdict — re-raise it as IntratoneApiError so the refresh
        # tick keeps its warn-and-retry path instead of starting a reauth.
        if isinstance(last_error, IntratoneApiError) and (
            last_error.status is None
            or last_error.status >= 500
            or last_error.body is None
        ):
            raise last_error

        # Don't put the phone number in the message — it ends up in WARNING logs.
        detail = (
            str(last_error)
            if last_error is not None
            else "no JWT in any auth/device response"
        )
        raise IntratoneAuthError(f"Authentication failed: {detail}")

    async def answer_call(self, call_id: str) -> bool:
        """POST /api/calls/{call_id}/answer — opens the door."""
        if not self.jwt:
            raise IntratoneAuthError("No JWT available")
        if not self.numeric_id:
            raise IntratoneAuthError("No numeric_id available")

        # call_id comes from an FCM push / service call — quote it.
        path = f"api/calls/{quote(call_id, safe='')}/answer"
        try:
            body = await self._post_form(
                path,
                {"smartphone_id": self.numeric_id},
                jwt=self.jwt,
            )
        except IntratoneConnectionError:
            raise  # transport failure — a JWT refresh can't help
        except IntratoneApiError:
            # Try a JWT refresh once on failure (likely 401).
            await self.refresh_jwt()
            body = await self._post_form(
                path,
                {"smartphone_id": self.numeric_id},
                jwt=self.jwt,
            )

        return _error_flag_ok(body.get("error"))

    async def _get_json(self, path: str) -> dict[str, Any]:
        """Authenticated GET returning the parsed JSON body.

        Refreshes the JWT once and retries on error (likely 401), mirroring
        `answer_call`.
        """
        if not self.jwt:
            raise IntratoneAuthError("No JWT available")

        async def _do() -> dict[str, Any]:
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.jwt}",
                "User-Agent": APP_USER_AGENT,
            }
            try:
                async with self._session.get(
                    API_BASE + path, headers=headers, timeout=REQUEST_TIMEOUT
                ) as resp:
                    return await _parse_json_response(resp, path)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise IntratoneConnectionError(
                    f"Network error calling {path}: {err}"
                ) from err

        try:
            return await _do()
        except IntratoneConnectionError:
            raise  # transport failure — a JWT refresh can't help
        except IntratoneApiError:
            await self.refresh_jwt()
            return await _do()

    async def list_access(self) -> list[IntratoneAccess]:
        """GET /api/access — list the remote-openable accesses ("Clé mobile").

        Returns one entry per access whose primary mode the REST API can open
        (`data`/mobipass or `ble`). `clemobil` accesses (app opens those by
        placing a GSM phone call), `unknown`, and id-less entries are skipped.
        """
        body = await self._get_json(PATH_ACCESS_LIST)
        # The exact server JSON shape isn't documented; log it once so a real
        # mobipass install can confirm/refine the parser below.
        _LOGGER.debug("Access list raw response: %s", body)
        accesses = _parse_accesses(body)
        _LOGGER.info("Intratone: %d remote-openable access(es) found", len(accesses))
        return accesses

    async def open_access(self, access: IntratoneAccess) -> bool:
        """POST /api/access/open/clemobil — open a door/gate without ringing.

        The endpoint is fixed for both mobipass and legacy Clémobil — the
        server resolves which mode to use from the access itself. The body is
        just the access identifiers from the iOS `AccessItem`: `phonenumber`
        and `access_id` (the struct's `id`). Confirmed by disassembling
        `APIManager.openAccessWithCleMobil` (no `openmode` is sent).
        """
        if not self.jwt:
            raise IntratoneAuthError("No JWT available")

        form = {
            "phonenumber": access.phonenumber,
            "access_id": access.access_id,
        }
        _LOGGER.debug(
            "Opening access id=%s mode=%s", access.access_id, access.openmode
        )
        try:
            body = await self._post_form(PATH_ACCESS_OPEN, form, jwt=self.jwt)
        except IntratoneConnectionError:
            raise  # transport failure — a JWT refresh can't help
        except IntratoneApiError as first_err:
            # A first failure is usually just an expired JWT (401): refresh once
            # and retry. If the retry also fails, the server is genuinely
            # refusing the open — log the full response (status + body) so a
            # beta-tester can see why (rate-limit/HTTP 429, gate offline, …)
            # before re-raising for the lock to surface.
            _LOGGER.debug(
                "Open access id=%s first attempt failed, retrying after JWT "
                "refresh: %s",
                access.access_id,
                first_err,
            )
            await self.refresh_jwt()
            try:
                body = await self._post_form(PATH_ACCESS_OPEN, form, jwt=self.jwt)
            except IntratoneApiError as err:
                _LOGGER.warning(
                    "Open access id=%s failed (HTTP %s): %s",
                    access.access_id,
                    err.status,
                    err.body if err.body is not None else err,
                )
                raise

        ok = _error_flag_ok(body.get("error"))
        if ok:
            _LOGGER.debug("Access id=%s opened", access.access_id)
        else:
            # Full server response so beta-testers can report exactly why an
            # open was refused (wrong field, account not provisioned, …).
            _LOGGER.warning(
                "Open access id=%s refused by server: %s", access.access_id, body
            )
        return ok

    async def mobipass_activate(self) -> None:
        """POST /api/mobipass/activate — request the CléMobil transfer code by SMS.

        Empty body; the device/phone is identified by the JWT. Raises
        `IntratoneMobipassError` when the server refuses (e.g. Mobipass not
        available for the account).
        """
        await self._mobipass_post(PATH_MOBIPASS_ACTIVATE, {})

    async def mobipass_verify(self, otp: str) -> None:
        """POST /api/mobipass/otp/verify — complete the transfer with the SMS code.

        On success the CléMobil key now belongs to this device (and is revoked
        on the user's phone). Raises `IntratoneMobipassError` (code
        `MOBIPASS_OTP_INVALID`) on a wrong/expired code.
        """
        await self._mobipass_post(PATH_MOBIPASS_VERIFY, {"otp": otp})

    async def _mobipass_post(self, path: str, form: dict[str, str]) -> None:
        """POST a Mobipass endpoint and raise on any server-signalled failure.

        Mobipass rejections arrive either as the shared `state:error` envelope
        or as a `state:ok` body with `error != 0`; both carry a `MOBIPASS_*`
        `code` (surfaced as `IntratoneMobipassError`). The one non-error `code`
        is `MOBIPASS_SMS_SENT` (the transfer code was already sent, e.g. on a
        retried activate): the app treats it as "proceed to code entry", so we
        return normally instead of raising. A first *non-Mobipass* failure is
        treated as an expired JWT and retried once after a refresh, mirroring
        `open_access`.
        """
        if not self.jwt:
            raise IntratoneAuthError("No JWT available")

        async def _attempt() -> None:
            try:
                body = await self._post_form(path, form, jwt=self.jwt)
            except IntratoneApiError as err:
                err_body = err.body if isinstance(err.body, dict) else None
                code = str(err_body.get("code") or "") if err_body else ""
                if code == MOBIPASS_CODE_SMS_SENT:
                    _LOGGER.debug("Mobipass %s: transfer code already sent", path)
                    return
                if code.startswith("MOBIPASS"):
                    raise _mobipass_error(err_body, err.status) from err
                raise  # non-Mobipass (likely 401) — let the caller retry
            # Log the raw response including success — `_post_form` only logs on
            # a `state:error` envelope, so without this the "it worked" path
            # (SMS sent / transfer completed) is invisible in a tester's log.
            _LOGGER.debug("Mobipass %s response: %s", path, body)
            err_flag = body.get("error")
            if err_flag is not None and not _error_flag_ok(err_flag):
                if str(body.get("code") or "") == MOBIPASS_CODE_SMS_SENT:
                    _LOGGER.debug("Mobipass %s: transfer code already sent", path)
                    return
                raise _mobipass_error(body, None)

        try:
            await _attempt()
        except (IntratoneMobipassError, IntratoneConnectionError):
            raise
        except IntratoneApiError as first_err:
            _LOGGER.debug(
                "Mobipass %s first attempt failed, retrying after JWT refresh: %s",
                path,
                first_err,
            )
            await self.refresh_jwt()
            await _attempt()

    async def refresh_jwt(self) -> None:
        """Refresh the JWT and persist it via the credentials Store.

        Concurrent callers (e.g. several locks hitting a 401 at once) share a
        single in-flight refresh instead of each re-running `auth/device` —
        the server rate-limits parallel auth storms (issue #39).
        """
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.get_running_loop().create_task(
                self._refresh_jwt_once()
            )
        await asyncio.shield(self._refresh_task)

    async def _refresh_jwt_once(self) -> None:
        data = await self.authenticate_device()
        new_jwt = data.get("jwt")
        if not new_jwt:
            raise IntratoneAuthError("Refresh succeeded but no JWT in response")

        await self._store.async_update(jwt=new_jwt)
        if data.get("id"):
            new_id = str(data["id"])
            if new_id != self._entry.data.get(CONF_NUMERIC_ID):
                self._hass.config_entries.async_update_entry(
                    self._entry,
                    data={**self._entry.data, CONF_NUMERIC_ID: new_id},
                )
        _LOGGER.debug("Intratone JWT refreshed")

    def async_start_jwt_refresh(
        self, on_refresh: Callable[[], None] | None = None
    ) -> None:
        """Schedule periodic JWT refresh.

        `on_refresh` (optional) is invoked after each successful refresh — used
        to re-evaluate the CléMobil/Mobipass state (now up to date on
        `self.mobipass_state`) without an extra network call.
        """

        async def _tick(_now) -> None:
            try:
                await self.refresh_jwt()
            except IntratoneAuthError as err:
                # Definitive: the server rejected every candidate — the
                # credentials are revoked. Ask the user to re-pair instead of
                # silently retrying forever (the FCM `unregister` push, the
                # only other reauth trigger, may be dead too).
                _LOGGER.warning(
                    "JWT refresh rejected by server, starting reauth: %s", err
                )
                self._entry.async_start_reauth(self._hass)
                return
            except Exception as err:  # noqa: BLE001
                # Transient (network, HTTP 5xx…): warn and retry next tick.
                _LOGGER.warning("JWT refresh failed: %s", err)
                return
            if on_refresh is not None:
                try:
                    on_refresh()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("post-refresh hook failed", exc_info=True)

        self._refresh_unsub = async_track_time_interval(
            self._hass, _tick, timedelta(hours=JWT_REFRESH_INTERVAL_HOURS)
        )

    def async_stop(self) -> None:
        """Cancel scheduled refresh and any in-flight JWT refresh."""
        if self._refresh_unsub is not None:
            self._refresh_unsub()
            self._refresh_unsub = None
        if self._refresh_task is not None and not self._refresh_task.done():
            # An orphaned refresh would otherwise finish against the
            # torn-down entry (Store write / async_update_entry mid-unload).
            self._refresh_task.cancel()
        self._refresh_task = None


def _parse_accesses(body: dict[str, Any]) -> list[IntratoneAccess]:
    """Extract `IntratoneAccess` entries from a `GET /api/access` response.

    Envelope confirmed as `data.list` (Android `AccessResponse.AccessData` and
    iOS disassembly); we keep a couple of fallbacks defensively. Per-item keys
    confirmed on both platforms: `id`, `residence`, `name`, `phonenumber`,
    `openmode` (lowercase). Only accesses whose primary mode is API-openable
    (`data`/`ble`) are returned.
    """
    data = body.get("data")
    items: Any = None
    if isinstance(data, dict):
        items = data.get("list") or data.get("accesses") or data.get("access")
    elif isinstance(data, list):
        items = data
    if items is None:
        items = body.get("list")
    if not isinstance(items, list):
        return []

    accesses: list[IntratoneAccess] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        access_id = str(item.get("id") or item.get("access_id") or "")
        phonenumber = str(
            item.get("phonenumber") or item.get("phone_number") or item.get("phone") or ""
        )
        name = str(item.get("name") or "")
        residence = str(item.get("residence") or item.get("residence_name") or "")

        # The list-item JSON key is `openmode` (lowercase, singular) — confirmed
        # by disassembling the iOS AccessItem decoder. Accept the camelCase
        # struct-field spellings too, and tolerate either a single string or a
        # list of modes.
        raw_modes = (
            item.get("openmode")
            or item.get("openModes")
            or item.get("openmodes")
            or []
        )
        if isinstance(raw_modes, str):
            raw_modes = [raw_modes]
        modes = [str(m).lower() for m in raw_modes if str(m).strip()]

        # The app dispatches on the *first* mode: `data`/`ble` open via the REST
        # API, `clemobil` opens by placing a GSM phone call (which HA can't do).
        # Only expose accesses whose primary mode we can actually open.
        primary = modes[0] if modes else None
        if primary not in API_OPENABLE_MODES or not access_id:
            # Logged so a beta-tester seeing "0 locks" can tell whether their
            # accesses are clemobil (phone-call only), unknown, or id-less.
            _LOGGER.debug(
                "Skipping access id=%s name=%r: primary mode=%r not API-openable "
                "(openable=%s)",
                access_id or "?",
                name,
                primary,
                API_OPENABLE_MODES,
            )
            continue

        accesses.append(
            IntratoneAccess(
                access_id=access_id,
                phonenumber=phonenumber,
                name=name,
                residence=residence,
                openmode=primary,
            )
        )
    return accesses


async def register_with_invite(
    session: aiohttp.ClientSession,
    *,
    device_id: str,
    fcm_token: str,
    code: str,
    codepass: str,
) -> dict[str, Any]:
    """POST /api/auth/registercodes — invite-code device registration.

    Returns the `data` block: {id, tel, ...}. Used during config_flow.
    """
    form = {
        "app_id": APP_ID,
        "app_token": APP_TOKEN,
        "code": code,
        "codepass": codepass,
        "os": DEVICE_OS,
        "osv": DEVICE_OSV,
        "model": DEVICE_MODEL,
        "manufacturer": DEVICE_MANUFACTURER,
        "device_id": device_id,
        "description": DEVICE_DESCRIPTION,
        "appversion": APP_VERSION,
        "id_fcm": fcm_token,
        "id_wonderpush": "",
        "pushkit_id": "",
        "bundleid": DEVICE_BUNDLE_ID,
        "device_country": "FRA",
        "device_language": "fr",
        "carrier_name": "Orange",
        "carrier_countrycode": "FR",
        "carrier_countryiso": "fr",
        "carrier_networkcode": "20801",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": APP_USER_AGENT,
    }
    try:
        async with session.post(
            API_BASE + "api/auth/registercodes",
            data=form,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError as err:
                raise IntratoneApiError(f"Non-JSON response: {err}") from err
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise IntratoneConnectionError(
            f"Network error calling registercodes: {err}"
        ) from err

    if not isinstance(body, dict) or body.get("state") == "error":
        msg = body.get("message") or body.get("code") if isinstance(body, dict) else body
        raise IntratoneAuthError(f"registercodes rejected: {msg}")

    data = body.get("data") or {}
    if not data.get("id") or not data.get("tel"):
        # Redact before this lands in a WARNING log — a partial body can
        # still carry the tel / device credentials.
        raise IntratoneAuthError(
            f"registercodes incomplete response: {_redact_auth_data(data)}"
        )
    return data


async def verify_user(
    session: aiohttp.ClientSession,
    *,
    tel: str,
    indicatif: str,
) -> dict[str, Any]:
    """POST /api/auth/verify — the app's first onboarding call (issue #61).

    The official app hits this the moment a phone number is entered, *before*
    `register`. Body is just `tel` + `tel_indicatif` (no JWT, no device id). The
    `data` block carries account-level flags — `compatible`, `openingaccess`,
    `inuse`, `total` — i.e. whether the account is CléMobil/Mobipass-eligible and
    whether the key is currently held by a device. Mirrors `AuthApi.verifyUser`.

    Returns the `data` block (empty on any failure — this call is best-effort:
    we replicate the app's sequence and surface the account flags for diagnosis,
    but a failure here must not block onboarding).
    """
    form = {"tel": tel, "tel_indicatif": indicatif}
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": APP_USER_AGENT,
    }
    try:
        async with session.post(
            API_BASE + "api/auth/verify",
            data=form,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            body = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        # ValueError covers a non-JSON/empty body (e.g. an HTML 5xx page).
        return {}
    if not isinstance(body, dict):
        return {}
    return body.get("data") or {}


async def register_phone_for_sms(
    session: aiohttp.ClientSession,
    *,
    device_id: str,
    fcm_token: str,
    tel: str,
    indicatif: str,
) -> dict[str, Any]:
    """POST /api/auth/register — phone-based onboarding (triggers SMS).

    Mirrors `AuthApi.registerDevice` in `com.cogelec.notificationpush`. The
    response carries the server-assigned numeric account id under `data.id`.
    """
    form = {
        "tel": tel,
        "tel_indicatif": indicatif,
        "os": DEVICE_OS,
        "osv": DEVICE_OSV,
        "model": DEVICE_MODEL,
        "manufacturer": DEVICE_MANUFACTURER,
        "device_id": device_id,
        "description": DEVICE_DESCRIPTION,
        "appversion": APP_VERSION,
        "id_fcm": fcm_token,
        "id_wonderpush": "",
        "bundleid": DEVICE_BUNDLE_ID,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": APP_USER_AGENT,
    }
    try:
        async with session.post(
            API_BASE + "api/auth/register",
            data=form,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError as err:
                raise IntratoneApiError(f"Non-JSON response: {err}") from err
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise IntratoneConnectionError(f"Network error calling register: {err}") from err

    if not isinstance(body, dict) or body.get("state") == "error":
        msg = (
            body.get("message") or body.get("code")
            if isinstance(body, dict)
            else body
        )
        raise IntratoneAuthError(f"register rejected: {msg}")

    return body.get("data") or {}


async def validate_sms_code(
    session: aiohttp.ClientSession,
    *,
    tel: str,
    indicatif: str,
    device_id: str,
    code: str,
) -> None:
    """POST /api/auth/validate — confirm the 4-digit SMS code.

    Raises `IntratoneAuthError` if the code is wrong / expired. On success
    the caller proceeds to `authenticate_for_invite` (a.k.a. /api/auth/device)
    to mint a JWT.
    """
    form = {
        "tel": tel,
        "tel_indicatif": indicatif,
        "device_id": device_id,
        "code": code,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": APP_USER_AGENT,
    }
    try:
        async with session.post(
            API_BASE + "api/auth/validate",
            data=form,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError as err:
                raise IntratoneApiError(f"Non-JSON response: {err}") from err
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise IntratoneConnectionError(f"Network error calling validate: {err}") from err

    if not isinstance(body, dict) or body.get("state") == "error":
        msg = (
            body.get("message") or body.get("code")
            if isinstance(body, dict)
            else body
        )
        raise IntratoneAuthError(f"validate rejected: {msg}")


async def authenticate_for_invite(
    session: aiohttp.ClientSession,
    *,
    tel: str,
    device_id: str,
    indicatif: str = DEFAULT_INDICATIF,
) -> dict[str, Any]:
    """One-shot auth used by config_flow (no entry yet)."""
    candidates = _tel_candidates(tel, indicatif)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": APP_USER_AGENT,
    }
    last: dict[str, Any] = {}
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            async with session.post(
                API_BASE + "api/auth/device",
                data={
                    "app_id": APP_ID,
                    "app_token": APP_TOKEN,
                    "tel": candidate,
                    "device_id": device_id,
                    "appversion": APP_VERSION,
                },
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                last = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            # Transport failure or non-JSON body — try the next candidate.
            last_error = err
            continue

        data = (last.get("data") or {}) if isinstance(last, dict) else {}
        if data.get("jwt"):
            return data
        await asyncio.sleep(0)

    if not last and last_error is not None:
        raise IntratoneApiError(f"auth/device failed: {last_error}") from last_error

    # Mask the `data` block before it lands in a WARNING log: an
    # ok-without-jwt body still carries the tel and device_id.
    redacted = dict(last) if isinstance(last, dict) else {"body": last}
    if isinstance(redacted.get("data"), dict):
        redacted["data"] = _redact_auth_data(redacted["data"])
    raise IntratoneAuthError(f"auth/device returned no JWT: {redacted}")
