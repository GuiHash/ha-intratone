"""Config flow for the Intratone integration."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_DEVICE_ID,
    CONF_GO2RTC_URL,
    CONF_INDICATIF,
    CONF_INVITE_CODE,
    CONF_NUMERIC_ID,
    CONF_REGISTER_METHOD,
    CONF_TEL,
    CONF_VIDEO_ENABLED,
    DEFAULT_GO2RTC_URL,
    DEFAULT_INDICATIF,
    DOMAIN,
    INVITE_RE,
    MOBIPASS_ERRORS,
    REGISTER_METHOD_INVITE,
    REGISTER_METHOD_SMS,
)
from .fcm_listener import fcm_register_standalone
from .rest_api import (
    IntratoneApiError,
    IntratoneAuthError,
    IntratoneMobipassError,
    authenticate_for_invite,
    register_phone_for_sms,
    register_with_invite,
    validate_sms_code,
    verify_user,
)
from .store import IntratoneCredentialsStore

_LOGGER = logging.getLogger(__name__)

# Shared with repairs.py (FCM re-pair flow).
USER_SCHEMA = vol.Schema({vol.Required(CONF_INVITE_CODE): str})

PHONE_SCHEMA = vol.Schema(
    {
        vol.Required("phone"): str,
        vol.Required("indicatif", default=DEFAULT_INDICATIF): str,
    }
)
SMS_SCHEMA = vol.Schema({vol.Required("code"): str})
# Shared with repairs.py (CléMobil transfer repair flow).
MOBIPASS_OTP_SCHEMA = vol.Schema({vol.Required("code"): str})
VIDEO_SCHEMA = vol.Schema({vol.Required(CONF_VIDEO_ENABLED, default=False): bool})


def _normalize_phone(raw: str, indicatif: str) -> str:
    """Strip whitespace, dashes, country prefix and national 0 from a number."""
    s = raw.strip().replace(" ", "").replace("-", "").replace(".", "")
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("00"):
        s = s[2:]
    if s.startswith(indicatif):
        s = s[len(indicatif):]
    if s.startswith("0"):
        s = s[1:]
    return s


class IntratoneOptionsFlowHandler(OptionsFlow):
    """Handle Intratone options (video, go2rtc URL)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_VIDEO_ENABLED,
                    default=current.get(CONF_VIDEO_ENABLED, False),
                ): bool,
                vol.Optional(
                    CONF_GO2RTC_URL,
                    default=current.get(CONF_GO2RTC_URL, DEFAULT_GO2RTC_URL),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class IntratoneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Intratone pairing flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> IntratoneOptionsFlowHandler:
        return IntratoneOptionsFlowHandler()

    def __init__(self) -> None:
        self._reauth_entry = None
        # Carries SMS-flow state between async_step_phone and async_step_sms.
        self._pending_sms: dict[str, Any] = {}
        # Entry title/data staged by a successful pairing, consumed by
        # async_step_video which creates the entry with the chosen options.
        self._pending_entry: dict[str, Any] | None = None

    async def async_step_user(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial pairing — let the user pick SMS or installer invite code."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["phone", "invite"],
        )

    async def async_step_invite(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair with an installer-issued invitation code (`XXXXXX-XXXX`)."""
        return await self._async_invite_step(user_input, step_id="invite")

    async def async_step_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """SMS flow — step 1: phone + indicatif, triggers an SMS."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # Normalize the indicatif FIRST: a raw "+33" would never match
            # inside _normalize_phone (the phone's own "+" is stripped there),
            # leaving the country prefix glued to the stored number.
            indicatif = user_input["indicatif"].strip().lstrip("+")
            phone = _normalize_phone(user_input["phone"], indicatif)
            if not phone or not indicatif:
                errors["phone"] = "invalid_phone"
            else:
                # 16-char hex mimics Android's Settings.Secure.ANDROID_ID; some
                # Intratone residences gate CléMobil/Mobipass eligibility on this
                # device_id shape (issue #61).
                device_id = uuid.uuid4().hex[:16]
                try:
                    session = async_get_clientsession(self.hass)
                    # Mirror the official app, which calls api/auth/verify first
                    # (before register). Best-effort: it surfaces the account-level
                    # CléMobil flags (compatible/openingaccess/inuse/total) for
                    # diagnosis and matches the app's onboarding sequence, but must
                    # not block pairing if it fails. See issue #61.
                    verify_data = await verify_user(
                        session, tel=phone, indicatif=indicatif
                    )
                    _LOGGER.debug("auth/verify account flags: %s", verify_data)
                    fcm_token, fcm_creds = await fcm_register_standalone(None)
                    await register_phone_for_sms(
                        session,
                        device_id=device_id,
                        fcm_token=fcm_token,
                        tel=phone,
                        indicatif=indicatif,
                    )
                except IntratoneAuthError as err:
                    _LOGGER.warning("register rejected: %s", err)
                    errors["base"] = "sms_failed"
                except IntratoneApiError as err:
                    _LOGGER.warning("register API error: %s", err)
                    errors["base"] = "sms_failed"
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception("Unexpected register error")
                    if "fcm" in str(err).lower() or "firebase" in str(err).lower():
                        errors["base"] = "fcm_failed"
                    else:
                        errors["base"] = "unknown"
                else:
                    self._pending_sms = {
                        "device_id": device_id,
                        "fcm_token": fcm_token,
                        "fcm_creds": fcm_creds,
                        "phone": phone,
                        "indicatif": indicatif,
                    }
                    return await self.async_step_sms()

        return self.async_show_form(
            step_id="phone", data_schema=PHONE_SCHEMA, errors=errors
        )

    async def async_step_sms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """SMS flow — step 2: enter the 4-digit code → mint a JWT."""
        errors: dict[str, str] = {}
        if user_input is not None:
            pending = self._pending_sms
            session = async_get_clientsession(self.hass)
            try:
                await validate_sms_code(
                    session,
                    tel=pending["phone"],
                    indicatif=pending["indicatif"],
                    device_id=pending["device_id"],
                    code=user_input["code"].strip(),
                )
                auth_data = await authenticate_for_invite(
                    session,
                    tel=pending["phone"],
                    device_id=pending["device_id"],
                    indicatif=pending["indicatif"],
                )
            except IntratoneAuthError as err:
                _LOGGER.warning("validate/auth rejected: %s", err)
                errors["base"] = "invalid_sms_code"
            except IntratoneApiError as err:
                _LOGGER.warning("validate API error: %s", err)
                errors["base"] = "auth_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected validate/auth error")
                errors["base"] = "unknown"
            else:
                numeric_id = str(auth_data["id"])
                await self.async_set_unique_id(numeric_id)
                self._abort_if_unique_id_configured()

                store = IntratoneCredentialsStore(self.hass, numeric_id)
                await store.async_load()
                await store.async_update(
                    jwt=auth_data["jwt"],
                    fcm_token=pending["fcm_token"],
                    fcm_creds=pending["fcm_creds"],
                )

                tel_display = f"{pending['indicatif']}{pending['phone']}"
                self._pending_entry = {
                    "title": f"Intratone (+{tel_display})",
                    "data": {
                        CONF_DEVICE_ID: pending["device_id"],
                        CONF_NUMERIC_ID: numeric_id,
                        CONF_TEL: pending["phone"],
                        CONF_INDICATIF: pending["indicatif"],
                        CONF_REGISTER_METHOD: REGISTER_METHOD_SMS,
                    },
                }
                return await self.async_step_video()

        return self.async_show_form(
            step_id="sms", data_schema=SMS_SCHEMA, errors=errors
        )

    async def async_step_video(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Final pairing step — opt into VP8 video.

        Video needs a standalone go2rtc instance, so it defaults to off.
        Without it the camera and backlight entities are not created (audio
        and door opening keep working). Changeable later via the options.
        """
        if user_input is not None:
            pending = self._pending_entry
            return self.async_create_entry(
                title=pending["title"],
                data=pending["data"],
                options={CONF_VIDEO_ENABLED: user_input[CONF_VIDEO_ENABLED]},
            )
        return self.async_show_form(step_id="video", data_schema=VIDEO_SCHEMA)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """CléMobil transfer — step 1: warn, then trigger the SMS transfer code.

        Since ~June 2026 the remote-open key ("CléMobil" / Mobipass) can live on
        only one device per phone number (issue #61). Confirming here sends a
        one-time code by SMS and, once entered, moves the key onto Home
        Assistant — revoking it in the official app on the user's phone.
        """
        entry = self._get_reconfigure_entry()
        api = getattr(entry, "runtime_data", None) and entry.runtime_data.api
        if api is None:
            return self.async_abort(reason="not_loaded")

        # Only offer the transfer when the account is eligible and the key still
        # lives elsewhere. Refresh the flags first (best-effort — a transient
        # failure falls through to the form). CléMobil eligibility is only granted
        # to invite-code registrations; an SMS-paired device is never provisioned
        # for it (issue #61), so steer those users to re-pair with an invitation
        # code rather than let the transfer fail with MOBIPASS_NOT_AVAILABLE.
        if user_input is None:
            try:
                await api.authenticate_device()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("mobipass reconfigure precheck failed", exc_info=True)
            state = api.mobipass_state
            if state is not None and not state.needs_transfer:
                if state.mobipass:
                    return self.async_abort(reason="mobipass_already_active")
                method = entry.data.get(CONF_REGISTER_METHOD)
                if method == REGISTER_METHOD_SMS:
                    # We know it was paired by SMS → assert the cause + fix.
                    return self.async_abort(reason="mobipass_use_invite_code")
                if method == REGISTER_METHOD_INVITE:
                    # Invite-paired yet not eligible → genuinely not available.
                    return self.async_abort(reason="mobipass_not_eligible")
                # Legacy entry: method wasn't recorded, so we can't be sure.
                return self.async_abort(reason="mobipass_unknown_method")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await api.mobipass_activate()
            except IntratoneMobipassError as err:
                _LOGGER.warning("mobipass activate refused: %s", err)
                errors["base"] = MOBIPASS_ERRORS.get(err.code, "mobipass_failed")
            except (IntratoneAuthError, IntratoneApiError) as err:
                _LOGGER.warning("mobipass activate failed: %s", err)
                errors["base"] = "mobipass_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected mobipass activate error")
                errors["base"] = "unknown"
            else:
                return await self.async_step_mobipass_otp()

        return self.async_show_form(
            step_id="reconfigure", data_schema=vol.Schema({}), errors=errors
        )

    async def async_step_mobipass_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """CléMobil transfer — step 2: enter the SMS code to complete the transfer."""
        entry = self._get_reconfigure_entry()
        api = getattr(entry, "runtime_data", None) and entry.runtime_data.api
        if api is None:
            return self.async_abort(reason="not_loaded")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await api.mobipass_verify(user_input["code"].strip())
            except IntratoneMobipassError as err:
                _LOGGER.warning("mobipass verify refused: %s", err)
                errors["base"] = MOBIPASS_ERRORS.get(err.code, "mobipass_failed")
            except (IntratoneAuthError, IntratoneApiError) as err:
                _LOGGER.warning("mobipass verify failed: %s", err)
                errors["base"] = "mobipass_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected mobipass verify error")
                errors["base"] = "unknown"
            else:
                # Reload so lock.py re-runs list_access() and the freshly
                # transferred accesses appear as lock entities.
                return self.async_update_reload_and_abort(
                    entry, reason="mobipass_transfer_successful"
                )

        return self.async_show_form(
            step_id="mobipass_otp", data_schema=MOBIPASS_OTP_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, _entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Re-pair when credentials become irrecoverable.

        Tries `api/auth/device` silently with the stored phone + device_id
        first — the Cogelec app does the same to renew its JWT without
        bothering the user. Falls back to the invite-code form only if the
        silent refresh is rejected (phone unbound, device wiped server-side,
        etc.).
        """
        self._reauth_entry = self._get_reauth_entry()
        new_data = await self._async_try_silent_reauth()
        if new_data is not None:
            return self.async_update_reload_and_abort(
                self._reauth_entry, data=new_data
            )
        return await self.async_step_reauth_confirm()

    async def _async_try_silent_reauth(self) -> dict[str, Any] | None:
        """Best-effort JWT refresh using already-persisted credentials.

        Writes the fresh JWT to the credentials Store and returns the
        updated entry data on success, or None on any failure (caller
        falls back to the invite-code form).
        """
        entry = self._reauth_entry
        if entry is None:
            return None
        tel = entry.data.get(CONF_TEL)
        device_id = entry.data.get(CONF_DEVICE_ID)
        if not tel or not device_id:
            return None
        try:
            session = async_get_clientsession(self.hass)
            data = await authenticate_for_invite(
                session,
                tel=tel,
                device_id=device_id,
                indicatif=entry.data.get(CONF_INDICATIF, DEFAULT_INDICATIF),
            )
        except (IntratoneAuthError, IntratoneApiError) as err:
            _LOGGER.info(
                "Silent reauth rejected (%s) — prompting for invite code", err
            )
            return None
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Silent reauth crashed — prompting for invite code"
            )
            return None

        store = IntratoneCredentialsStore(
            self.hass, entry.unique_id or entry.entry_id
        )
        await store.async_load()
        await store.async_update(jwt=data["jwt"])

        new_data = dict(entry.data)
        if data.get("id"):
            new_data[CONF_NUMERIC_ID] = str(data["id"])
        return new_data

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_invite_step(user_input, step_id="reauth_confirm")

    async def _async_invite_step(
        self,
        user_input: dict[str, Any] | None,
        *,
        step_id: str,
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            match = INVITE_RE.match(user_input[CONF_INVITE_CODE])
            if not match:
                errors["base"] = "invalid_format"
            else:
                code, codepass = match.group(1), match.group(2)
                try:
                    entry_data, creds = await self._pair(code, codepass)
                except IntratoneAuthError as err:
                    _LOGGER.warning("Pairing failed: %s", err)
                    errors["base"] = "invalid_code"
                except IntratoneApiError as err:
                    _LOGGER.warning("API error during pairing: %s", err)
                    errors["base"] = "auth_failed"
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception("Unexpected pairing error")
                    if "fcm" in str(err).lower() or "firebase" in str(err).lower():
                        errors["base"] = "fcm_failed"
                    else:
                        errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(entry_data[CONF_NUMERIC_ID])

                    # Abort BEFORE touching the Store: `_pair()` already
                    # registered server-side, but overwriting the Store of a
                    # running account would desync its FcmListener (checked in
                    # with the old fcm_token) from the stored credentials.
                    if self._reauth_entry is not None:
                        self._abort_if_unique_id_mismatch()
                    else:
                        self._abort_if_unique_id_configured()

                    # Pre-write rotated credentials to the per-account Store
                    # so they never touch entry.data. The Store key uses the
                    # numeric_id (= unique_id) which we just set.
                    store = IntratoneCredentialsStore(
                        self.hass, entry_data[CONF_NUMERIC_ID]
                    )
                    await store.async_load()
                    await store.async_update(**creds)

                    if self._reauth_entry is not None:
                        return self.async_update_reload_and_abort(
                            self._reauth_entry, data=entry_data
                        )

                    self._pending_entry = {
                        "title": f"Intratone ({entry_data[CONF_TEL]})",
                        "data": entry_data,
                    }
                    return await self.async_step_video()

        return self.async_show_form(
            step_id=step_id,
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    async def _pair(
        self, code: str, codepass: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run FCM register + Intratone registercodes + auth/device.

        Returns `(entry_data, creds)` — entry_data goes on the config entry
        (device id, phone, numeric id), creds goes into the per-account
        Store (JWT, FCM token, FCM credentials).
        """
        session = async_get_clientsession(self.hass)

        # 16-char hex mimics Android's Settings.Secure.ANDROID_ID; some Intratone
        # residences gate CléMobil/Mobipass eligibility on this device_id shape
        # (issue #61).
        device_id = (
            self._reauth_entry.data.get(CONF_DEVICE_ID)
            if self._reauth_entry is not None
            else None
        ) or uuid.uuid4().hex[:16]

        existing_creds: dict[str, Any] | None = None
        if self._reauth_entry is not None and self._reauth_entry.unique_id:
            cached = IntratoneCredentialsStore(
                self.hass, self._reauth_entry.unique_id
            )
            await cached.async_load()
            existing_creds = cached.fcm_creds

        fcm_token, fcm_creds = await fcm_register_standalone(existing_creds)

        register_data = await register_with_invite(
            session,
            device_id=device_id,
            fcm_token=fcm_token,
            code=code,
            codepass=codepass,
        )
        tel = str(register_data["tel"])
        numeric_id = str(register_data["id"])

        auth_data = await authenticate_for_invite(
            session, tel=tel, device_id=device_id
        )

        entry_data = {
            CONF_DEVICE_ID: device_id,
            CONF_NUMERIC_ID: numeric_id,
            CONF_TEL: tel,
            CONF_REGISTER_METHOD: REGISTER_METHOD_INVITE,
        }
        creds = {
            "jwt": auth_data["jwt"],
            "fcm_token": fcm_token,
            "fcm_creds": fcm_creds,
        }
        return entry_data, creds
