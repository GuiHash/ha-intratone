"""Coordinator unit tests — ring sequencing and lazy-INVITE wiring.

Phase 2.5: The SIP INVITE is deferred until the user taps live view or
Unlock (matches the Cogelec app's `NotificationCallActivity → CallActivity`
flow). FCM push only fires the HomeKit doorbell event.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.intratone.coordinator import IntratoneCoordinator


@pytest.fixture
def coordinator(hass, mock_entry):
    api = MagicMock()
    api.answer_call = AsyncMock(return_value=True)
    return IntratoneCoordinator(hass, mock_entry, api)


@pytest.fixture
def coordinator_with_cm(coordinator):
    cm = MagicMock()
    cm.start_call = AsyncMock(return_value="fake-call-id")
    cm.hang_up = AsyncMock()
    cm.abort_call = AsyncMock()
    coordinator.attach_call_manager(cm)
    return coordinator, cm


_FULL_PUSH = {
    "call_id": "300705065",
    "message": "PORTE RUE",
    "LOGIN_TO_CALL": "2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36",
    "LOGIN": "cogelecTest",
    "PASS": "CogeleC",
    "ip_adress": "178.32.84.135",
}


async def test_handle_push_increments_ring_seq(coordinator) -> None:
    await coordinator.async_handle_push(
        {"call_id": "1", "message": "PORTE RUE", "LOGIN_TO_CALL": "ABC"}
    )
    assert coordinator.data is not None
    assert coordinator.data.ring_seq == 1
    assert coordinator.data.door_name == "PORTE RUE"
    assert coordinator.data.call_id == "1"

    await coordinator.async_handle_push({"call_id": "2", "message": "PORTE COUR"})
    assert coordinator.data.ring_seq == 2
    assert coordinator.data.call_id == "2"


async def test_handle_push_without_call_id_is_ignored(coordinator) -> None:
    await coordinator.async_handle_push({"message": "no call id"})
    assert coordinator.data is None


async def test_handle_push_with_call_id_but_no_message_is_ignored(
    coordinator,
) -> None:
    """Mirrors Cogelec's `detectNotificationTypeFromData`: a v1-shaped push
    is a call only when BOTH `call_id` and `message` are present."""
    await coordinator.async_handle_push({"call_id": "1"})
    assert coordinator.data is None


async def test_handle_push_type_24_alternative_format(coordinator) -> None:
    """The newer FCM shape keys the call_id on `NOTIFICATION_UUID` and
    routes via `domain_sip` instead of `ip_adress`."""
    await coordinator.async_handle_push(
        {
            "TYPE": "24",
            "NOTIFICATION_UUID": "uuid-42",
            "LOGIN_TO_CALL": "TARGET",
            "LOGIN": "u",
            "PASS": "p",
            "domain_sip": "sip.intratone.info",
        }
    )
    assert coordinator.data is not None
    assert coordinator.data.call_id == "uuid-42"
    # SIP creds were resolved against `domain_sip`, so the lazy INVITE is armed.
    assert coordinator._pending is not None
    assert coordinator._pending.target_uri == "sip:TARGET@sip.intratone.info"


async def test_open_door_with_no_call_returns_false(coordinator) -> None:
    assert await coordinator.async_open_door() is False
    coordinator.api.answer_call.assert_not_awaited()


async def test_open_door_without_call_manager_returns_false(coordinator) -> None:
    """Without a CallManager attached there's no SIP MESSAGE to send → door
    cannot open."""
    await coordinator.async_handle_push({"call_id": "42", "message": "X"})
    assert await coordinator.async_open_door() is False


async def test_push_does_not_call_answer_immediately(coordinator) -> None:
    """Lazy SIP: `/answer` is NOT called at push time anymore — only when
    the user actually taps live view or Unlock. Matches the Cogelec app
    which calls `informCallPickedUp` from `Call.State.Connected`."""
    await coordinator.async_handle_push({"call_id": "42", "message": "X"})
    coordinator.api.answer_call.assert_not_awaited()


# --- Lazy SIP / audio wiring --------------------------------------------


async def test_push_with_sip_creds_does_not_invite_immediately(
    coordinator_with_cm,
) -> None:
    """FCM push only fires the HomeKit doorbell ring. The SIP INVITE waits
    for `async_ensure_call_started` (called from camera/lock entities on
    user tap)."""
    coordinator, cm = coordinator_with_cm
    await coordinator.async_handle_push(_FULL_PUSH)
    cm.start_call.assert_not_awaited()


async def test_ensure_call_started_triggers_invite_with_pending_creds(
    coordinator_with_cm,
) -> None:
    coordinator, cm = coordinator_with_cm
    await coordinator.async_handle_push(_FULL_PUSH)

    assert await coordinator.async_ensure_call_started() is True

    cm.start_call.assert_awaited_once_with(
        target_uri="sip:2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36@178.32.84.135",
        target_host="178.32.84.135",
        sip_username="cogelecTest",
        sip_password="CogeleC",
    )
    coordinator.api.answer_call.assert_awaited_once_with("300705065")


async def test_ensure_call_started_is_idempotent(coordinator_with_cm) -> None:
    """Camera and Lock may both call ensure_call_started; only the first
    actually fires the INVITE."""
    coordinator, cm = coordinator_with_cm
    await coordinator.async_handle_push(_FULL_PUSH)
    await coordinator.async_ensure_call_started()
    await coordinator.async_ensure_call_started()
    cm.start_call.assert_awaited_once()
    coordinator.api.answer_call.assert_awaited_once()


async def test_ensure_call_started_returns_false_without_pending(
    coordinator_with_cm,
) -> None:
    coordinator, cm = coordinator_with_cm
    assert await coordinator.async_ensure_call_started() is False
    cm.start_call.assert_not_awaited()


async def test_ensure_call_started_skipped_when_push_lacks_sip_creds(
    coordinator_with_cm,
) -> None:
    coordinator, cm = coordinator_with_cm
    await coordinator.async_handle_push({"call_id": "1", "message": "X"})
    assert await coordinator.async_ensure_call_started() is False
    cm.start_call.assert_not_awaited()


async def test_set_stream_url_updates_call_state(coordinator_with_cm) -> None:
    """set_stream_url matches against the SIP call_id captured during the
    lazy INVITE, not the FCM-side call_id used for the door REST endpoint."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-abc"
    await coordinator.async_handle_push(_FULL_PUSH)
    await coordinator.async_ensure_call_started()
    assert coordinator.data.stream_url is None

    # Callback fires with the SIP call_id, which the coordinator remembered.
    coordinator.set_stream_url("sip-call-abc", "rtsp://127.0.0.1:8556/intratone")
    assert coordinator.data.stream_url == "rtsp://127.0.0.1:8556/intratone"

    coordinator.set_stream_url("sip-call-abc", None)
    assert coordinator.data.stream_url is None


