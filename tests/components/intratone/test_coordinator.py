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
    cm.abort_active_call = AsyncMock()
    cm.send_backlight = MagicMock(return_value=True)
    cm.active_call_id = None
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


async def test_relay_rtsp_url_delegates_to_call_manager(coordinator_with_cm) -> None:
    coordinator, cm = coordinator_with_cm
    cm.relay_rtsp_url = "rtsp://127.0.0.1:8554/intratone"
    assert coordinator.relay_rtsp_url == "rtsp://127.0.0.1:8554/intratone"


async def test_relay_rtsp_url_none_without_call_manager(coordinator) -> None:
    assert coordinator.relay_rtsp_url is None


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
        max_duration_s=None,
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


async def test_ensure_call_started_retries_after_start_call_returns_none(
    coordinator_with_cm,
) -> None:
    """CallManager.start_call returns None on TCP connect failure/timeout.
    The pending invite must NOT be consumed: /answer stays unsent (so the
    intercom keeps ringing the user's real phone) and a later tap during
    the same ring retries the INVITE."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = None
    await coordinator.async_handle_push(_FULL_PUSH)

    assert await coordinator.async_ensure_call_started() is False
    coordinator.api.answer_call.assert_not_awaited()

    # Transient failure — the next user tap must retry the INVITE.
    cm.start_call.return_value = "sip-call-retry"
    assert await coordinator.async_ensure_call_started() is True
    assert cm.start_call.await_count == 2
    coordinator.api.answer_call.assert_awaited_once_with("300705065")


async def test_ensure_call_started_retries_after_start_call_raises(
    coordinator_with_cm,
) -> None:
    coordinator, cm = coordinator_with_cm
    cm.start_call.side_effect = OSError("tcp connect failed")
    await coordinator.async_handle_push(_FULL_PUSH)

    assert await coordinator.async_ensure_call_started() is False
    coordinator.api.answer_call.assert_not_awaited()

    cm.start_call.side_effect = None
    cm.start_call.return_value = "sip-call-retry"
    assert await coordinator.async_ensure_call_started() is True
    assert cm.start_call.await_count == 2
    coordinator.api.answer_call.assert_awaited_once_with("300705065")


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


async def test_open_door_waits_for_confirm_when_invite_already_in_flight(
    coordinator_with_cm,
) -> None:
    """Camera-tap-then-unlock: the camera already started the INVITE
    (pending.started=True) but the dialog is still INVITING when the user
    taps Unlock ~1s later. open_door must wait for the dialog to confirm
    instead of firing the MESSAGE into an unconfirmed dialog and failing."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-cam-first"
    await coordinator.async_handle_push(_FULL_PUSH)
    # Camera tap: INVITE in flight, stream not ready yet.
    await coordinator.async_ensure_call_started()

    confirmed = False
    # The real sip_client only accepts in-dialog MESSAGEs once CONFIRMED.
    cm.send_open_door = MagicMock(side_effect=lambda code: confirmed)

    async def confirm_after_delay() -> None:
        nonlocal confirmed
        await asyncio.sleep(0.01)
        confirmed = True
        coordinator.set_stream_url("sip-call-cam-first", "rtsp://x")

    asyncio.create_task(confirm_after_delay())

    assert await coordinator.async_open_door() is True
    cm.start_call.assert_awaited_once()  # no duplicate INVITE
    cm.send_open_door.assert_called_once_with("*")


async def test_toggle_backlight_waits_for_confirm_when_invite_already_in_flight(
    coordinator_with_cm,
) -> None:
    """Camera-tap-then-backlight: same race as unlock — the INVITE is already
    in flight (pending.started=True) but the dialog is still INVITING when the
    user toggles backlight. The MESSAGE must wait for the dialog to confirm."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-cam-first"
    await coordinator.async_handle_push(_FULL_PUSH)
    # Camera tap: INVITE in flight, stream not ready yet.
    await coordinator.async_ensure_call_started()

    confirmed = False
    # The real sip_client only accepts in-dialog MESSAGEs once CONFIRMED.
    cm.send_backlight = MagicMock(side_effect=lambda: confirmed)

    async def confirm_after_delay() -> None:
        nonlocal confirmed
        await asyncio.sleep(0.01)
        confirmed = True
        coordinator.set_stream_url("sip-call-cam-first", "rtsp://x")

    asyncio.create_task(confirm_after_delay())

    assert await coordinator.async_toggle_backlight() is True
    cm.start_call.assert_awaited_once()  # no duplicate INVITE
    cm.send_backlight.assert_called_once_with()


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


async def test_new_push_aborts_previous_active_call(
    coordinator_with_cm,
) -> None:
    """When a second FCM push arrives while a previous call is still tracked
    (mid-call or in the 60 s grace), the coordinator aborts the old call so
    the next ensure_call_started isn't blocked by 'Call already active'."""
    coordinator, cm = coordinator_with_cm
    cm.active_call_id = "previous-sip-call-id"

    await coordinator.async_handle_push(_FULL_PUSH)

    cm.abort_active_call.assert_awaited_once()
    # The new ring's state is still applied.
    assert coordinator.data is not None
    assert coordinator.data.ring_seq == 1
    assert coordinator._pending is not None


