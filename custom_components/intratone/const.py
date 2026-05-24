"""Constants for the Intratone integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "intratone"
MANUFACTURER: Final = "Cogelec"
MODEL: Final = "Intratone Bridge (HA)"

API_BASE: Final = "https://sip.intratone.info/"
APP_ID: Final = "app_apisip_android"
APP_TOKEN: Final = ">KompY95?oijeIKR8049?OLysIekjpceKejLAHhh"
APP_VERSION: Final = "4.6.3"
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
