"""Tests for CallManager — orchestration of SIP UAC (TCP) + AudioBridge.

We mock the AudioBridge so no ffmpeg is spawned, patch
`loop.create_connection` so no real TCP socket is opened, and patch
`_bind_rtp_socket` so no real UDP port is bound. The IntratoneSipClient
itself is exercised through the manager via the fake TCP transport.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from custom_components.intratone import call_manager as cm_mod
from custom_components.intratone.audio_bridge import AudioBridge, BridgeStoppedError
from custom_components.intratone.call_manager import (
    _RTP_RCVBUF_BYTES,
    CallManager,
    _bind_rtp_pair,
    _bind_rtp_socket,
    _new_rtp_socket,
)
from custom_components.intratone.sip_client import (
    CallEstablished,
    CallState,
    IntratoneSipClient,
)

LOCAL_HOST = "192.168.1.50"
SIP_USER = "cogelecTest"
SIP_PASS = "CogeleC"
SERVER_IP = "178.32.84.135"
TARGET_URI = f"sip:LOGIN_TO_CALL_TOKEN@{SERVER_IP}"


class _FakeTcpTransport:
    """Records writes; tests introspect outgoing SIP bytes via `.written`."""

    def __init__(self, bound_port: int = 54321) -> None:
        self.written: list[bytes] = []
        self._bound_port = bound_port
        self._closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        self._closed = True

    def is_closing(self) -> bool:
        return self._closed

    def get_extra_info(self, name: str, default=None):
        if name == "sockname":
            return ("0.0.0.0", self._bound_port)
        return default


@pytest.fixture
def fake_bridge():
    bridge = MagicMock(spec=AudioBridge)
    bridge.start = AsyncMock(return_value="rtsp://127.0.0.1:8556/intratone")
    bridge.stop = AsyncMock()
    return bridge


@pytest.fixture
def active_calls():
    return []


@pytest.fixture
def ended_calls():
    return []


@pytest.fixture
async def manager(fake_bridge, active_calls, ended_calls):
    """A started CallManager whose `create_connection` is mocked to install
    a fake TCP transport on the protocol. Also stubs `_bind_rtp_socket` so
    pytest-socket's network ban doesn't trip."""
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda call_id, url: active_calls.append((call_id, url)),
        on_call_ended=lambda call_id: ended_calls.append(call_id),
        audio_bridge=fake_bridge,
    )

    fake_transports: list[_FakeTcpTransport] = []

    async def fake_create_connection(protocol_factory, **_kwargs):
        proto = protocol_factory()
        transport = _FakeTcpTransport()
        proto.connection_made(transport)
        fake_transports.append(transport)
        return transport, proto

    fake_rtp_sock = MagicMock()
    fake_rtp_sock.getsockname = MagicMock(return_value=("0.0.0.0", 16400))
    fake_rtp_sock.close = MagicMock()

    with (
        patch.object(
            asyncio.get_running_loop(),
            "create_connection",
            side_effect=fake_create_connection,
        ),
        patch(
            "custom_components.intratone.call_manager._bind_rtp_socket",
            return_value=fake_rtp_sock,
        ),
    ):
        await mgr.async_start()
        # Expose the transports so tests can assert on writes.
        mgr._test_transports = fake_transports  # type: ignore[attr-defined]
        yield mgr
        await mgr.async_stop()


async def test_async_start_marks_running(manager):
    assert manager.is_running


async def test_async_start_is_idempotent(manager):
    await manager.async_start()
    assert manager.is_running


async def test_start_call_without_starting_returns_none(fake_bridge):
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda *_: None,
        on_call_ended=lambda *_: None,
        audio_bridge=fake_bridge,
    )
    # Without async_start() the manager refuses calls.
    call_id = await mgr.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is None


async def test_start_call_never_logs_sip_identities(manager, caplog):
    """The INVITE target is `sip:<LOGIN_TO_CALL>@<server ip>` — both masked
    in diagnostics and FCM push logs, so no record may carry them."""
    with caplog.at_level(logging.DEBUG, logger="custom_components.intratone"):
        await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Outgoing SIP INVITE" in text
    assert "LOGIN_TO_CALL_TOKEN" not in text
    assert SIP_USER not in text
    assert SIP_PASS not in text


async def test_start_call_opens_tcp_and_sends_invite(manager):
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    assert manager.active_call_id == call_id

    transports = manager._test_transports  # type: ignore[attr-defined]
    assert len(transports) == 1
    invite = transports[0].written[0]
    assert invite.startswith(f"INVITE {TARGET_URI} SIP/2.0".encode())
    # PCMU codec in SDP, not Opus.
    assert b"PCMU/8000" in invite
    assert b"opus" not in invite.lower()
    # Transport is TCP throughout.
    assert b"SIP/2.0/TCP" in invite
    assert b"transport=tcp" in invite


async def test_second_overlapping_ring_is_ignored(manager):
    first = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    second = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert first is not None
    assert second is None
    # Only one TCP connection opened.
    assert len(manager._test_transports) == 1  # type: ignore[attr-defined]


async def test_sip_tcp_connect_failure_returns_none(fake_bridge):
    """If `create_connection` raises (DNS failure, refused), `start_call`
    returns None and the RTP socket is closed — next ring isn't blocked."""
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda *_: None,
        on_call_ended=lambda *_: None,
        audio_bridge=fake_bridge,
    )
    fake_rtp_sock = MagicMock()
    fake_rtp_sock.getsockname = MagicMock(return_value=("0.0.0.0", 16400))
    fake_rtp_sock.close = MagicMock()

    async def boom(*_args, **_kwargs):
        raise OSError("connection refused")

    with (
        patch.object(
            asyncio.get_running_loop(), "create_connection", side_effect=boom
        ),
        patch(
            "custom_components.intratone.call_manager._bind_rtp_socket",
            return_value=fake_rtp_sock,
        ),
    ):
        await mgr.async_start()
        result = await mgr.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)

    assert result is None
    assert mgr.active_call_id is None
    fake_rtp_sock.close.assert_called_once()