async def test_new_push_does_not_abort_when_no_previous_call(
    coordinator_with_cm,
) -> None:
    """First ring after startup: nothing to abort, abort_active_call is not
    awaited (would log a noisy 'aborting' INFO for no reason)."""
    coordinator, cm = coordinator_with_cm
    cm.active_call_id = None

    await coordinator.async_handle_push(_FULL_PUSH)

    cm.abort_active_call.assert_not_awaited()


async def test_call_cancel_push_aborts_active_call(coordinator_with_cm) -> None:
    """`notif_type=callCancel` aborts any in-flight call and clears _pending
    so the iPhone tile won't try to fetch a dead stream."""
    coordinator, cm = coordinator_with_cm
    cm.active_call_id = "sip-call-going"
    await coordinator.async_handle_push(_FULL_PUSH)
    assert coordinator._pending is not None

    await coordinator.async_handle_push(
        {"notif_type": "callCancel", "call_id": "300705065"}
    )

    cm.abort_active_call.assert_awaited()
    assert coordinator._pending is None


async def test_call_cancel_via_cancel_extra(coordinator_with_cm) -> None:
    """Alternative shape — older pushes carry a `cancel` extra field
    without a `notif_type` (FirebaseMessaging.java:248)."""
    coordinator, cm = coordinator_with_cm
    cm.active_call_id = "sip-call-going"
    await coordinator.async_handle_push(_FULL_PUSH)

    await coordinator.async_handle_push({"cancel": "1", "call_id": "300705065"})

    cm.abort_active_call.assert_awaited()
    assert coordinator._pending is None


async def test_call_cancel_for_different_ring_is_ignored(
    coordinator_with_cm,
) -> None:
    """An out-of-order cancel push for a PREVIOUS ring must not kill a new
    ring that just arrived — only a cancel matching the active call_id (or
    one carrying no id at all) tears the call down."""
    coordinator, cm = coordinator_with_cm
    await coordinator.async_handle_push(_FULL_PUSH)  # active ring 300705065
    cm.active_call_id = "sip-call-new-ring"

    await coordinator.async_handle_push(
        {"notif_type": "callCancel", "call_id": "OLD-RING"}
    )

    cm.abort_active_call.assert_not_awaited()
    assert coordinator._pending is not None


async def test_call_cancel_without_id_still_aborts(coordinator_with_cm) -> None:
    """A cancel push carrying no identifier keeps the historical
    unconditional-abort behavior."""
    coordinator, cm = coordinator_with_cm
    await coordinator.async_handle_push(_FULL_PUSH)
    cm.active_call_id = "sip-call-going"

    await coordinator.async_handle_push({"notif_type": "callCancel"})

    cm.abort_active_call.assert_awaited_once()
    assert coordinator._pending is None


async def test_call_cancel_wakes_blocked_stream_waiter(coordinator_with_cm) -> None:
    """A camera task blocked in async_wait_for_stream must wake immediately
    when the visitor cancels — not stall the full 20s timeout wedging
    HomeKit's stream_source path."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-cancel-wake"
    await coordinator.async_handle_push(_FULL_PUSH)
    await coordinator.async_ensure_call_started()
    cm.active_call_id = "sip-call-cancel-wake"

    waiter = asyncio.create_task(coordinator.async_wait_for_stream(timeout=5.0))
    await asyncio.sleep(0)  # let the waiter block on stream_ready

    await coordinator.async_handle_push(
        {"notif_type": "callCancel", "call_id": "300705065"}
    )

    url = await asyncio.wait_for(waiter, timeout=0.1)
    assert url is None


async def test_stream_teardown_wakes_blocked_stream_waiter(
    coordinator_with_cm,
) -> None:
    """Same wake-up guarantee on the BYE/teardown path (set_stream_url with
    url=None): waiters must read the final None state promptly."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-bye-wake"
    await coordinator.async_handle_push(_FULL_PUSH)
    await coordinator.async_ensure_call_started()

    waiter = asyncio.create_task(coordinator.async_wait_for_stream(timeout=5.0))
    await asyncio.sleep(0)  # let the waiter block on stream_ready

    coordinator.set_stream_url("sip-call-bye-wake", None)

    url = await asyncio.wait_for(waiter, timeout=0.1)
    assert url is None


async def test_unregister_push_triggers_reauth(coordinator_with_cm) -> None:
    """`notif_type=unregister` means the server invalidated our creds;
    kick off the HA reauth flow so the silent JWT refresh runs first and,
    if it fails, the user sees a repair prompt."""
    coordinator, _ = coordinator_with_cm
    coordinator.entry.async_start_reauth = MagicMock()

    await coordinator.async_handle_push({"notif_type": "unregister"})

    coordinator.entry.async_start_reauth.assert_called_once_with(coordinator.hass)


async def test_unregister_via_extra(coordinator_with_cm) -> None:
    coordinator, _ = coordinator_with_cm
    coordinator.entry.async_start_reauth = MagicMock()

    await coordinator.async_handle_push({"unregister": "1"})

    coordinator.entry.async_start_reauth.assert_called_once_with(coordinator.hass)


