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

    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    sip_client = manager._sip_client

    # Promote the call to CONFIRMED so hang_up actually sends BYE.
    sip_client._call.state = CallState.CONFIRMED  # type: ignore[union-attr]
    sip_client._call.remote_to_header = f"<{TARGET_URI}>;tag=srv"  # type: ignore[union-attr]

    await manager.hang_up()

    fake_bridge.stop.assert_awaited()


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
