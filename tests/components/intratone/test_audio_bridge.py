"""AudioBridge tests — RTP protocol + ffmpeg subprocess lifecycle.

We test the `_RtpProtocol` directly with a fake transport (no real sockets,
pytest-socket blocks them) and the ffmpeg lifecycle with mocked subprocess.
"""

from __future__ import annotations

import asyncio
import signal
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.intratone.audio_bridge import (
    _PCMU_PAYLOAD_TYPE,
    _RTP_HEADER_FMT,
    _RTP_HEADER_SIZE,
    _SAMPLES_PER_PACKET,
    _ULAW_SILENCE_BYTE,
    AudioBridge,
    _RtpProtocol,
    _build_rtp_packet,
)


# --- RTP packet builder ----------------------------------------------------


def test_build_rtp_packet_has_correct_header():
    payload = b"\x00" * 160
    pkt = _build_rtp_packet(seq=42, timestamp=12345, ssrc=0xABCDEF12, payload=payload)
    assert len(pkt) == _RTP_HEADER_SIZE + 160
    flags, pt, seq, ts, ssrc = struct.unpack(_RTP_HEADER_FMT, pkt[:_RTP_HEADER_SIZE])
    assert flags == 0b10000000  # V=2
    assert pt == _PCMU_PAYLOAD_TYPE  # PCMU
    assert seq == 42
    assert ts == 12345
    assert ssrc == 0xABCDEF12


def test_build_rtp_packet_wraps_seq_and_ts():
    pkt = _build_rtp_packet(seq=0x10000, timestamp=0x1_0000_0000, ssrc=0, payload=b"")
    _, _, seq, ts, _ = struct.unpack(_RTP_HEADER_FMT, pkt[:_RTP_HEADER_SIZE])
    assert seq == 0
    assert ts == 0


# --- _RtpProtocol --------------------------------------------------------


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


@pytest.fixture
async def rtp_setup():
    payloads: list[bytes] = []
    proto = _RtpProtocol(
        remote_addr=("178.32.84.135", 12345),
        on_ulaw=payloads.append,
        send_keepalives=False,  # keep tests deterministic
    )
    transport = _FakeTransport()
    proto.connection_made(transport)
    yield proto, transport, payloads
    proto.close()


async def test_datagram_received_forwards_ulaw_payload(rtp_setup):
    proto, _, payloads = rtp_setup
    # Build an incoming RTP packet with one µ-law sample (0xFF = silence).
    payload = _ULAW_SILENCE_BYTE * _SAMPLES_PER_PACKET
    pkt = _build_rtp_packet(seq=1, timestamp=160, ssrc=42, payload=payload)
    proto.datagram_received(pkt, ("178.32.84.135", 12345))

    assert proto.packets_received == 1
    # Payload is forwarded as-is — ffmpeg's `-f mulaw` input does the decode.
    assert payloads == [payload]


async def test_datagram_received_drops_too_small_packet(rtp_setup):
    proto, _, payloads = rtp_setup
    proto.datagram_received(b"\x00" * 8, ("178.32.84.135", 12345))
    assert proto.packets_received == 0
    assert payloads == []


async def test_datagram_received_drops_empty_payload(rtp_setup):
    proto, _, payloads = rtp_setup
    pkt = _build_rtp_packet(seq=1, timestamp=0, ssrc=0, payload=b"")
    proto.datagram_received(pkt, ("178.32.84.135", 12345))
    assert proto.packets_received == 0
    assert payloads == []