async def test_call_established_spawns_bridge_and_fires_active(
    manager, fake_bridge, active_calls
):
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    sip_client = manager._sip_client
    assert sip_client is not None
    local_rtp = sip_client._call.local_rtp_port  # type: ignore[union-attr]

    sip_client._on_call_established(  # type: ignore[union-attr]
        CallEstablished(
            call_id=call_id,
            remote_rtp_ip="178.32.84.99",
            remote_rtp_port=20002,
            local_rtp_port=local_rtp,
        )
    )
    # Let the create_task'd bridge start run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    fake_bridge.start.assert_awaited_once()
    call_kwargs = fake_bridge.start.await_args.kwargs
    assert call_kwargs["remote_rtp_ip"] == "178.32.84.99"
    assert call_kwargs["remote_rtp_port"] == 20002
    assert "rtp_socket" in call_kwargs
    assert active_calls == [(call_id, "rtsp://127.0.0.1:8556/intratone")]


async def test_spawn_bridge_sends_mute_off_after_bridge_up(
    manager, fake_bridge, active_calls
):
    """Right after the bridge is consumable, CallManager fires `MUTE_OFF` on
    the same SIP dialog — mirrors Cogelec's behaviour on manual pickup and
    appears to extend the server-side call window."""
    from custom_components.intratone.sip_client import CallState

    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    sip_client = manager._sip_client
    local_rtp = sip_client._call.local_rtp_port  # type: ignore[union-attr]
    # Confirm the dialog so send_mute_off can serialize the in-dialog MESSAGE.
    sip_client._call.state = CallState.CONFIRMED  # type: ignore[union-attr]
    sip_client._call.remote_to_header = f"<{TARGET_URI}>;tag=srv"  # type: ignore[union-attr]

    sip_client._on_call_established(  # type: ignore[union-attr]
        CallEstablished(
            call_id=call_id,
            remote_rtp_ip="178.32.84.99",
            remote_rtp_port=20002,
            local_rtp_port=local_rtp,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert active_calls == [(call_id, "rtsp://127.0.0.1:8556/intratone")]
    # The MESSAGE landed on the same TCP transport that carried the INVITE.
    transports = manager._test_transports  # type: ignore[attr-defined]
    assert any(b"MUTE_OFF" in payload for payload in transports[0].written)


async def test_bridge_video_failure_callback_triggers_reinvite_audio_only(
    manager, fake_bridge,
):
    """AudioBridge invokes `on_video_failure` when the PLI burst exhausts
    without a keyframe — CallManager wires that to `send_reinvite_audio_only`
    on the SIP client. End-to-end: a no-video gateway never blocks audio.
    """
    from custom_components.intratone.sip_client import CallState

    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    sip_client = manager._sip_client
    # Need a video-enabled, CONFIRMED dialog for the re-INVITE to actually fire.
    sip_client._call.state = CallState.CONFIRMED  # type: ignore[union-attr]
    sip_client._call.remote_to_header = f"<{TARGET_URI}>;tag=srv"  # type: ignore[union-attr]
    sip_client._call.local_video_rtp_port = 20000  # type: ignore[union-attr]

    sip_client._on_call_established(  # type: ignore[union-attr]
        CallEstablished(
            call_id=call_id,
            remote_rtp_ip="178.32.84.99",
            remote_rtp_port=20002,
            local_rtp_port=sip_client._call.local_rtp_port,  # type: ignore[union-attr]
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Pull the callback that CallManager handed to AudioBridge.start().
    on_video_failure = fake_bridge.start.await_args.kwargs.get("on_video_failure")
    assert on_video_failure is not None
    on_video_failure()

    # An in-dialog re-INVITE landed on the same TCP transport with the
    # bumped CSeq, no `m=video` media line, and the MUTE_OFF/initial INVITE
    # CSeqs still on the wire — i.e. the renegotiation happened in-dialog.
    transports = manager._test_transports  # type: ignore[attr-defined]
    written = transports[0].written
    reinvite = next(
        (
            b for b in written
            if b.startswith(b"INVITE ") and b"CSeq: 52 INVITE" in b
        ),
        None,
    )
    assert reinvite is not None, "no re-INVITE with CSeq 52 found"
    assert b"m=audio" in reinvite
    assert b"m=video" not in reinvite


async def test_bridge_start_failure_hangs_up(manager, fake_bridge, active_calls):
    fake_bridge.start.side_effect = RuntimeError("ffmpeg blew up")
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    sip_client = manager._sip_client
    sip_client._on_call_established(  # type: ignore[union-attr]
        CallEstablished(
            call_id=call_id,
            remote_rtp_ip="x",
            remote_rtp_port=1,
            local_rtp_port=2,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert active_calls == []


async def test_call_terminated_fires_ended_after_grace(
    manager, fake_bridge, ended_calls
):
    """SIP teardown does NOT immediately clear active_call_id — we hold the
    bridge alive for `_POST_BYE_GRACE_S` so a slow iPhone live-view tap still
    sees a valid stream URL. After the grace, both bridge and tracker clear."""
    from custom_components.intratone import call_manager as cm_mod

    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    sip_client = manager._sip_client

    with patch.object(cm_mod, "_POST_BYE_GRACE_S", 0.01):
        sip_client._on_call_terminated(call_id)  # type: ignore[union-attr]
        # During the grace period, the active_call_id is still set.
        assert manager.active_call_id == call_id
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)

    fake_bridge.stop.assert_awaited()
    assert ended_calls == [call_id]
    assert manager.active_call_id is None


async def test_hang_up_stops_bridge(manager, fake_bridge):
    from custom_components.intratone.sip_client import CallState

    await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    sip_client = manager._sip_client

    # Promote the call to CONFIRMED so hang_up actually sends BYE.
    sip_client._call.state = CallState.CONFIRMED  # type: ignore[union-attr]
    sip_client._call.remote_to_header = f"<{TARGET_URI}>;tag=srv"  # type: ignore[union-attr]

    await manager.hang_up()

    fake_bridge.stop.assert_awaited()


async def test_abort_active_call_during_active_dialog(
    manager, fake_bridge, ended_calls
):
    """While the SIP dialog is still up, abort_active_call sends BYE, stops
    the bridge, fires on_call_ended and clears all state — so the next ring
    isn't blocked by the 'Call already active' guard."""
    from custom_components.intratone.sip_client import CallState

    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    sip_client = manager._sip_client
    sip_client._call.state = CallState.CONFIRMED  # type: ignore[union-attr]
    sip_client._call.remote_to_header = f"<{TARGET_URI}>;tag=srv"  # type: ignore[union-attr]

    await manager.abort_active_call()

    fake_bridge.stop.assert_awaited_once()
    assert ended_calls == [call_id]
    assert manager.active_call_id is None
    assert manager._sip_client is None


async def test_abort_active_call_during_post_bye_grace(
    manager, fake_bridge, ended_calls
):
    """After BYE, _sip_client is None but _active_call_id stays set during
    the 60 s grace. abort_active_call must cancel the grace task, stop the
    bridge, and clear state immediately so a follow-up ring can proceed."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    sip_client = manager._sip_client
    # Simulate the BYE path: _handle_call_terminated clears sip_client and
    # schedules the grace task while keeping _active_call_id set.
    sip_client._on_call_terminated(call_id)  # type: ignore[union-attr]
    assert manager.active_call_id == call_id
    assert manager._sip_client is None
    assert manager._grace_task is not None and not manager._grace_task.done()

    await manager.abort_active_call()

    fake_bridge.stop.assert_awaited()
    assert ended_calls == [call_id]
    assert manager.active_call_id is None
    assert manager._grace_task is None


async def test_abort_active_call_is_noop_when_no_active_call(
    manager, fake_bridge, ended_calls
):
    await manager.abort_active_call()
    fake_bridge.stop.assert_not_awaited()
    assert ended_calls == []


# --- in-flight _spawn_bridge races -----------------------------------------


def _slow_bridge_start(fake_bridge):
    """Make fake_bridge.start controllable: `started` fires once the spawn
    task is inside bridge.start() (mid-ffmpeg-spawn in production, which
    takes 100-400 ms); `release` lets it run to completion."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _start(**_kwargs):
        started.set()
        await release.wait()
        return "rtsp://127.0.0.1:8556/intratone"

    fake_bridge.start = AsyncMock(side_effect=_start)
    return started, release


def _establish(manager, call_id) -> None:
    manager._sip_client._on_call_established(
        CallEstablished(
            call_id=call_id,
            remote_rtp_ip="178.32.84.99",
            remote_rtp_port=20002,
            local_rtp_port=16400,
        )
    )


async def test_async_stop_cancels_inflight_spawn_bridge(
    manager, fake_bridge, active_calls
):
    """Reload mid-establishment: the 200 OK schedules _spawn_bridge; if
    async_stop() doesn't cancel it, the pending task resumes AFTER unload,
    spawns ffmpeg with nothing left to stop it, and calls back into a dead
    coordinator."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    started, release = _slow_bridge_start(fake_bridge)
    _establish(manager, call_id)
    await asyncio.wait_for(started.wait(), timeout=1)

    await manager.async_stop()

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # The spawn task must have been cancelled before it could mark the call
    # active against a torn-down manager.
    assert active_calls == []


async def test_abort_active_call_cancels_inflight_spawn_bridge(
    manager, fake_bridge, active_calls, ended_calls
):
    """New ring while call A's bridge is still starting: abort_active_call
    runs while _spawn_bridge is inside bridge.start() (bridge not yet
    is_running, so bridge.stop() alone is a no-op for it) — the in-flight
    task must be cancelled, or A's start() would complete against A's dead
    RTP endpoint and call B would then be served that stale bridge."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    started, release = _slow_bridge_start(fake_bridge)
    _establish(manager, call_id)
    await asyncio.wait_for(started.wait(), timeout=1)

    await manager.abort_active_call()

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ended_calls == [call_id]
    assert active_calls == []


async def test_spawn_bridge_discards_bridge_when_call_superseded_during_start(
    manager, fake_bridge, active_calls
):
    """bridge.start() has several awaits (ffmpeg spawn, endpoint wraps): if
    the active call changed meanwhile, the just-started bridge belongs to the
    OLD call's RTP endpoint — publishing it would give a clean-looking call
    with zero audio. _spawn_bridge must re-check the active call after
    start() returns, stop the stale bridge, and NOT fire on_call_active."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    started, release = _slow_bridge_start(fake_bridge)
    _establish(manager, call_id)
    await asyncio.wait_for(started.wait(), timeout=1)

    # A fresh ring superseded this call while ffmpeg was starting.
    manager._active_call_id = "new-call-id"

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert active_calls == []
    fake_bridge.stop.assert_awaited()


async def test_send_backlight_delegates_to_sip_client(manager):
    """During an active call, send_backlight forwards to the SIP client
    which serializes the in-dialog MESSAGE body=`contrast`."""
    from custom_components.intratone.sip_client import CallState

    await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    sip_client = manager._sip_client
    sip_client._call.state = CallState.CONFIRMED  # type: ignore[union-attr]
    sip_client._call.remote_to_header = f"<{TARGET_URI}>;tag=srv"  # type: ignore[union-attr]

    assert manager.send_backlight() is True
    transports = manager._test_transports  # type: ignore[attr-defined]
    assert any(b"contrast" in payload for payload in transports[0].written)


async def test_send_backlight_returns_false_without_active_call(manager):
    assert manager.send_backlight() is False


async def test_async_stop_closes_transport_and_stops_bridge(manager, fake_bridge):
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    transports = manager._test_transports  # type: ignore[attr-defined]
    await manager.async_stop()
    assert manager._sip_transport is None
    assert transports[0]._closed is True
    fake_bridge.stop.assert_awaited()
    assert not manager.is_running


async def test_async_stop_is_idempotent(manager, fake_bridge):
    await manager.async_stop()
    await manager.async_stop()  # Must not raise.


async def test_max_call_duration_forces_teardown(manager, fake_bridge, ended_calls):
    """A call that never receives BYE is auto-terminated after the cap so
    the next ring isn't silently dropped by the `already active` guard."""
    from custom_components.intratone import call_manager as cm_mod
    from custom_components.intratone.sip_client import CallState

    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    sip_client = manager._sip_client
    sip_client._call.state = CallState.CONFIRMED  # type: ignore[union-attr]
    sip_client._call.remote_to_header = f"<{TARGET_URI}>;tag=srv"  # type: ignore[union-attr]

    with (
        patch.object(cm_mod, "_MAX_CALL_DURATION_S", 0.01),
        patch.object(cm_mod, "_POST_BYE_GRACE_S", 0.01),
    ):
        # Re-arm the task with the patched delay.
        if manager._max_duration_task is not None:
            manager._max_duration_task.cancel()
        manager._max_duration_task = asyncio.create_task(
            manager._auto_terminate_after(call_id, 0.01)
        )
        # Wait for: max-duration timer + grace period after BYE.
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert manager.active_call_id is None
    assert ended_calls == [call_id]


# --- ffmpeg prewarm -------------------------------------------------------


async def test_start_call_prewarms_video_ffmpeg(fake_bridge):
    """With video enabled, start_call must kick the ffmpeg prewarm right after
    the INVITE goes out — its startup overlaps the INVITE→200 OK round-trip."""
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda *_: None,
        on_call_ended=lambda *_: None,
        video_enabled=True,
        audio_bridge=fake_bridge,
    )

    async def fake_create_connection(protocol_factory, **_kwargs):
        proto = protocol_factory()
        transport = _FakeTcpTransport()
        proto.connection_made(transport)
        return transport, proto

    def _fake_sock(port: int) -> MagicMock:
        sock = MagicMock()
        sock.getsockname = MagicMock(return_value=("0.0.0.0", port))
        sock.close = MagicMock()
        return sock

    with (
        patch.object(
            asyncio.get_running_loop(),
            "create_connection",
            side_effect=fake_create_connection,
        ),
        patch(
            "custom_components.intratone.call_manager._bind_rtp_socket",
            return_value=_fake_sock(16400),
        ),
        patch(
            "custom_components.intratone.call_manager._bind_rtp_pair",
            return_value=(_fake_sock(16402), _fake_sock(16403)),
        ),
    ):
        await mgr.async_start()
        call_id = await mgr.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
        assert call_id is not None
        fake_bridge.prewarm.assert_called_once_with(video=True)
        await mgr.async_stop()


async def test_call_terminated_before_established_cancels_prewarm(
    manager, fake_bridge
):
    """INVITE rejected before 200 OK → the bridge is only stopped after the
    60 s grace window; the unadopted prewarmed ffmpeg must be reaped now."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    manager._handle_call_terminated(call_id)
    fake_bridge.cancel_prewarm.assert_called_once()


async def test_start_call_does_not_prewarm_without_video(manager, fake_bridge):
    """Audio-only config: the lavfi placeholder ffmpeg pushes to go2rtc as
    soon as it spawns, so prewarming it would publish a stream before the
    call even exists — must stay on-demand."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    fake_bridge.prewarm.assert_not_called()


# --- socket binder --------------------------------------------------------


def test_bind_rtp_socket_returns_bound_socket(socket_enabled):
    """`_bind_rtp_socket` returns a UDP socket already bound to a free port —
    eliminating the bind/probe race the older `_pick_rtp_port` had."""
    from custom_components.intratone.call_manager import _bind_rtp_socket

    sock = _bind_rtp_socket()
    try:
        host, port = sock.getsockname()
        assert host in ("0.0.0.0", "")
        assert 16384 <= port < 16484
        assert port % 2 == 0  # RTP convention: even ports
    finally:
        sock.close()


def test_bind_rtp_socket_never_shrinks_receive_buffer(socket_enabled):
    """The RTP receive buffer must be at least the OS default — a VP8 keyframe
    burst dropped while the event loop is busy (ffmpeg spawn) costs the full
    8-12 s natural keyframe interval, so shrinking the buffer is a regression."""
    import socket as socket_mod

    from custom_components.intratone.call_manager import (
        _bind_rtp_pair,
        _bind_rtp_socket,
    )

    plain = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_DGRAM)
    try:
        default_rcvbuf = plain.getsockopt(
            socket_mod.SOL_SOCKET, socket_mod.SO_RCVBUF
        )
    finally:
        plain.close()

    sock = _bind_rtp_socket()
    rtp_sock, rtcp_sock = _bind_rtp_pair()
    try:
        for s in (sock, rtp_sock):
            assert (
                s.getsockopt(socket_mod.SOL_SOCKET, socket_mod.SO_RCVBUF)
                >= default_rcvbuf
            )
    finally:
        sock.close()
        rtp_sock.close()
        rtcp_sock.close()


def test_bind_rtp_pair_returns_adjacent_ports(socket_enabled):
    """`_bind_rtp_pair` returns (RTP, RTCP) on consecutive (even, odd) ports
    — required so the gateway can send RTCP back to our advertised port + 1."""
    from custom_components.intratone.call_manager import _bind_rtp_pair

    rtp_sock, rtcp_sock = _bind_rtp_pair()
    try:
        rtp_port = rtp_sock.getsockname()[1]
        rtcp_port = rtcp_sock.getsockname()[1]
        assert rtp_port % 2 == 0
        assert rtcp_port == rtp_port + 1
        assert 16384 <= rtp_port < 16484
    finally:
        rtp_sock.close()
        rtcp_sock.close()


# --- RTP socket allocation (no real sockets) ------------------------------


def _fake_socket_module(sock) -> SimpleNamespace:
    """Stand-in for the `socket` module as call_manager sees it: the real
    constants, with `socket()` handing out `sock`."""
    return SimpleNamespace(
        AF_INET=socket.AF_INET,
        SOCK_DGRAM=socket.SOCK_DGRAM,
        SOL_SOCKET=socket.SOL_SOCKET,
        SO_RCVBUF=socket.SO_RCVBUF,
        socket=MagicMock(return_value=sock),
    )


@pytest.mark.parametrize(
    ("default_rcvbuf", "expected_setsockopt"),
    [
        (65536, [call(socket.SOL_SOCKET, socket.SO_RCVBUF, _RTP_RCVBUF_BYTES)]),
        (_RTP_RCVBUF_BYTES * 4, []),
    ],
)
def test_new_rtp_socket_only_ever_raises_receive_buffer(
    default_rcvbuf, expected_setsockopt
):
    """A small OS default is raised to the keyframe-burst target; a larger
    one (macOS) is left alone — never shrunk. Same on every platform."""
    sock = MagicMock()
    sock.getsockopt.return_value = default_rcvbuf
    with patch.object(cm_mod, "socket", _fake_socket_module(sock)):
        assert _new_rtp_socket() is sock
    sock.setblocking.assert_called_once_with(False)
    assert sock.setsockopt.call_args_list == expected_setsockopt


def test_new_rtp_socket_tolerates_receive_buffer_errors():
    """SO_RCVBUF tuning is best effort (sandboxed kernels refuse it): the
    socket is still handed out, non-blocking, instead of failing the ring."""
    sock = MagicMock()
    sock.getsockopt.return_value = 65536
    sock.setsockopt.side_effect = OSError(errno.EPERM, "Operation not permitted")
    with patch.object(cm_mod, "socket", _fake_socket_module(sock)):
        assert _new_rtp_socket() is sock
    sock.setblocking.assert_called_once_with(False)
    sock.close.assert_not_called()


class _FakeUdpSocket:
    """What `_new_rtp_socket()` hands out, minus the kernel: bind() fails
    with EADDRINUSE on the ports listed as busy."""

    def __init__(self, busy_ports: set[int]) -> None:
        self._busy_ports = busy_ports
        self.port: int | None = None
        self.closed = False

    def bind(self, addr: tuple[str, int]) -> None:
        if addr[1] in self._busy_ports:
            raise OSError(errno.EADDRINUSE, f"Address already in use: {addr[1]}")
        self.port = addr[1]

    def getsockname(self) -> tuple[str, int | None]:
        return ("0.0.0.0", self.port)

    def close(self) -> None:
        self.closed = True


def _patch_new_rtp_socket(busy_ports: set[int]):
    created: list[_FakeUdpSocket] = []

    def _new() -> _FakeUdpSocket:
        sock = _FakeUdpSocket(busy_ports)
        created.append(sock)
        return sock

    return created, patch.object(cm_mod, "_new_rtp_socket", side_effect=_new)


def test_bind_rtp_socket_skips_busy_ports_and_releases_probes():
    created, patcher = _patch_new_rtp_socket({16384, 16386})
    with patcher:
        sock = _bind_rtp_socket()
    assert sock.port == 16388
    # Each probe that hit a busy port was closed — no fd leak per retry.
    assert [s.closed for s in created] == [True, True, False]


def test_bind_rtp_socket_raises_when_range_exhausted():
    created, patcher = _patch_new_rtp_socket(set(range(16384, 16484)))
    with patcher, pytest.raises(RuntimeError, match="No free RTP port"):
        _bind_rtp_socket()
    assert len(created) == 50  # every even port of the range was tried
    assert all(s.closed for s in created)


def test_bind_rtp_pair_skips_pair_whose_rtcp_port_is_busy():
    """The RTCP half must be the RTP port + 1: when it's taken, the already
    bound RTP half is released and the next even port is tried."""
    created, patcher = _patch_new_rtp_socket({16385})
    with patcher:
        rtp_sock, rtcp_sock = _bind_rtp_pair()
    assert (rtp_sock.port, rtcp_sock.port) == (16386, 16387)
    assert created[0].closed and created[1].closed  # half-bound first pair
    assert not rtp_sock.closed and not rtcp_sock.closed


def test_bind_rtp_pair_raises_when_range_exhausted():
    # First pair fails on its RTP half, every other pair on its RTCP half.
    busy = {16384} | set(range(16385, 16484, 2))
    created, patcher = _patch_new_rtp_socket(busy)
    with patcher, pytest.raises(RuntimeError, match=r"No free RTP\+RTCP pair"):
        _bind_rtp_pair()
    assert len(created) == 1 + 49 * 2
    assert all(s.closed for s in created)


# --- CallManager edge and failure paths -----------------------------------


def _mock_rtp_sock(port: int) -> MagicMock:
    sock = MagicMock()
    sock.getsockname = MagicMock(return_value=("0.0.0.0", port))
    sock.close = MagicMock()
    return sock


async def _fake_create_connection(protocol_factory, **_kwargs):
    proto = protocol_factory()
    transport = _FakeTcpTransport()
    proto.connection_made(transport)
    return transport, proto


def _confirm_dialog(manager) -> None:
    """Promote the pending INVITE to an established dialog so in-dialog
    requests (MESSAGE, BYE, re-INVITE) actually serialize."""
    manager._sip_client._call.state = CallState.CONFIRMED
    manager._sip_client._call.remote_to_header = f"<{TARGET_URI}>;tag=srv"


def _establish_call(manager, call_id, **overrides) -> None:
    info = {
        "call_id": call_id,
        "remote_rtp_ip": "178.32.84.99",
        "remote_rtp_port": 20002,
        "local_rtp_port": 16400,
        **overrides,
    }
    manager._sip_client._on_call_established(CallEstablished(**info))


async def _run_pending(ticks: int = 10) -> None:
    """Let already-scheduled tasks run — bare event-loop ticks, no
    wall-clock wait."""
    for _ in range(ticks):
        await asyncio.sleep(0)


@pytest.fixture
async def video_manager(fake_bridge, active_calls, ended_calls):
    """Like `manager`, with video enabled: yields (manager, (audio RTP,
    video RTP, video RTCP) mock sockets)."""
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda call_id, url: active_calls.append((call_id, url)),
        on_call_ended=lambda call_id: ended_calls.append(call_id),
        video_enabled=True,
        audio_bridge=fake_bridge,
    )
    sockets = (_mock_rtp_sock(16400), _mock_rtp_sock(16402), _mock_rtp_sock(16403))
    with (
        patch.object(
            asyncio.get_running_loop(),
            "create_connection",
            side_effect=_fake_create_connection,
        ),
        patch.object(cm_mod, "_bind_rtp_socket", return_value=sockets[0]),
        patch.object(cm_mod, "_bind_rtp_pair", return_value=sockets[1:]),
    ):
        await mgr.async_start()
        yield mgr, sockets
        await mgr.async_stop()


def test_relay_rtsp_url_is_relay_plus_stream_path():
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda *_: None,
        on_call_ended=lambda *_: None,
        go2rtc_url="rtsp://127.0.0.1:8554/",
    )
    assert mgr.relay_rtsp_url == "rtsp://127.0.0.1:8554/intratone"


async def test_sip_connect_timeout_returns_none_and_releases_socket(fake_bridge):
    """A black-holed SIP server (firewall drop) must not hang the ring: the
    connect is bounded, start_call returns None, the RTP port is freed."""
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda *_: None,
        on_call_ended=lambda *_: None,
        audio_bridge=fake_bridge,
    )
    rtp_sock = _mock_rtp_sock(16400)

    async def never_connects(*_args, **_kwargs):
        await asyncio.Event().wait()

    with (
        patch.object(
            asyncio.get_running_loop(),
            "create_connection",
            side_effect=never_connects,
        ),
        patch.object(cm_mod, "_bind_rtp_socket", return_value=rtp_sock),
        patch.object(cm_mod, "_SIP_CONNECT_TIMEOUT_S", 0),
    ):
        await mgr.async_start()
        assert await mgr.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS) is None

    rtp_sock.close.assert_called_once()
    assert mgr.active_call_id is None
    await mgr.async_stop()