async def test_async_toggle_backlight_sends_signal(coordinator_with_cm) -> None:
    """Once the SIP dialog is up, toggling the switch sends the
    `contrast` MESSAGE via the call manager."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-bl"

    await coordinator.async_handle_push(_FULL_PUSH)
    await coordinator.async_ensure_call_started()
    coordinator.set_stream_url("sip-call-bl", "rtsp://x")

    assert await coordinator.async_toggle_backlight() is True
    cm.send_backlight.assert_called_once()


async def test_async_toggle_backlight_returns_false_without_call(
    coordinator_with_cm,
) -> None:
    coordinator, cm = coordinator_with_cm
    assert await coordinator.async_toggle_backlight() is False
    cm.send_backlight.assert_not_called()


async def test_wait_for_stream_timeout_returns_none_without_killing_call(
    coordinator_with_cm,
) -> None:
    """On timeout we surface None to HomeKit but keep the SIP dialog alive so
    the user can still tap Unlock during the call window."""
    coordinator, cm = coordinator_with_cm
    cm.start_call.return_value = "sip-call-timeout"
    await coordinator.async_handle_push(_FULL_PUSH)
    await coordinator.async_ensure_call_started()

    url = await coordinator.async_wait_for_stream(timeout=0.01)

    assert url is None
    # No teardown on call_manager — door MESSAGE still possible until natural BYE.
    cm.hang_up.assert_not_awaited()


async def test_handle_push_parses_hardware_identity_fields(coordinator) -> None:
    """When the FCM push carries iOS-style hardware identity, CallState
    surfaces it for the event entity. Accepts both camelCase and snake_case
    variants since the wire format is undocumented and Cogelec has changed
    names server-side before."""
    await coordinator.async_handle_push(
        {
            "call_id": "1",
            "message": "PORTE RUE",
            "LOGIN_TO_CALL": "ABC",
            "hardwareName": "Entrée principale",
            "hardwareType": "kit_villa",
            "hardwareId": "hw-42",
        }
    )
    assert coordinator.data.hardware_name == "Entrée principale"
    assert coordinator.data.hardware_type == "kit_villa"
    assert coordinator.data.hardware_id == "hw-42"


async def test_handle_push_falls_back_to_hardware_name_when_message_absent(
    coordinator,
) -> None:
    """`message` is the historical Android door label; `hardwareName` is the
    richer iOS field. When only the latter is present (some payload
    flavours), use it for `door_name` so the event isn't a generic
    "Doorbell"."""
    await coordinator.async_handle_push(
        {
            "call_id": "1",
            "LOGIN_TO_CALL": "ABC",
            "hardware_name": "Garage gate",
        }
    )
    # `message` is the v1-shape gate for "is this a call push" — without it
    # the historical detector treats this as non-call. So this needs the
    # TYPE=24 path or the bell `notif_type`. Skip the message and use the
    # new-shape signal.
    await coordinator.async_handle_push(
        {
            "TYPE": "24",
            "NOTIFICATION_UUID": "uuid-43",
            "LOGIN_TO_CALL": "ABC",
            "hardware_name": "Garage gate",
        }
    )
    assert coordinator.data.door_name == "Garage gate"


async def test_handle_push_parses_call_end_delay(coordinator_with_cm) -> None:
    """When `callEndDelay` is present in the push, the override is forwarded
    to CallManager.start_call so the timeout matches the gateway-configured
    call window instead of the hardcoded 120 s wedge protection."""
    coordinator, cm = coordinator_with_cm
    payload = {**_FULL_PUSH, "callEndDelay": "45"}
    await coordinator.async_handle_push(payload)
    await coordinator.async_ensure_call_started()
    cm.start_call.assert_awaited_once_with(
        target_uri="sip:2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36@178.32.84.135",
        target_host="178.32.84.135",
        sip_username="cogelecTest",
        sip_password="CogeleC",
        max_duration_s=45.0,
    )


async def test_handle_push_ignores_zero_call_end_delay(coordinator_with_cm) -> None:
    """A 0 or empty `callEndDelay` falls back to the hardcoded default —
    surfacing 0 would cause the auto-terminate task to fire immediately."""
    coordinator, cm = coordinator_with_cm
    payload = {**_FULL_PUSH, "callEndDelay": "0"}
    await coordinator.async_handle_push(payload)
    await coordinator.async_ensure_call_started()
    # max_duration_s=None means "use the CallManager hardcoded default"
    _, kwargs = cm.start_call.await_args
    assert kwargs["max_duration_s"] is None


async def test_handle_push_ignores_garbage_call_end_delay(
    coordinator_with_cm,
) -> None:
    coordinator, cm = coordinator_with_cm
    payload = {**_FULL_PUSH, "callEndDelay": "not-a-number"}
    await coordinator.async_handle_push(payload)
    await coordinator.async_ensure_call_started()
    _, kwargs = cm.start_call.await_args
    assert kwargs["max_duration_s"] is None
