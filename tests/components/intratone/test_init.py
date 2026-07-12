"""End-to-end test: load entry → fake FCM push → event fires → button → API call."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.intratone.const import (
    API_BASE,
    CONF_VIDEO_ENABLED,
    DOMAIN,
    PATH_ACCESS_LIST,
)


@pytest.fixture
def aiomock():
    with aioresponses() as m:
        yield m


async def test_mobipass_repair_created_when_transfer_needed(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    """auth/device reporting compatible-but-not-here raises the transfer repair."""
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={
            "state": "ok",
            "data": {
                "jwt": "j",
                "id": "3844428",
                "mobipass_compatible": "1",
                "mobipass": "0",
            },
        },
        repeat=True,
    )
    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        payload={"state": "ok", "data": {"list": []}},
        repeat=True,
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"mobipass_transfer_{mock_entry.entry_id}"
    )
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": mock_entry.entry_id}


async def test_mobipass_repair_absent_when_key_held_here(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    """When the key is already active on this device, no repair is raised."""
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={
            "state": "ok",
            "data": {
                "jwt": "j",
                "id": "3844428",
                "mobipass_compatible": "1",
                "mobipass": "1",
            },
        },
        repeat=True,
    )
    aiomock.get(
        f"{API_BASE}{PATH_ACCESS_LIST}",
        payload={"state": "ok", "data": {"list": []}},
        repeat=True,
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"mobipass_transfer_{mock_entry.entry_id}"
        )
        is None
    )


async def test_fcm_token_stale_repair_created_on_rotation(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    """A live FCM token that no longer matches the registered one raises a repair."""
    from unittest.mock import AsyncMock

    # Store holds "fake-fcm-token" (migrated from entry.data); simulate Google
    # rotating the token so checkin_or_register returns a different value.
    mock_fcm_client.instance.checkin_or_register = AsyncMock(
        return_value="rotated-token"
    )
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"fcm_token_stale_{mock_entry.entry_id}"
    )
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": mock_entry.entry_id}


async def test_no_fcm_token_stale_repair_when_token_matches(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    """The default token matches the stored one → no push-token repair."""
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"fcm_token_stale_{mock_entry.entry_id}"
        )
        is None
    )


async def test_full_setup_push_open_door(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    mock_entry.add_to_hass(hass)
    # Camera + backlight only exist with the video option on.
    hass.config_entries.async_update_entry(
        mock_entry, options={CONF_VIDEO_ENABLED: True}
    )

    # Allow JWT refresh tick on first setup if it happens.
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    # Door open call.
    aiomock.post(
        f"{API_BASE}api/calls/300705065/answer",
        payload={"error": 0, "state": "ok"},
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    event_eid = registry.async_get_entity_id(
        "event", DOMAIN, f"{mock_entry.entry_id}_doorbell"
    )
    lock_eid = registry.async_get_entity_id(
        "lock", DOMAIN, f"{mock_entry.entry_id}_door_lock"
    )
    camera_eid = registry.async_get_entity_id(
        "camera", DOMAIN, f"{mock_entry.entry_id}_camera"
    )
    button_eid = registry.async_get_entity_id(
        "button", DOMAIN, f"{mock_entry.entry_id}_go2rtc_test"
    )
    assert event_eid and lock_eid and camera_eid and button_eid

    # Inject a fake FCM push by calling the coordinator directly.
    runtime = mock_entry.runtime_data
    await runtime.coordinator.async_handle_push(
        {
            "call_id": "300705065",
            "message": "PORTE RUE",
            "NBPORTE": "1",
            "LOGIN_TO_CALL": "2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36",
            "LOGIN": "cogelecTest",
            "PASS": "CogeleC",
            "ip_adress": "178.32.84.135",
        }
    )
    await hass.async_block_till_done()

    event_state = hass.states.get(event_eid)
    assert event_state.attributes.get("event_type") == "ring"
    assert event_state.attributes.get("door_name") == "PORTE RUE"
    # NBPORTE rides along so user automations can branch on which door rang.
    assert event_state.attributes.get("door_number") == "1"

    # Unlock the door via the lock entity.
    await hass.services.async_call(
        "lock",
        "unlock",
        {"entity_id": lock_eid},
        blocking=True,
    )

    answer_url = f"{API_BASE}api/calls/300705065/answer"
    matching_calls = [
        call for key, calls in aiomock.requests.items()
        if str(key[1]) == answer_url
        for call in calls
    ]
    assert len(matching_calls) >= 1

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    mock_fcm_client.instance.stop.assert_awaited()


async def test_legacy_creds_in_entry_data_migrate_to_store(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    """Pre-Store installs kept JWT / FCM creds in `entry.data`. On the first
    setup after upgrade, those fields move into the per-account Store and
    are stripped from the config entry."""
    from custom_components.intratone.const import (
        CONF_FCM_CREDS,
        CONF_FCM_TOKEN,
        CONF_JWT,
    )

    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )

    # mock_entry_data (from conftest) carries the legacy fields.
    assert CONF_JWT in mock_entry.data
    assert CONF_FCM_CREDS in mock_entry.data

    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    # After setup: entry.data is clean, Store holds the rotated values.
    cleaned = hass.config_entries.async_get_entry(mock_entry.entry_id).data
    for key in (CONF_JWT, CONF_FCM_TOKEN, CONF_FCM_CREDS):
        assert key not in cleaned, key

    runtime = mock_entry.runtime_data
    assert runtime.store.jwt == "fake.jwt.token"
    assert runtime.store.fcm_token == "fake-fcm-token"
    assert runtime.store.fcm_creds == {"gcm": {"android_id": 1, "security_token": 2}}


async def test_remove_entry_deletes_credentials_store(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock, hass_storage
) -> None:
    """Removing the integration must also delete the per-account credentials
    Store (`.storage/intratone.<key>.creds` holds a live JWT + FCM creds)."""
    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    storage_key = f"intratone.{mock_entry.unique_id}.creds"
    assert storage_key in hass_storage

    await hass.config_entries.async_remove(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert storage_key not in hass_storage


async def test_simulate_ring_service(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    from custom_components.intratone.const import DOMAIN

    mock_entry.add_to_hass(hass)
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        "simulate_ring",
        {"door_name": "PORTE COUR", "call_id": "sim-test"},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = mock_entry.runtime_data.coordinator.data
    assert state is not None
    assert state.call_id == "sim-test"
    assert state.door_name == "PORTE COUR"


async def test_go2rtc_repair_issue_raised_and_cleared(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    """A failed push to go2rtc raises the non-fixable relay repair with the
    configured URL; the next success clears it."""
    from custom_components.intratone import report_relay_status
    from custom_components.intratone.const import CONF_GO2RTC_URL

    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry,
        options={CONF_VIDEO_ENABLED: True, CONF_GO2RTC_URL: "rtsp://10.0.0.9:8554"},
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    issue_id = f"go2rtc_unreachable_{mock_entry.entry_id}"

    report_relay_status(hass, mock_entry, False)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert not issue.is_fixable
    assert issue.translation_placeholders == {"url": "rtsp://10.0.0.9:8554"}

    report_relay_status(hass, mock_entry, True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_camera_and_backlight_absent_without_video_option(
    hass, mock_entry, mock_fcm_client, mock_call_manager, aiomock
) -> None:
    """With video off (the default), the camera, backlight and go2rtc-test
    entities are not created, and stale registry entries left over from a
    previous video-enabled run are removed. The other entities are
    unaffected."""
    mock_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "camera", DOMAIN, f"{mock_entry.entry_id}_camera", config_entry=mock_entry
    )
    registry.async_get_or_create(
        "switch", DOMAIN, f"{mock_entry.entry_id}_backlight", config_entry=mock_entry
    )
    registry.async_get_or_create(
        "button",
        DOMAIN,
        f"{mock_entry.entry_id}_go2rtc_test",
        config_entry=mock_entry,
    )
    aiomock.post(
        f"{API_BASE}api/auth/device",
        payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "3844428"}},
        repeat=True,
    )

    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        registry.async_get_entity_id(
            "camera", DOMAIN, f"{mock_entry.entry_id}_camera"
        )
        is None
    )
    assert (
        registry.async_get_entity_id(
            "switch", DOMAIN, f"{mock_entry.entry_id}_backlight"
        )
        is None
    )
    assert (
        registry.async_get_entity_id(
            "button", DOMAIN, f"{mock_entry.entry_id}_go2rtc_test"
        )
        is None
    )
    # The doorbell event entity is unaffected by the video option.
    assert registry.async_get_entity_id(
        "event", DOMAIN, f"{mock_entry.entry_id}_doorbell"
    )