async def test_sip_connect_failure_survives_socket_close_error(fake_bridge):
    """Releasing the pre-bound RTP socket is best effort: an OSError from
    close() must not turn a clean "connect failed → None" into an exception,
    and the socket is not closed a second time on unload."""
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda *_: None,
        on_call_ended=lambda *_: None,
        audio_bridge=fake_bridge,
    )
    rtp_sock = _mock_rtp_sock(16400)
    rtp_sock.close.side_effect = OSError(errno.EBADF, "Bad file descriptor")

    async def refused(*_args, **_kwargs):
        raise OSError(errno.ECONNREFUSED, "Connection refused")

    with (
        patch.object(
            asyncio.get_running_loop(), "create_connection", side_effect=refused
        ),
        patch.object(cm_mod, "_bind_rtp_socket", return_value=rtp_sock),
    ):
        await mgr.async_start()
        assert await mgr.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS) is None
        await mgr.async_stop()

    rtp_sock.close.assert_called_once()


async def test_invite_failure_releases_call_resources_and_reraises(manager):
    """If the INVITE can't be sent, start_call re-raises (the coordinator
    logs it and lets the next tap retry) after closing the TCP transport and
    the RTP socket — the next ring must not be blocked by a half-open call."""
    rtp_sock = _mock_rtp_sock(16400)
    with (
        patch.object(cm_mod, "_bind_rtp_socket", return_value=rtp_sock),
        patch.object(
            IntratoneSipClient,
            "call",
            side_effect=RuntimeError("Transport not connected"),
        ),
        pytest.raises(RuntimeError, match="Transport not connected"),
    ):
        await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)

    assert manager._test_transports[0].is_closing()  # type: ignore[attr-defined]
    rtp_sock.close.assert_called_once()
    assert manager.active_call_id is None
    assert await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "start_call leaks the bound audio RTP socket when the video RTP/RTCP "
        "pair can't be allocated (held until the next ring or unload)"
    ),
)
async def test_video_port_exhaustion_releases_audio_socket(fake_bridge):
    """All video port pairs busy: start_call raises (the coordinator logs it
    and lets the next tap retry) — but the audio RTP socket it had already
    bound must be released, like on every other start_call failure path."""
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda *_: None,
        on_call_ended=lambda *_: None,
        video_enabled=True,
        audio_bridge=fake_bridge,
    )
    audio_sock = _mock_rtp_sock(16400)
    with (
        patch.object(cm_mod, "_bind_rtp_socket", return_value=audio_sock),
        patch.object(
            cm_mod,
            "_bind_rtp_pair",
            side_effect=RuntimeError("No free RTP+RTCP pair"),
        ),
    ):
        await mgr.async_start()
        try:
            with pytest.raises(RuntimeError, match="No free RTP"):
                await mgr.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
            audio_sock.close.assert_called_once()
        finally:
            await mgr.async_stop()


