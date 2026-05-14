"""Tests for CallManager — orchestration of SIP UAC + AudioBridge.

We mock the AudioBridge so no ffmpeg is spawned and patch
`create_datagram_endpoint` so no real socket is opened. The IntratoneSipClient
is exercised end-to-end via injected datagrams.
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


class _FakeDatagramTransport:
    """Records every sendto() so tests can introspect outgoing SIP traffic."""

    def __init__(self, bound_port: int = 5070) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self._bound_port = bound_port
        self._closed = False

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))

    def close(self) -> None:
        self._closed = True

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
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda call_id, url: active_calls.append((call_id, url)),
        on_call_ended=lambda call_id: ended_calls.append(call_id),
        audio_bridge=fake_bridge,
    )

    async def fake_create_datagram_endpoint(protocol_factory, **_kwargs):
        proto = protocol_factory()
        transport = _FakeDatagramTransport()
        proto.connection_made(transport)
        return transport, proto

    with (
        patch.object(
            asyncio.get_running_loop(),
            "create_datagram_endpoint",
            side_effect=fake_create_datagram_endpoint,
        ),
        patch(
            "custom_components.intratone.call_manager._pick_rtp_port",
            return_value=16400,
        ),
    ):
        await mgr.async_start()
        yield mgr
    await mgr.async_stop()


async def test_async_start_creates_sip_client(manager):
    assert manager.is_running
    assert manager._sip_client is not None


async def test_async_start_is_idempotent(manager):
    transport_before = manager._transport
    await manager.async_start()
    assert manager._transport is transport_before


async def test_start_call_when_not_started(fake_bridge):
    mgr = CallManager(
        local_host=LOCAL_HOST,
        on_call_active=lambda *_: None,
        on_call_ended=lambda *_: None,
        audio_bridge=fake_bridge,
    )
    # Without async_start() the manager refuses calls.
    call_id = await mgr.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is None


async def test_start_call_sends_invite(manager):
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    assert manager.active_call_id == call_id

    transport = manager._transport
    sent = transport.sent  # type: ignore[attr-defined]
    assert len(sent) == 1
    data, addr = sent[0]
    assert addr == (SERVER_IP, 5060)
    assert data.startswith(f"INVITE {TARGET_URI} SIP/2.0".encode())
    # PCMU codec in SDP body, not Opus.
    assert b"PCMU/8000" in data
    assert b"opus" not in data.lower()


async def test_second_overlapping_ring_is_ignored(manager):
    first = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    second = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert first is not None
    assert second is None
    transport = manager._transport
    assert len(transport.sent) == 1  # type: ignore[attr-defined]


async def test_call_established_spawns_bridge_and_fires_active(
    manager, fake_bridge, active_calls
):
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    assert call_id is not None
    sip_client = manager._sip_client
    assert sip_client is not None
    local_rtp = sip_client._calls[call_id].local_rtp_port  # type: ignore[union-attr]

    sip_client._on_call_established(  # type: ignore[union-attr]
        CallEstablished(
            call_id=call_id,
            remote_rtp_ip="178.32.84.99",
            remote_rtp_port=20002,
            local_rtp_port=local_rtp,
        )
    )
    # Let the create_task in _handle_call_established run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    fake_bridge.start.assert_awaited_once_with(
        local_rtp_port=local_rtp,
        remote_rtp_ip="178.32.84.99",
        remote_rtp_port=20002,
    )
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
    # No callback fired and the call_id is still tracked (will tear down on BYE).
    assert active_calls == []


async def test_call_terminated_fires_ended_and_clears_active(
    manager, fake_bridge, ended_calls
):
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    sip_client = manager._sip_client

    sip_client._on_call_terminated(call_id)  # type: ignore[union-attr]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    fake_bridge.stop.assert_awaited()
    assert ended_calls == [call_id]
    assert manager.active_call_id is None


async def test_hang_up_stops_bridge(manager, fake_bridge):
    call_id = await manager.start_call(TARGET_URI, SERVER_IP, SIP_USER, SIP_PASS)
    sip_client = manager._sip_client

    # Promote the call to CONFIRMED so hang_up actually sends BYE.
    sip_client._calls[call_id].state = sip_client._calls[call_id].state.CONFIRMED  # type: ignore[union-attr]

    await manager.hang_up()

    fake_bridge.stop.assert_awaited()


async def test_async_stop_closes_transport_and_stops_bridge(
    manager, fake_bridge
):
    transport = manager._transport
    await manager.async_stop()
    assert manager._transport is None
    assert transport._closed is True  # type: ignore[attr-defined]
    fake_bridge.stop.assert_awaited()


async def test_async_stop_is_idempotent(manager, fake_bridge):
    await manager.async_stop()
    await manager.async_stop()  # Must not raise.


# --- port picker ----------------------------------------------------------


def test_pick_rtp_port_returns_free_port(socket_enabled):
    """Verify the port picker against the real socket layer.

    Uses the pytest-socket `socket_enabled` fixture to unblock socket use only
    for this test; the rest of the suite stays sandboxed.
    """
    from custom_components.intratone.call_manager import _pick_rtp_port

    port = _pick_rtp_port()
    assert 16384 <= port < 16484
    assert port % 2 == 0  # RTP convention: even ports