async def test_keepalive_sends_silence_periodically():
    """Confirm the silence loop emits packets at the configured cadence."""
    proto = _RtpProtocol(
        remote_addr=("178.32.84.135", 12345),
        on_ulaw=lambda _: None,
        send_keepalives=True,
    )
    transport = _FakeTransport()
    with patch(
        "custom_components.intratone.audio_bridge._PACKET_INTERVAL_S", 0.001
    ):
        proto.connection_made(transport)
        # Let the silence loop emit a handful of packets.
        await asyncio.sleep(0.02)
        proto.close()

    assert len(transport.sent) >= 3
    # All packets target the peer endpoint.
    for _, addr in transport.sent:
        assert addr == ("178.32.84.135", 12345)
    # Each packet is RTP header + 160 µ-law silence bytes.
    pkt, _ = transport.sent[0]
    assert len(pkt) == _RTP_HEADER_SIZE + _SAMPLES_PER_PACKET
    flags, pt, _, _, _ = struct.unpack(_RTP_HEADER_FMT, pkt[:_RTP_HEADER_SIZE])
    assert flags == 0b10000000 and pt == _PCMU_PAYLOAD_TYPE
    assert pkt[_RTP_HEADER_SIZE:] == _ULAW_SILENCE_BYTE * _SAMPLES_PER_PACKET
    # Sequence numbers must increment.
    seqs = [
        struct.unpack(_RTP_HEADER_FMT, p[:_RTP_HEADER_SIZE])[2]
        for p, _ in transport.sent
    ]
    assert seqs == sorted(seqs)
    assert proto.packets_sent == len(transport.sent)


async def test_close_cancels_keepalive_task():
    proto = _RtpProtocol(
        remote_addr=("1.2.3.4", 1), on_ulaw=lambda _: None, send_keepalives=True
    )
    proto.connection_made(_FakeTransport())
    task = proto._keepalive_task
    proto.close()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


# --- AudioBridge ffmpeg lifecycle -----------------------------------------


@pytest.fixture
def fake_process():
    """Fake asyncio subprocess with stub streams and clean exit on wait."""
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.is_closing = MagicMock(return_value=False)
    proc.stdin.close = MagicMock()
    proc.stdin.write = MagicMock()
    # stderr reader — empty stream so the drainer task exits cleanly.
    proc.stderr = MagicMock()
    proc.stderr.readline = AsyncMock(return_value=b"")
    proc.send_signal = MagicMock(
        side_effect=lambda sig: setattr(proc, "_last_signal", sig)
    )
    proc.kill = MagicMock()

    async def _wait():
        proc.returncode = 0
        return 0

    proc.wait = AsyncMock(side_effect=_wait)
    return proc


@pytest.fixture
def mock_subprocess(fake_process):
    with patch(
        "custom_components.intratone.audio_bridge.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_process),
    ) as m:
        yield m


@pytest.fixture
def fake_datagram_endpoint():
    """Patches asyncio loop.create_datagram_endpoint to skip socket binding."""
    transport = _FakeTransport()

    async def _create(protocol_factory, **_kwargs):
        proto = protocol_factory()
        proto.connection_made(transport)
        return transport, proto

    loop = asyncio.get_event_loop()
    with patch.object(loop, "create_datagram_endpoint", side_effect=_create):
        yield transport