async def test_send_open_door_sends_in_dialog_message(manager):
    await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    _confirm_dialog(manager)

    assert manager.send_open_door("1") is True
    written = manager._test_transports[0].written  # type: ignore[attr-defined]
    assert any(b"opendoor:1" in payload for payload in written)


async def test_dialog_actions_without_active_call_are_noops(manager, fake_bridge):
    assert manager.send_open_door() is False
    assert manager.send_mute_off() is False
    await manager.hang_up()
    fake_bridge.stop.assert_not_awaited()


async def test_stale_sip_callbacks_are_ignored(manager, fake_bridge, ended_calls):
    """Callbacks carrying another call's id (a previous dialog's late
    packets) must not touch the current call."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    sip_client = manager._sip_client

    _establish_call(manager, "stale-call@192.168.1.50")
    sip_client._on_call_terminated("stale-call@192.168.1.50")
    await _run_pending()

    fake_bridge.start.assert_not_awaited()
    fake_bridge.cancel_prewarm.assert_not_called()
    assert manager.active_call_id == call_id
    assert manager._sip_client is sip_client
    assert manager._grace_task is None
    assert ended_calls == []


async def test_bridge_stopped_race_neither_hangs_up_nor_publishes(
    manager, fake_bridge, active_calls
):
    """BridgeStoppedError = a concurrent stop() (reload, superseding ring)
    owns the teardown. _spawn_bridge must bow out quietly: no BYE of its own,
    no MUTE_OFF, and no RTSP URL published for the stopped bridge."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    _confirm_dialog(manager)
    fake_bridge.start.side_effect = BridgeStoppedError("stopped during ffmpeg spawn")

    _establish_call(manager, call_id)
    await asyncio.wait_for(manager._spawn_bridge_task, timeout=1)

    assert active_calls == []
    written = manager._test_transports[0].written  # type: ignore[attr-defined]
    assert not any(p.startswith((b"BYE ", b"CANCEL ")) for p in written)
    assert not any(b"MUTE_OFF" in p for p in written)
    assert manager.active_call_id == call_id