async def test_set_stream_url_ignored_for_stale_sip_call_id(
    coordinator_with_cm,
) -> None:
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-current"
    await coordinator.async_handle_push(_FULL_PUSH)
    await coordinator.async_ensure_call_started()
    coordinator.set_stream_url("sip-call-stale", "rtsp://x")
    assert coordinator.data.stream_url is None


async def test_set_stream_url_sets_ready_event(coordinator_with_cm) -> None:
    """Camera entity awaits `async_wait_for_stream`; set_stream_url with a
    real URL must unblock that await."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-1"
    await coordinator.async_handle_push(_FULL_PUSH)
    await coordinator.async_ensure_call_started()
    coordinator.set_stream_url("sip-call-1", "rtsp://x:8556/intratone")

    url = await coordinator.async_wait_for_stream(timeout=0.1)
    assert url == "rtsp://x:8556/intratone"


async def test_open_door_lazy_starts_call_when_user_taps_unlock_first(
    coordinator_with_cm,
) -> None:
    """If the user taps Unlock without opening live view first, the open_door
    path itself triggers the SIP INVITE before sending the MESSAGE."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-unlock-first"
    cm.send_open_door = MagicMock(return_value=True)

    await coordinator.async_handle_push(_FULL_PUSH)
    # No prior ensure_call_started — simulating "user goes straight to unlock".

    # Stream readiness comes from the bridge callback; simulate it firing
    # shortly after start_call returns (the camera path would await this).
    import asyncio as _asyncio

    async def fire_ready_after_delay() -> None:
        await _asyncio.sleep(0.01)
        coordinator.set_stream_url("sip-call-unlock-first", "rtsp://x")

    _asyncio.create_task(fire_ready_after_delay())

    assert await coordinator.async_open_door() is True

    cm.start_call.assert_awaited_once()
    cm.send_open_door.assert_called_once_with("*")


async def test_open_door_sends_sip_message_with_payload_code(
    coordinator_with_cm,
) -> None:
    """The `opendoor:<code>` body uses the FCM `codes` field (default `*`)."""
    coordinator, cm = coordinator_with_cm
    cm.send_open_door = MagicMock(return_value=True)
    await coordinator.async_handle_push(
        {"call_id": "42", "message": "X", "codes": "5"}
    )
    assert coordinator.data.door_code == "5"
    # No SIP creds in this payload → ensure_call_started is a no-op, but
    # async_open_door still tries the SIP MESSAGE on whatever call_manager
    # state exists.
    assert await coordinator.async_open_door() is True
    cm.send_open_door.assert_called_once_with("5")


async def test_open_door_returns_false_when_sip_message_fails(
    coordinator_with_cm,
) -> None:
    """If the call has already BYE'd, send_open_door returns False."""
    coordinator, cm = coordinator_with_cm
    cm.send_open_door = MagicMock(return_value=False)
    await coordinator.async_handle_push({"call_id": "42", "message": "X"})
    assert await coordinator.async_open_door() is False


async def test_wait_for_stream_timeout_aborts_call(coordinator_with_cm) -> None:
    """When stream_ready times out, abort_call() is scheduled on the call
    manager so ffmpeg and the SIP leg are torn down immediately."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-timeout"
    await coordinator.async_handle_push(_FULL_PUSH)
    await coordinator.async_ensure_call_started()

    # stream_ready is never set → timeout fires
    url = await coordinator.async_wait_for_stream(timeout=0.01)

    assert url is None
    await asyncio.sleep(0.01)  # let the scheduled task run
    cm.abort_call.assert_awaited_once()
