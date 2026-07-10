"""Constants for the Intratone integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "intratone"
MANUFACTURER: Final = "Cogelec"
MODEL: Final = "Intratone Bridge (HA)"

API_BASE: Final = "https://sip.intratone.info/"
APP_ID: Final = "app_apisip_android"
APP_TOKEN: Final = ">KompY95?oijeIKR8049?OLysIekjpceKejLAHhh"
# Sent as `appversion` on every request. Bumped 4.6.3 → 4.6.4 to match the app
# version that introduced Mobipass: the server returns MOBIPASS_NOT_AVAILABLE to
# affected accounts on this client, and it may be gating the mobipass endpoints
# on the reported app version (issue #61).
APP_VERSION: Final = "4.6.4"
DEVICE_BUNDLE_ID: Final = "com.cogelec.notificationpush"

FCM_PROJECT_ID: Final = "android-ipvideo-studio"
FCM_APP_ID: Final = "1:676502914290:android:5393f05ec7f22bd6"
FCM_API_KEY: Final = "AIzaSyB7RtCyt6LZWMruWKj7Z_9Ii7_VAIVdSKU"
FCM_SENDER_ID: Final = "676502914290"

CONF_INVITE_CODE: Final = "invite_code"
CONF_DEVICE_ID: Final = "device_id"
CONF_NUMERIC_ID: Final = "numeric_id"
CONF_TEL: Final = "tel"
CONF_JWT: Final = "jwt"
CONF_FCM_TOKEN: Final = "fcm_token"
CONF_FCM_CREDS: Final = "fcm_creds"

DEFAULT_INDICATIF: Final = "33"

JWT_REFRESH_INTERVAL_HOURS: Final = 12

CONF_VIDEO_ENABLED: Final = "video_enabled"
CONF_GO2RTC_URL: Final = "go2rtc_url"
DEFAULT_GO2RTC_URL: Final = "rtsp://127.0.0.1:8554"

# Remote door opening ("Clé mobile" / mobipass) — opening a door/gate without
# anyone ringing. Confirmed against the decompiled iOS app v4.4.10
# (`APIManager.openAccessWithCleMobil` → POST /access/open/clemobil). Despite
# its legacy name, this single endpoint is the API open path for `data`
# (mobipass, 4G) and `ble` accesses; the body carries no mode, just
# `phonenumber` + `access_id` (there is no `/access/open/data` endpoint).
# `clemobil`-mode accesses are opened by a GSM phone call instead, not this
# endpoint. See INTRATONE_API.md §4.5.
PATH_ACCESS_LIST: Final = "api/access"
PATH_ACCESS_OPEN: Final = "api/access/open/clemobil"

# Mobipass ("CléMobil") ownership transfer. Since ~June 2026 Cogelec lets only
# one device per phone number hold the remote-open key; moving it to a new
# device needs a one-time code sent by SMS (issue #61). Reverse-engineered from
# Android `com.cogelec.notificationpush` v4.6.4 (`MobipassRemoteDataSource`):
#   POST api/mobipass/activate    → triggers the SMS (empty body, JWT auth)
#   POST api/mobipass/otp/verify  → completes the transfer (form field `otp`)
PATH_MOBIPASS_ACTIVATE: Final = "api/mobipass/activate"
PATH_MOBIPASS_VERIFY: Final = "api/mobipass/otp/verify"

# `code` values the server returns on a failed Mobipass request (the Android
# `HTTPResponse.code`), mapped to user-facing errors in the config flow.
MOBIPASS_CODE_OTP_INVALID: Final = "MOBIPASS_OTP_INVALID"
MOBIPASS_CODE_BLOCKED: Final = "MOBIPASS_CODE_BLOCKED"
MOBIPASS_CODE_NOT_AVAILABLE: Final = "MOBIPASS_NOT_AVAILABLE"

# `openmode` values. The official app routes the "open" tap by the access's
# *first* mode (Android `AccessViewModel.openAccess`):
#   - `data` (mobipass, 4G) and `ble`  → REST API open (what we can do)
#   - `clemobil` (legacy 2G)           → a real GSM phone call
#     (`openAccessByPhoneUseCase` → `LaunchPhoneCall`), which Home Assistant
#     cannot place — so we don't expose those accesses.
OPENMODE_CLEMOBIL: Final = "clemobil"
OPENMODE_DATA: Final = "data"
OPENMODE_BLE: Final = "ble"
API_OPENABLE_MODES: Final = (OPENMODE_DATA, OPENMODE_BLE)