async def test_bridge_failure_after_bye_does_not_hang_up_again(
    manager, fake_bridge, active_calls
):
    """BYE lands while ffmpeg is still spawning, then the spawn fails: the
    dialog is already gone, so there's nothing to hang up — the spawn task
    must finish cleanly and leave the call to the post-BYE grace teardown."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _start(**_kwargs):
        started.set()
        await release.wait()
        raise RuntimeError("ffmpeg exited")

    fake_bridge.start = AsyncMock(side_effect=_start)
    _establish_call(manager, call_id)
    await asyncio.wait_for(started.wait(), timeout=1)

    manager._sip_client._on_call_terminated(call_id)
    assert manager._sip_client is None
    release.set()
    await asyncio.wait_for(manager._spawn_bridge_task, timeout=1)

    assert active_calls == []
    assert manager.active_call_id == call_id  # grace window still running
    assert manager._grace_task is not None and not manager._grace_task.done()


async def test_immediate_bye_after_200_ok_never_starts_bridge(
    manager, fake_bridge, active_calls, ended_calls
):
    """200 OK and BYE parsed from the same TCP segment: both callbacks fire
    before the spawn task first runs, so the RTP socket is already released.
    The bridge must not start, and the call still ends exactly once."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    with patch.object(cm_mod, "_POST_BYE_GRACE_S", 0):
        _establish_call(manager, call_id)
        manager._sip_client._on_call_terminated(call_id)
        await asyncio.wait_for(manager._spawn_bridge_task, timeout=1)
        await asyncio.wait_for(manager._grace_task, timeout=1)

    fake_bridge.start.assert_not_awaited()
    assert active_calls == []
    assert ended_calls == [call_id]
    assert manager.active_call_id is None


