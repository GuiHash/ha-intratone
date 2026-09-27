"""Tests for CallManager — orchestration of SIP UAC (TCP) + AudioBridge.

We mock the AudioBridge so no ffmpeg is spawned, patch
`loop.create_connection` so no real TCP socket is opened, and patch
`_bind_rtp_socket` so no real UDP port is bound. The IntratoneSipClient
itself is exercised through the manager via the fake TCP transport.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.intratone.audio_bridge import AudioBridge
from custom_components.intratone.call_manager import CallManager
from custom_components.intratone.sip_client import CallEstablished

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