async def test_start_returns_rtsp_url(mock_subprocess, fake_datagram_endpoint):
    bridge = AudioBridge(rtsp_port=8556, rtsp_path="intratone")
    url = await bridge.start(
        local_rtp_port=16384,
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    assert url == "rtsp://127.0.0.1:8556/intratone"
    assert bridge.is_running


async def test_start_spawns_ffmpeg_with_mulaw_stdin_input(
    mock_subprocess, fake_datagram_endpoint
):
    bridge = AudioBridge(ffmpeg_binary="ffmpeg")
    await bridge.start(
        local_rtp_port=16384, remote_rtp_ip="178.32.84.135", remote_rtp_port=20000
    )
    args = mock_subprocess.call_args.args
    binary, *rest = args
    assert binary == "ffmpeg"
    joined = " ".join(rest)
    # ffmpeg decodes µ-law itself, no Python audioop needed.
    assert "-f mulaw -ar 8000 -ac 1 -i pipe:0" in joined
    assert "-c:a libopus" in joined
    assert "-rtsp_flags listen" in joined
    assert "rtsp://0.0.0.0:8556/intratone" in joined


async def test_start_is_idempotent(mock_subprocess, fake_datagram_endpoint):
    bridge = AudioBridge()
    await bridge.start(
        local_rtp_port=16384, remote_rtp_ip="178.32.84.135", remote_rtp_port=20000
    )
    await bridge.start(
        local_rtp_port=16384, remote_rtp_ip="178.32.84.135", remote_rtp_port=20000
    )
    assert mock_subprocess.call_count == 1


async def test_received_rtp_forwards_ulaw_to_ffmpeg_stdin(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    bridge = AudioBridge()
    await bridge.start(
        local_rtp_port=16384, remote_rtp_ip="178.32.84.135", remote_rtp_port=20000
    )
    assert bridge._rtp is not None
    payload = _ULAW_SILENCE_BYTE * _SAMPLES_PER_PACKET
    pkt = _build_rtp_packet(seq=1, timestamp=160, ssrc=42, payload=payload)
    bridge._rtp.datagram_received(pkt, ("178.32.84.135", 20000))

    fake_process.stdin.write.assert_called_once()
    written = fake_process.stdin.write.call_args.args[0]
    # µ-law forwarded as-is (no Python decode).
    assert written == payload


async def test_stop_sends_sigterm_and_closes_socket(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    bridge = AudioBridge()
    await bridge.start(
        local_rtp_port=16384, remote_rtp_ip="178.32.84.135", remote_rtp_port=20000
    )
    transport = bridge._rtp_transport
    assert transport is not None

    await bridge.stop()

    assert getattr(fake_process, "_last_signal") == signal.SIGTERM
    fake_process.wait.assert_awaited()
    assert transport.closed is True
    assert not bridge.is_running


async def test_stop_kills_if_ffmpeg_hangs(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    bridge = AudioBridge()
    await bridge.start(
        local_rtp_port=16384, remote_rtp_ip="178.32.84.135", remote_rtp_port=20000
    )
    waits = [asyncio.get_running_loop().create_future()]
    call_count = 0

    async def wait_impl():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return await waits[0]  # cancelled by wait_for timeout
        return 0

    fake_process.wait = wait_impl
    with patch(
        "custom_components.intratone.audio_bridge._FFMPEG_TERMINATE_TIMEOUT", 0.01
    ):
        await bridge.stop()
    fake_process.kill.assert_called_once()


async def test_stop_safe_when_never_started():
    bridge = AudioBridge()
    await bridge.stop()  # must not raise
    assert not bridge.is_running


async def test_stop_safe_when_called_twice(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    bridge = AudioBridge()
    await bridge.start(
        local_rtp_port=16384, remote_rtp_ip="178.32.84.135", remote_rtp_port=20000
    )
    await bridge.stop()
    await bridge.stop()  # idempotent — no second SIGTERM
    assert fake_process.send_signal.call_count == 1


async def test_bind_failure_kills_ffmpeg(mock_subprocess, fake_process):
    """If the RTP bind fails, the ffmpeg subprocess must not be orphaned."""

    async def _create_fails(_protocol_factory, **_kwargs):
        raise OSError("Address already in use")

    bridge = AudioBridge()
    loop = asyncio.get_event_loop()
    with patch.object(loop, "create_datagram_endpoint", side_effect=_create_fails):
        with pytest.raises(OSError):
            await bridge.start(
                local_rtp_port=16384,
                remote_rtp_ip="178.32.84.135",
                remote_rtp_port=20000,
            )
    fake_process.kill.assert_called_once()
    assert bridge._process is None
    assert not bridge.is_running


async def test_stderr_is_drained_to_logger(
    mock_subprocess, fake_process, fake_datagram_endpoint, caplog
):
    """ffmpeg stderr is read line-by-line and logged so the pipe never fills."""
    import logging

    lines = iter(
        [b"Stream mapping:\n", b"Output #0, rtsp\n", b""]  # empty = EOF
    )
    fake_process.stderr.readline = AsyncMock(side_effect=lambda: next(lines))

    bridge = AudioBridge()
    with caplog.at_level(logging.WARNING, logger="custom_components.intratone.audio_bridge"):
        await bridge.start(
            local_rtp_port=16384,
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
        )
        # Yield to let the drainer task consume the stub output.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    drained = [r.message for r in caplog.records if "ffmpeg:" in r.message]
    assert any("Stream mapping" in m for m in drained)
    assert any("Output #0" in m for m in drained)
    await bridge.stop()