async def test_negotiated_video_hands_video_sockets_to_bridge(
    video_manager, fake_bridge
):
    mgr, (audio_sock, video_sock, rtcp_sock) = video_manager
    call_id = await mgr.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)

    _establish_call(
        mgr,
        call_id,
        remote_video_rtp_ip="178.32.84.99",
        remote_video_rtp_port=52982,
    )
    await asyncio.wait_for(mgr._spawn_bridge_task, timeout=1)

    kwargs = fake_bridge.start.await_args.kwargs
    assert kwargs["rtp_socket"] is audio_sock
    assert kwargs["video_socket"] is video_sock
    assert kwargs["video_rtcp_socket"] is rtcp_sock
    assert kwargs["remote_video_rtp_port"] == 52982
    video_sock.close.assert_not_called()
    rtcp_sock.close.assert_not_called()


async def test_server_rejected_video_releases_video_sockets(
    video_manager, fake_bridge, active_calls
):
    """200 OK without m=video: the pre-bound video RTP/RTCP sockets are
    closed (best effort — a failing close doesn't block the call) and the
    bridge starts audio-only."""
    mgr, (audio_sock, video_sock, rtcp_sock) = video_manager
    rtcp_sock.close.side_effect = OSError(errno.EBADF, "Bad file descriptor")
    call_id = await mgr.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)

    _establish_call(mgr, call_id)  # no remote video endpoint
    await asyncio.wait_for(mgr._spawn_bridge_task, timeout=1)

    video_sock.close.assert_called_once()
    rtcp_sock.close.assert_called_once()
    kwargs = fake_bridge.start.await_args.kwargs
    assert kwargs["rtp_socket"] is audio_sock
    assert kwargs["video_socket"] is None
    assert kwargs["video_rtcp_socket"] is None
    assert active_calls == [(call_id, "rtsp://127.0.0.1:8556/intratone")]


async def test_video_failure_after_call_ended_is_ignored(manager, fake_bridge):
    """The PLI loop can give up after the BYE (post-BYE grace keeps the
    bridge alive): with no dialog left there's nothing to re-INVITE."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    _confirm_dialog(manager)
    _establish_call(manager, call_id)
    await asyncio.wait_for(manager._spawn_bridge_task, timeout=1)
    on_video_failure = fake_bridge.start.await_args.kwargs["on_video_failure"]
    transport = manager._test_transports[0]  # type: ignore[attr-defined]

    manager._sip_client._on_call_terminated(call_id)
    written = len(transport.written)
    on_video_failure()

    assert len(transport.written) == written


async def test_video_failure_reinvite_error_is_contained(
    manager, fake_bridge, caplog
):
    """on_video_failure runs inside the bridge's PLI loop: a failing
    re-INVITE is logged, never raised into the media pipeline."""
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    _confirm_dialog(manager)
    _establish_call(manager, call_id)
    await asyncio.wait_for(manager._spawn_bridge_task, timeout=1)
    on_video_failure = fake_bridge.start.await_args.kwargs["on_video_failure"]

    with patch.object(
        manager._sip_client,
        "send_reinvite_audio_only",
        side_effect=RuntimeError("boom"),
    ) as reinvite:
        on_video_failure()

    reinvite.assert_called_once_with(call_id)
    assert "re-INVITE audio-only failed" in caplog.text


async def test_max_duration_timer_of_superseded_call_is_ignored(manager):
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    _confirm_dialog(manager)

    await manager._auto_terminate_after("superseded@192.168.1.50", 0)

    assert manager.active_call_id == call_id
    assert manager._sip_client is not None
    written = manager._test_transports[0].written  # type: ignore[attr-defined]
    assert not any(p.startswith(b"BYE ") for p in written)


async def test_aborted_call_timers_never_end_the_next_call(
    manager, fake_bridge, ended_calls
):
    """Aborting call A (superseded by a new ring) BYEs its dialog; whatever
    timer that teardown leaves behind must not end call B or stop its
    bridge when it fires."""
    call_a = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    _confirm_dialog(manager)
    with patch.object(cm_mod, "_POST_BYE_GRACE_S", 0):
        await manager.abort_active_call()
        call_b = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
        await _run_pending()

    assert ended_calls == [call_a]
    fake_bridge.stop.assert_awaited_once()
    assert manager.active_call_id == call_b
    assert manager._sip_client is not None
