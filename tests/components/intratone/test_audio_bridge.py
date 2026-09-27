"""AudioBridge tests — RTP protocol + ffmpeg subprocess lifecycle.

We test the `_RtpProtocol` directly with a fake transport (no real sockets,
pytest-socket blocks them) and the ffmpeg lifecycle with mocked subprocess.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import logging
import signal
import socket
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.intratone import audio_bridge as bridge_mod
from custom_components.intratone.audio_bridge import (
    _PCMU_PAYLOAD_TYPE,
    _RTCP_PLI_PT,
    _RTP_HEADER_FMT,
    _RTP_HEADER_SIZE,
    _SAMPLES_PER_PACKET,
    _ULAW_SILENCE_BYTE,
    _VP8_PLACEHOLDER_KEYFRAME,
    _VP8_PLACEHOLDER_PACKETS,
    AudioBridge,
    BridgeStoppedError,
    _pick_free_udp_port,
    _RtpProtocol,
    _VideoRtcpProtocol,
    _VideoRtpProtocol,
    _build_pli_packet,
    _build_rtp_packet,
    _is_vp8_keyframe,
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


def _fake_rtp_socket(port: int) -> MagicMock:
    """A mock socket that reports a bound port without actually binding."""
    sock = MagicMock()
    sock.getsockname = MagicMock(return_value=("0.0.0.0", port))
    sock.close = MagicMock()
    return sock


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
    # stderr reader emits the marker that `start()` waits for (push to go2rtc
    # established), then EOF — so the drainer task exits cleanly and start()
    # doesn't wait its 5s timeout.
    stderr_lines = iter([b"Output #0, rtsp, to 'rtsp://test':\n", b""])
    proc.stderr = MagicMock()
    proc.stderr.readline = AsyncMock(side_effect=lambda: next(stderr_lines))
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
    bridge = AudioBridge(rtsp_relay_url="rtsp://127.0.0.1:8554", rtsp_path="intratone")
    url = await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    assert url == "rtsp://127.0.0.1:8554/intratone"
    assert bridge.is_running
    await bridge.stop()


async def test_start_spawns_ffmpeg_with_mulaw_stdin_input(
    mock_subprocess, fake_datagram_endpoint
):
    bridge = AudioBridge(ffmpeg_binary="ffmpeg")
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000
    )
    args = mock_subprocess.call_args.args
    binary, *rest = args
    assert binary == "ffmpeg"
    joined = " ".join(rest)
    # ffmpeg decodes µ-law itself (no Python audioop needed) and synthesizes
    # a dark placeholder video so HomeKit's Camera service has something to map.
    assert "-f mulaw -ar 8000 -ac 1 -i pipe:0" in joined
    assert "-f lavfi -i color=" in joined
    assert "-c:a libopus" in joined
    assert "-c:v libx264" in joined
    # ffmpeg pushes to go2rtc relay over TCP RTSP (no listen flag — that mode
    # is broken in recent builds, doesn't actually listen).
    assert "-rtsp_transport tcp" in joined
    assert "-f rtsp" in joined
    assert "rtsp://127.0.0.1:8554/intratone" in joined
    await bridge.stop()


async def test_start_is_idempotent(mock_subprocess, fake_datagram_endpoint):
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000
    )
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000
    )
    assert mock_subprocess.call_count == 1
    await bridge.stop()


async def test_relay_status_reported_true_on_push_success(
    mock_subprocess, fake_datagram_endpoint
):
    """A successful ANNOUNCE to go2rtc fires on_relay_status(True) so the
    integration can clear a previously raised relay repair issue."""
    statuses: list[bool] = []
    bridge = AudioBridge(on_relay_status=statuses.append)
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    assert statuses == [True]
    await bridge.stop()


async def test_relay_status_reported_false_when_ffmpeg_dies_before_push(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    """go2rtc down → ffmpeg exits before emitting the push marker →
    on_relay_status(False) so a repair issue can be raised."""
    from custom_components.intratone import audio_bridge as bridge_mod

    # stderr EOFs immediately (no `Output #0, rtsp` marker) and the process
    # is already dead by the time start() checks it.
    fake_process.stderr.readline = AsyncMock(return_value=b"")
    fake_process.returncode = 1

    statuses: list[bool] = []
    bridge = AudioBridge(on_relay_status=statuses.append)
    with patch.object(bridge_mod, "_FFMPEG_PUSH_READY_TIMEOUT_S", 0.05):
        await bridge.start(
            rtp_socket=_fake_rtp_socket(16384),
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
        )
    assert statuses == [False]
    await bridge.stop()


async def test_received_rtp_forwards_ulaw_to_ffmpeg_stdin(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000
    )
    assert bridge._rtp is not None
    payload = _ULAW_SILENCE_BYTE * _SAMPLES_PER_PACKET
    pkt = _build_rtp_packet(seq=1, timestamp=160, ssrc=42, payload=payload)
    bridge._rtp.datagram_received(pkt, ("178.32.84.135", 20000))

    fake_process.stdin.write.assert_called_once()
    written = fake_process.stdin.write.call_args.args[0]
    # µ-law forwarded as-is (no Python decode).
    assert written == payload
    await bridge.stop()


async def test_stop_sends_sigterm_and_closes_socket(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000
    )
    transport = bridge._rtp_transport
    assert transport is not None

    await bridge.stop()

    assert fake_process._last_signal == signal.SIGTERM
    fake_process.wait.assert_awaited()
    assert transport.closed is True
    assert not bridge.is_running


async def test_stop_kills_if_ffmpeg_hangs(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000
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
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000
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
    rtp_sock = _fake_rtp_socket(16384)
    with patch.object(loop, "create_datagram_endpoint", side_effect=_create_fails):
        with pytest.raises(OSError):
            await bridge.start(
                rtp_socket=rtp_sock,
                remote_rtp_ip="178.32.84.135",
                remote_rtp_port=20000,
            )
    fake_process.kill.assert_called_once()
    rtp_sock.close.assert_called_once()
    assert bridge._process is None
    assert not bridge.is_running


async def test_stderr_is_drained_to_logger(
    mock_subprocess, fake_process, fake_datagram_endpoint, caplog
):
    """ffmpeg stderr is read line-by-line and logged so the pipe never fills.

    Routine ffmpeg lines (Stream mapping, Output #N, frame= progress) go to
    DEBUG; only lines containing Error/Invalid/Failed/Broken/fatal get WARNING.
    """
    import logging

    lines = iter(
        [b"Stream mapping:\n", b"Output #0, rtsp\n", b""]  # empty = EOF
    )
    fake_process.stderr.readline = AsyncMock(side_effect=lambda: next(lines))

    bridge = AudioBridge()
    with caplog.at_level(logging.DEBUG, logger="custom_components.intratone.audio_bridge"):
        await bridge.start(
            rtp_socket=_fake_rtp_socket(16384),
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
        )
        # Yield to let the drainer task consume the stub output.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    drained = [
        r.message for r in caplog.records
        if "ffmpeg:" in r.message or "FFMPEG_STARTUP" in r.message
    ]
    assert any("Stream mapping" in m for m in drained)
    assert any("Output #0" in m for m in drained)
    await bridge.stop()


# --- start()/stop() races ---------------------------------------------------


async def test_stop_during_push_ready_wait_aborts_start_cleanly(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    """stop() while start() is parked on the ffmpeg push-ready wait: the old
    code nulled `_ffmpeg_push_ready` under the waiter and let start() resume
    against torn-down state (creating a stats task after stop() returned).
    start() must wake immediately and abort instead of reporting a stopped
    bridge as consumable."""
    emitted = False

    async def readline():
        nonlocal emitted
        if not emitted:
            emitted = True
            return b"Stream mapping:\n"  # never the `Output #0, rtsp` marker
        await asyncio.Event().wait()  # park forever (cancelled by stop)

    fake_process.stderr.readline = readline
    bridge = AudioBridge()
    start_task = asyncio.create_task(
        bridge.start(
            rtp_socket=_fake_rtp_socket(16384),
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
        )
    )
    # Let start() progress to the push-ready wait (RTP endpoint wrapped).
    for _ in range(20):
        await asyncio.sleep(0)
        if bridge._rtp_transport is not None:
            break
    assert not start_task.done()

    await bridge.stop()

    with pytest.raises(BridgeStoppedError):
        await asyncio.wait_for(start_task, timeout=1)
    assert bridge._stats_task is None
    assert not bridge.is_running


async def test_stop_between_endpoint_wraps_leaves_no_orphans(
    mock_subprocess, fake_process
):
    """stop() while start() is suspended wrapping the video endpoint: the
    resumed start() used to assign fresh video transports and spawn the PLI
    task AFTER stop() had snapshotted-and-closed everything — leaking UDP
    transports plus a PLI loop pinging the old gateway every 30 s forever."""
    transports: list[_FakeTransport] = []
    gate = asyncio.Event()
    wrap_calls = 0

    async def _create(protocol_factory, **_kwargs):
        nonlocal wrap_calls
        wrap_calls += 1
        if wrap_calls == 2:  # video RTP wrap — stop() lands here
            await gate.wait()
        proto = protocol_factory()
        transport = _FakeTransport()
        transports.append(transport)
        proto.connection_made(transport)
        return transport, proto

    loop = asyncio.get_event_loop()
    bridge = AudioBridge()
    with (
        patch.object(loop, "create_datagram_endpoint", side_effect=_create),
        _patch_video_port(),
    ):
        start_task = asyncio.create_task(
            bridge.start(
                rtp_socket=_fake_rtp_socket(16384),
                remote_rtp_ip="178.32.84.135",
                remote_rtp_port=20000,
                video_socket=_fake_rtp_socket(16386),
                remote_video_rtp_ip="178.32.84.135",
                remote_video_rtp_port=52982,
                video_rtcp_socket=_fake_rtp_socket(16387),
            )
        )
        for _ in range(50):
            await asyncio.sleep(0)
            if wrap_calls == 2:
                break
        assert wrap_calls == 2

        await bridge.stop()
        gate.set()
        with pytest.raises(BridgeStoppedError):
            await asyncio.wait_for(start_task, timeout=1)

    assert all(t.closed for t in transports)
    assert bridge._pli_task is None
    assert bridge._video_rtp_transport is None
    assert bridge._video_rtcp_transport is None
    assert not bridge.is_running


async def test_stop_survives_sigterm_process_lookup_error(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    """SIGTERM can race the child watcher's reap (ProcessLookupError). stop()
    must swallow it like _discard_prewarm does — otherwise _teardown_bridge
    aborts before on_call_ended fires and the coordinator/camera stay stuck
    'active'."""
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    fake_process.send_signal = MagicMock(side_effect=ProcessLookupError)

    await bridge.stop()  # must not raise

    assert not bridge.is_running


async def test_stop_kill_fallback_survives_process_lookup_error(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    """Same reap race on the kill() fallback after the SIGTERM grace expires."""
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    hang = asyncio.get_running_loop().create_future()
    call_count = 0

    async def wait_impl():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return await hang  # SIGTERM grace: process never exits
        return 0

    fake_process.wait = wait_impl
    fake_process.kill = MagicMock(side_effect=ProcessLookupError)
    with patch(
        "custom_components.intratone.audio_bridge._FFMPEG_TERMINATE_TIMEOUT", 0.01
    ):
        await bridge.stop()  # must not raise

    fake_process.kill.assert_called_once()


async def test_video_rtp_wrap_failure_is_fatal(mock_subprocess, fake_process):
    """A failed video RTP wrap cannot degrade to audio-only: ffmpeg was
    spawned with `-f sdp -i data:…` + `-map 1:v`, so without VP8 packets the
    RTSP muxer never initializes and NO media (audio included) reaches
    go2rtc. start() must clean up and raise so CallManager hangs up instead
    of publishing a dead stream after the 15 s push-ready timeout."""
    wrap_calls = 0

    async def _create(protocol_factory, **_kwargs):
        nonlocal wrap_calls
        wrap_calls += 1
        if wrap_calls == 2:  # video RTP wrap
            raise OSError("Address already in use")
        proto = protocol_factory()
        transport = _FakeTransport()
        proto.connection_made(transport)
        return transport, proto

    loop = asyncio.get_event_loop()
    video_sock = _fake_rtp_socket(16386)
    rtcp_sock = _fake_rtp_socket(16387)
    bridge = AudioBridge()
    with (
        patch.object(loop, "create_datagram_endpoint", side_effect=_create),
        _patch_video_port(),
    ):
        with pytest.raises(OSError):
            await bridge.start(
                rtp_socket=_fake_rtp_socket(16384),
                remote_rtp_ip="178.32.84.135",
                remote_rtp_port=20000,
                video_socket=video_sock,
                remote_video_rtp_ip="178.32.84.135",
                remote_video_rtp_port=52982,
                video_rtcp_socket=rtcp_sock,
            )

    fake_process.kill.assert_called_once()
    video_sock.close.assert_called_once()
    rtcp_sock.close.assert_called_once()
    assert not bridge.is_running


async def test_cancel_start_during_prewarm_consume_reattaches_prewarm():
    """CallManager cancels an in-flight start() when the call is superseded.
    If the cancel lands while start() awaits the still-spawning prewarm task,
    `_consume_prewarm` has already popped `_prewarm_task` to None — the
    prewarm must be re-attached on the way out, or its live ffmpeg keeps
    running unreferenced and the follow-up stop() finds nothing to reap."""
    proc = _make_fake_process()
    spawn_gate = asyncio.Event()

    async def slow_spawn(*_args, **_kwargs):
        await spawn_gate.wait()
        return proc

    with (
        patch(
            "custom_components.intratone.audio_bridge.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=slow_spawn),
        ),
        _patch_video_port(),
    ):
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await asyncio.sleep(0)  # prewarm task now parked inside _spawn_ffmpeg

        start_task = asyncio.create_task(
            bridge.start(
                rtp_socket=_fake_rtp_socket(16384),
                remote_rtp_ip="178.32.84.135",
                remote_rtp_port=20000,
            )
        )
        await asyncio.sleep(0)  # start() now awaiting _consume_prewarm

        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

        # Let the (still running) prewarm spawn finish...
        spawn_gate.set()
        await asyncio.sleep(0)
        # ...it must still be tracked so stop() can reap its ffmpeg.
        assert bridge._prewarm_task is not None
        await bridge.stop()
    proc.kill.assert_called_once()


async def test_cancel_start_during_push_ready_wait_closes_sockets(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    """A raw cancel of start() (call superseded / entry unload) is not a
    stop() race, so the generation checkpoints never fire — start() must
    still close the sockets that live only in its locals, reap ffmpeg, and
    leave no stats/PLI task behind before letting the cancel propagate."""
    emitted = False

    async def readline():
        nonlocal emitted
        if not emitted:
            emitted = True
            return b"Stream mapping:\n"  # never the `Output #0, rtsp` marker
        await asyncio.Event().wait()  # park forever (cancelled by the unwind)

    fake_process.stderr.readline = readline
    rtp_sock = _fake_rtp_socket(16384)
    bridge = AudioBridge()
    start_task = asyncio.create_task(
        bridge.start(
            rtp_socket=rtp_sock,
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
        )
    )
    # Let start() progress to the push-ready wait (RTP endpoint wrapped).
    for _ in range(20):
        await asyncio.sleep(0)
        if bridge._rtp_transport is not None:
            break
    assert not start_task.done()

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    rtp_sock.close.assert_called()
    fake_process.kill.assert_called_once()
    assert bridge._stats_task is None
    assert bridge._pli_task is None
    assert bridge._process is None
    assert not bridge.is_running


async def test_cancelled_stop_propagates_cancellation(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    """A stop() that is itself cancelled while awaiting the stderr drainer
    must not report normal completion — CancelledError has to propagate so
    the caller knows teardown didn't finish."""
    release = asyncio.Event()
    drainer_cancelled = asyncio.Event()

    async def readline():
        try:
            await asyncio.Event().wait()  # no output → push-ready times out
        except asyncio.CancelledError:
            drainer_cancelled.set()
            await release.wait()  # drainer slow to unwind
            raise

    fake_process.stderr.readline = readline
    bridge = AudioBridge()
    with patch(
        "custom_components.intratone.audio_bridge._FFMPEG_PUSH_READY_TIMEOUT_S",
        0.01,
    ):
        await bridge.start(
            rtp_socket=_fake_rtp_socket(16384),
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
        )

    stop_task = asyncio.create_task(bridge.stop())
    # stop() cancels the drainer then awaits it — cancel stop() at that point.
    await asyncio.wait_for(drainer_cancelled.wait(), timeout=1)
    stop_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop_task


# --- AudioBridge.prewarm ---------------------------------------------------


def _make_fake_process():
    """Standalone fake ffmpeg process — unlike the `fake_process` fixture,
    several can coexist in one test (prewarm + on-demand respawn)."""
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.is_closing = MagicMock(return_value=False)
    proc.stdin.close = MagicMock()
    proc.stdin.write = MagicMock()
    stderr_lines = iter([b"Output #0, rtsp, to 'rtsp://test':\n", b""])
    proc.stderr = MagicMock()
    proc.stderr.readline = AsyncMock(side_effect=lambda: next(stderr_lines))
    proc.send_signal = MagicMock(
        side_effect=lambda sig: setattr(proc, "_last_signal", sig)
    )
    proc.kill = MagicMock()

    async def _wait():
        proc.returncode = 0
        return 0

    proc.wait = AsyncMock(side_effect=_wait)
    return proc


def _patch_spawn(processes):
    return patch(
        "custom_components.intratone.audio_bridge.asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=processes),
    )


def _patch_video_port(port: int = 55555):
    return patch(
        "custom_components.intratone.audio_bridge._pick_free_udp_port",
        return_value=port,
    )


async def _await_prewarm(bridge) -> None:
    """Wait for the prewarm task to finish spawning ffmpeg — deterministic,
    unlike counting bare event-loop ticks."""
    await asyncio.wait_for(asyncio.shield(bridge._prewarm_task), timeout=5)


async def test_video_input_is_an_inline_sdp_not_a_temp_file():
    """The SDP describing the loopback VP8 stream is passed inline as a
    `data:` URL: no disk I/O in the call setup path, nothing to clean up or
    leak. ffmpeg must be allowed the `data` protocol to read it."""
    with _patch_spawn([_make_fake_process()]) as spawn, _patch_video_port(55555):
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await _await_prewarm(bridge)
        args = list(spawn.await_args.args[1:])
        await bridge.stop()

    assert args[args.index("-protocol_whitelist") + 1] == "data,udp,rtp"
    sdp_input = args[args.index("sdp") + 2]  # `-f sdp -i <url>`
    prefix = "data:application/sdp;base64,"
    assert sdp_input.startswith(prefix)
    sdp = base64.b64decode(sdp_input.removeprefix(prefix)).decode()
    assert "c=IN IP4 127.0.0.1\r\n" in sdp
    assert "m=video 55555 RTP/AVP 96\r\n" in sdp
    assert "a=rtpmap:96 VP8/90000\r\n" in sdp


async def test_prewarm_ffmpeg_is_reused_by_video_start(fake_datagram_endpoint):
    """prewarm() spawns the video-SDP ffmpeg during SIP negotiation; a video
    start() must reuse that process instead of spawning a second one."""
    with _patch_spawn([_make_fake_process()]) as spawn, _patch_video_port():
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await _await_prewarm(bridge)
        assert spawn.await_count == 1
        args = " ".join(spawn.await_args.args[1:])
        assert "-f sdp" in args  # video variant, not the lavfi placeholder

        url = await bridge.start(
            rtp_socket=_fake_rtp_socket(16384),
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
            video_socket=_fake_rtp_socket(16386),
            remote_video_rtp_ip="178.32.84.135",
            remote_video_rtp_port=52982,
            video_rtcp_socket=_fake_rtp_socket(16387),
        )
        assert url == bridge.rtsp_url
        assert bridge.is_running
        assert spawn.await_count == 1  # no respawn — prewarmed process reused
        await bridge.stop()


async def test_prewarm_discarded_when_server_rejects_video(fake_datagram_endpoint):
    """If the 200 OK carries no video, the prewarmed video-SDP ffmpeg must be
    killed and the lavfi-placeholder variant spawned instead."""
    prewarm_proc = _make_fake_process()
    call_proc = _make_fake_process()
    with (
        _patch_spawn([prewarm_proc, call_proc]) as spawn,
        _patch_video_port(),
    ):
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await _await_prewarm(bridge)

        url = await bridge.start(
            rtp_socket=_fake_rtp_socket(16384),
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
        )
        assert url == bridge.rtsp_url
        assert spawn.await_count == 2
        prewarm_proc.kill.assert_called_once()
        second_args = " ".join(spawn.await_args_list[1].args[1:])
        assert "-f lavfi" in second_args
        assert "-f sdp" not in second_args
        await bridge.stop()


async def test_stop_discards_unused_prewarm():
    """A call that dies before 200 OK never consumes the prewarm — stop()
    must kill the process."""
    proc = _make_fake_process()
    with (
        _patch_spawn([proc]),
        _patch_video_port(),
    ):
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await _await_prewarm(bridge)
        await bridge.stop()

    proc.kill.assert_called_once()
    assert not bridge.is_running
    assert bridge._prewarm_task is None


async def test_cancel_prewarm_kills_process_without_stop():
    """`cancel_prewarm()` (sync, callable from SIP callbacks) must reap the
    unadopted ffmpeg promptly instead of leaving it to idle until the 60 s
    post-BYE grace teardown finally calls stop()."""
    proc = _make_fake_process()
    with _patch_spawn([proc]), _patch_video_port():
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await asyncio.sleep(0)
        bridge.cancel_prewarm()
        assert bridge._prewarm_task is None
        # The reap is a background task that first awaits the in-flight
        # prewarm — await it rather than spinning on bare event-loop ticks.
        await bridge._prewarm_cleanup_task
        proc.kill.assert_called_once()
        await bridge.stop()  # still safe afterwards


async def test_prewarm_failure_falls_back_to_normal_spawn(fake_datagram_endpoint):
    """A failed prewarm (ffmpeg missing, spawn error) must not break the call:
    start() falls back to the on-demand spawn path."""
    proc = _make_fake_process()
    with (
        _patch_spawn([FileNotFoundError("no ffmpeg"), proc]) as spawn,
        _patch_video_port(),
    ):
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await asyncio.sleep(0)
        url = await bridge.start(
            rtp_socket=_fake_rtp_socket(16384),
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
        )
        assert url == bridge.rtsp_url
        assert bridge.is_running
        assert spawn.await_count == 2
        await bridge.stop()


async def test_prewarm_noop_when_bridge_already_running(
    mock_subprocess, fake_datagram_endpoint
):
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    bridge.prewarm(video=True)
    assert bridge._prewarm_task is None
    assert mock_subprocess.await_count == 1
    await bridge.stop()


# --- PLI helpers ---------------------------------------------------------


def test_build_pli_packet_matches_rfc_4585_format():
    """RFC 4585 §6.3.1 PLI = V=2, P=0, FMT=1, PT=206, length=2 (12 bytes total),
    followed by sender SSRC and media source SSRC."""
    pkt = _build_pli_packet(sender_ssrc=0x11223344, media_ssrc=0xAABBCCDD)
    assert len(pkt) == 12
    b0, b1, length, sender, media = struct.unpack(">BBHII", pkt)
    assert (b0 >> 6) & 0x03 == 2  # V=2
    assert (b0 >> 5) & 0x01 == 0  # P=0
    assert b0 & 0x1F == 1  # FMT=1 (PLI)
    assert b1 == _RTCP_PLI_PT  # 206 = PSFB
    assert length == 2
    assert sender == 0x11223344
    assert media == 0xAABBCCDD


def test_is_vp8_keyframe_detects_keyframe():
    """S=1, PID=0, no extensions, frame tag byte 0 LSB=0 → keyframe."""
    payload = bytes([0x10, 0x00, 0x00, 0x00])  # desc S=1 PID=0; frame tag KF
    assert _is_vp8_keyframe(payload) is True


def test_is_vp8_keyframe_rejects_interframe():
    """Same shape but frame tag LSB=1 → interframe."""
    payload = bytes([0x10, 0x01, 0x00, 0x00])
    assert _is_vp8_keyframe(payload) is False


def test_is_vp8_keyframe_rejects_non_start_of_frame():
    """S=0 means continuation packet — never a keyframe start."""
    payload = bytes([0x00, 0x00, 0x00, 0x00])  # S=0
    assert _is_vp8_keyframe(payload) is False


def test_is_vp8_keyframe_handles_payload_descriptor_extension():
    """X=1 → 1 ext byte; I=1, M=0 → 1 PictureID byte. Skip 2 extra bytes then
    look at the frame tag."""
    # desc: X=1, S=1, PID=0 → 0x90; ext: I=1, others=0 → 0x80; PictureID=0x42;
    # frame tag byte 0 with LSB=0 → 0x00
    payload = bytes([0x90, 0x80, 0x42, 0x00, 0x00, 0x00])
    assert _is_vp8_keyframe(payload) is True


def test_is_vp8_keyframe_truncated_payload_returns_false():
    assert _is_vp8_keyframe(b"") is False
    assert _is_vp8_keyframe(b"\x10") is False  # only descriptor


# --- _VideoRtcpProtocol --------------------------------------------------


async def test_video_rtcp_send_pli_writes_to_transport():
    proto = _VideoRtcpProtocol(remote_addr=("178.32.84.135", 52983))
    transport = _FakeTransport()
    proto.connection_made(transport)

    proto.send_pli(media_ssrc=0x12345678)

    assert proto.pli_sent == 1
    assert len(transport.sent) == 1
    data, addr = transport.sent[0]
    assert addr == ("178.32.84.135", 52983)
    assert len(data) == 12
    # PT byte = 206 (PSFB)
    assert data[1] == _RTCP_PLI_PT
    # Last 4 bytes = media SSRC
    media_ssrc = int.from_bytes(data[8:12], "big")
    assert media_ssrc == 0x12345678


async def test_video_rtcp_send_pli_noop_when_no_transport():
    proto = _VideoRtcpProtocol(remote_addr=("x", 1))
    proto.send_pli(media_ssrc=0)  # transport is None → no crash
    assert proto.pli_sent == 0


# --- _VideoRtpProtocol keyframe detection --------------------------------


async def test_video_rtp_protocol_marks_keyframe_received():
    """Feed a synthetic VP8 keyframe RTP packet and verify the flag flips."""
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    proto.connection_made(_FakeTransport())

    # 12-byte RTP header + VP8 payload that decodes as a keyframe
    rtp_header = struct.pack(
        ">BBHII",
        0b10000000,  # V=2
        96,  # PT=96 (VP8 dynamic)
        1,  # seq
        0,  # ts
        0xDEADBEEF,  # ssrc
    )
    vp8_payload = bytes([0x10, 0x00, 0x00, 0x00])  # S=1, PID=0, KF
    proto.datagram_received(rtp_header + vp8_payload, ("178.32.84.135", 52982))

    assert proto.keyframe_received is True
    assert proto.first_keyframe_at is not None
    assert proto.first_rtp_at is not None
    assert proto.first_rtp_event.is_set()
    assert proto.keyframe_event.is_set()


async def test_video_rtp_protocol_keyframe_flag_sticks_after_interframe():
    """Once keyframe_received is True, subsequent interframes don't clear it."""
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    proto.connection_made(_FakeTransport())

    rtp_header = struct.pack(">BBHII", 0b10000000, 96, 1, 0, 0xDEADBEEF)
    # First a keyframe, then an interframe
    proto.datagram_received(
        rtp_header + bytes([0x10, 0x00, 0x00, 0x00]), ("x", 1)
    )
    proto.datagram_received(
        rtp_header + bytes([0x10, 0x01, 0x00, 0x00]), ("x", 1)
    )
    assert proto.keyframe_received is True


_VP8_RTP_HEADER = struct.pack(">BBHII", 0b10000000, 96, 1, 0, 0xDEADBEEF)
_VP8_KEYFRAME_PKT = _VP8_RTP_HEADER + bytes([0x10, 0x00, 0x00, 0x00])
_VP8_INTERFRAME_PKT = _VP8_RTP_HEADER + bytes([0x10, 0x01, 0x00, 0x00])


async def test_video_rtp_gate_drops_interframes_until_keyframe():
    """Pre-keyframe P-frames must NOT reach ffmpeg (VP8 decoder would enter a
    stuck error state); the keyframe itself and everything after must be
    forwarded to the ffmpeg loopback target."""
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    transport = _FakeTransport()
    proto.connection_made(transport)

    proto.datagram_received(_VP8_INTERFRAME_PKT, ("x", 1))
    assert proto.rtp_packets_forwarded == 0
    assert transport.sent == []

    proto.datagram_received(_VP8_KEYFRAME_PKT, ("x", 1))
    proto.datagram_received(_VP8_INTERFRAME_PKT, ("x", 1))
    assert proto.rtp_packets_forwarded == 2
    assert [addr for _, addr in transport.sent] == [("127.0.0.1", 12345)] * 2


# --- VP8 placeholder until the real video arrives ---------------------------


def _vp8_pkt(seq: int, ts: int, payload: bytes, marker: bool = True) -> bytes:
    return (
        struct.pack(
            ">BBHII", 0b10000000, (0x80 if marker else 0) | 96, seq, ts, 0xDEADBEEF
        )
        + payload
    )


def _parse_rtp(pkt: bytes) -> tuple[int, int, int, int, int, bytes]:
    """(pt, marker, seq, ts, ssrc, payload) of an RTP packet."""
    _, b1, seq, ts, ssrc = struct.unpack(">BBHII", pkt[:12])
    return b1 & 0x7F, b1 >> 7, seq, ts, ssrc, pkt[12:]


_PLACEHOLDER_PKTS = len(_VP8_PLACEHOLDER_PACKETS)  # RTP packets per frame
_REAL_KEYFRAME = bytes([0x10, 0x00, 0x00, 0x00, 0xAA])  # S=1, PID=0, key
_REAL_CONTINUATION = bytes([0x00, 0xBB, 0xCC])  # S=0: rest of the same frame
_REAL_INTERFRAME = bytes([0x10, 0x01, 0x00, 0x00, 0xDD])


def _placeholder_patches(interval: float = 0.001, resume: float = 10.0):
    return (
        patch(
            "custom_components.intratone.audio_bridge._VIDEO_PLACEHOLDER_INTERVAL_S",
            interval,
        ),
        patch(
            "custom_components.intratone.audio_bridge._VIDEO_PLACEHOLDER_RESUME_S",
            resume,
        ),
    )


def test_vp8_placeholder_is_a_640x480_keyframe():
    """RFC 6386 §9.1: keyframe tag, start code 9d 01 2a, then 14-bit width /
    height — must match the ffmpeg output canvas so it is never rescaled."""
    frame = _VP8_PLACEHOLDER_KEYFRAME
    assert frame[0] & 0x01 == 0  # keyframe
    assert frame[3:6] == b"\x9d\x01\x2a"
    width = int.from_bytes(frame[6:8], "little") & 0x3FFF
    height = int.from_bytes(frame[8:10], "little") & 0x3FFF
    assert (width, height) == (640, 480)


def test_vp8_placeholder_is_packetized_per_rfc_7741():
    """The frame is split into MTU-safe packets: S=1 (start of partition) on
    the first only, PID=0, and the payloads reassemble into the frame."""
    pkts = _VP8_PLACEHOLDER_PACKETS
    assert _PLACEHOLDER_PKTS > 1
    assert _is_vp8_keyframe(pkts[0])
    assert [p[0] for p in pkts] == [0x10] + [0x00] * (_PLACEHOLDER_PKTS - 1)
    assert all(len(p) + _RTP_HEADER_SIZE <= 1300 for p in pkts)
    assert b"".join(p[1:] for p in pkts) == _VP8_PLACEHOLDER_KEYFRAME


async def test_video_placeholder_feeds_ffmpeg_until_real_video():
    """With no VP8 from the gateway, ffmpeg still gets a valid video stream:
    the placeholder keyframe, one per tick, as a continuous RTP stream. This
    is what lets ffmpeg publish (audio included) without the real video."""
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    transport = _FakeTransport()
    proto.connection_made(transport)
    interval, resume = _placeholder_patches()
    with interval, resume:
        proto.start_placeholder()
        # First frame goes out immediately.
        assert len(transport.sent) == _PLACEHOLDER_PKTS
        await _wait_until(lambda: len(transport.sent) >= 3 * _PLACEHOLDER_PKTS)
        proto.close()
        sent = len(transport.sent)
        await asyncio.sleep(0.01)
    assert len(transport.sent) == sent  # close() stops the placeholder

    assert {addr for _, addr in transport.sent} == {("127.0.0.1", 12345)}
    pkts = [_parse_rtp(data) for data, _ in transport.sent]
    frames = [
        pkts[i : i + _PLACEHOLDER_PKTS]
        for i in range(0, len(pkts), _PLACEHOLDER_PKTS)
    ]
    assert all(p[0] == 96 for p in pkts)  # VP8
    for frame in frames:
        assert [p[5] for p in frame] == _VP8_PLACEHOLDER_PACKETS
        assert [p[1] for p in frame] == [0] * (_PLACEHOLDER_PKTS - 1) + [1]
        assert len({p[3] for p in frame}) == 1  # one timestamp per frame
    assert len({p[4] for p in pkts}) == 1  # one SSRC
    seqs = [p[2] for p in pkts]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    frame_tss = [frame[0][3] for frame in frames]
    assert all(b > a for a, b in zip(frame_tss, frame_tss[1:]))
    assert proto.placeholder_frames_sent == len(frames)
    assert proto.rtp_packets_forwarded == 0  # real-video counter untouched


async def test_video_switches_from_placeholder_to_real_vp8_on_keyframe():
    """The first real keyframe takes over from the placeholder. ffmpeg must
    see ONE continuous stream: same SSRC, consecutive seq, timestamps moving
    forward, with the gateway's own frame spacing preserved."""
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    transport = _FakeTransport()
    proto.connection_made(transport)
    interval, resume = _placeholder_patches()
    with interval, resume:
        proto.start_placeholder()
        await _wait_until(lambda: len(transport.sent) >= 2 * _PLACEHOLDER_PKTS)

        proto.datagram_received(_vp8_pkt(6, 1000, _REAL_INTERFRAME), ("x", 1))
        placeholders = len(transport.sent)
        proto.datagram_received(
            _vp8_pkt(7, 5000, _REAL_KEYFRAME, marker=False), ("x", 1)
        )
        proto.datagram_received(_vp8_pkt(8, 5000, _REAL_CONTINUATION), ("x", 1))
        proto.datagram_received(_vp8_pkt(9, 14000, _REAL_INTERFRAME), ("x", 1))
        # While the real stream is live, no more placeholder frames.
        await asyncio.sleep(0.02)
        proto.close()

    pkts = [_parse_rtp(data) for data, _ in transport.sent]
    assert len(pkts) == placeholders + 3  # pre-keyframe P-frame was dropped
    assert proto.placeholder_frames_sent * _PLACEHOLDER_PKTS == placeholders
    assert proto.rtp_packets_forwarded == 3
    assert len({p[4] for p in pkts}) == 1
    seqs = [p[2] for p in pkts]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    last_placeholder, key, cont, inter = pkts[placeholders - 1 :]
    assert key[3] > last_placeholder[3]
    assert cont[3] == key[3]  # same frame, same timestamp
    assert inter[3] - key[3] == 9000  # gateway spacing preserved
    assert [key[1], cont[1], inter[1]] == [0, 1, 1]  # marker bits kept
    assert [key[5], cont[5], inter[5]] == [
        _REAL_KEYFRAME,
        _REAL_CONTINUATION,
        _REAL_INTERFRAME,
    ]


async def test_video_placeholder_resumes_when_real_video_stalls():
    """If the gateway stops sending VP8 mid-call, the placeholder takes over
    again (otherwise ffmpeg's RTP input times out and the whole stream, audio
    included, dies). Real video then only resumes on a fresh keyframe — the
    decoder's reference frame is now the placeholder."""
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    transport = _FakeTransport()
    proto.connection_made(transport)
    interval, resume = _placeholder_patches(interval=0.001, resume=0.01)
    with interval, resume:
        proto.start_placeholder()
        proto.datagram_received(_vp8_pkt(1, 5000, _REAL_KEYFRAME), ("x", 1))
        before_stall = proto.placeholder_frames_sent
        await _wait_until(lambda: proto.placeholder_frames_sent > before_stall)

        proto.datagram_received(_vp8_pkt(2, 14000, _REAL_INTERFRAME), ("x", 1))
        assert proto.rtp_packets_forwarded == 1  # P-frame after stall dropped
        proto.datagram_received(_vp8_pkt(3, 23000, _REAL_KEYFRAME), ("x", 1))
        assert proto.rtp_packets_forwarded == 2
        proto.close()

    pkts = [_parse_rtp(data) for data, _ in transport.sent]
    seqs = [p[2] for p in pkts]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    tss = [p[3] for p in pkts]
    assert tss == sorted(tss)  # never backwards (a frame's packets share one)


async def test_video_ffmpeg_letterboxes_into_a_fixed_canvas():
    """The real camera resolution is unknown and differs from the
    placeholder's: ffmpeg must fit every input into the same 640x480 canvas
    (aspect ratio kept, black bars) so the H.264 output never changes size
    mid-stream. The lavfi placeholder variant has no VP8 input to fit."""
    with _patch_spawn([_make_fake_process(), _make_fake_process()]) as spawn:
        bridge = AudioBridge()
        await bridge._spawn_ffmpeg(video_port=55555)
        video_args = list(spawn.await_args.args[1:])
        await bridge._spawn_ffmpeg(video_port=None)
        lavfi_args = list(spawn.await_args.args[1:])

    vf = video_args[video_args.index("-filter:v") + 1]
    assert "scale=640:480:force_original_aspect_ratio=decrease" in vf
    assert "pad=640:480" in vf
    assert video_args.index("-filter:v") > video_args.index("1:v")
    assert "-filter:v" not in lavfi_args


async def test_video_start_feeds_placeholder_right_away(fake_datagram_endpoint):
    """start() with video must feed ffmpeg the placeholder immediately — no
    waiting on the gateway's first keyframe before the push can happen — and
    stop() must end it."""
    with _patch_spawn([_make_fake_process()]), _patch_video_port(55555):
        bridge = AudioBridge()
        await bridge.start(
            rtp_socket=_fake_rtp_socket(16384),
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
            video_socket=_fake_rtp_socket(16386),
            remote_video_rtp_ip="178.32.84.135",
            remote_video_rtp_port=52982,
            video_rtcp_socket=_fake_rtp_socket(16387),
        )

        def to_ffmpeg():
            return [
                d for d, addr in fake_datagram_endpoint.sent
                if addr == ("127.0.0.1", 55555)
            ]

        assert to_ffmpeg()
        assert _parse_rtp(to_ffmpeg()[0])[5] == _VP8_PLACEHOLDER_PACKETS[0]
        await bridge.stop()
        sent = len(to_ffmpeg())
        await asyncio.sleep(0.6)  # > one placeholder interval
        assert len(to_ffmpeg()) == sent


# --- AudioBridge._pli_loop -------------------------------------------------


def _bridge_with_video(on_video_failure=None):
    """AudioBridge with real video RTP/RTCP protocols on fake transports —
    just enough wiring for `_pli_loop` to run without sockets or ffmpeg."""
    bridge = AudioBridge()
    bridge._video_rtp = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    bridge._video_rtp.connection_made(_FakeTransport())
    bridge._video_rtcp = _VideoRtcpProtocol(remote_addr=("178.32.84.135", 52983))
    rtcp_transport = _FakeTransport()
    bridge._video_rtcp.connection_made(rtcp_transport)
    bridge._on_video_failure = on_video_failure
    return bridge, rtcp_transport


async def _wait_until(cond, timeout_s: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


async def test_pli_loop_sends_first_pli_immediately_on_first_rtp():
    """The first PLI (keyframe request) must go out as soon as the first RTP
    packet reveals the media SSRC — event-driven, no polling delay. A handful
    of bare event-loop ticks (no wall-clock sleep) must be enough."""
    bridge, rtcp_transport = _bridge_with_video()
    task = asyncio.create_task(bridge._pli_loop())
    for _ in range(5):
        await asyncio.sleep(0)
    assert rtcp_transport.sent == []  # no RTP yet → no PLI

    bridge._video_rtp.datagram_received(_VP8_INTERFRAME_PKT, ("x", 1))
    for _ in range(10):
        await asyncio.sleep(0)
        if rtcp_transport.sent:
            break
    assert len(rtcp_transport.sent) == 1
    task.cancel()


async def test_pli_loop_waits_for_late_video_rtp():
    """Video that only starts flowing late in the call (the placeholder
    covers the gap) must still get its keyframe request: no PLI and no
    video-failure callback while nothing arrives, then the PLI goes out as
    soon as the first RTP does."""
    failure = MagicMock()
    bridge, rtcp_transport = _bridge_with_video(on_video_failure=failure)
    task = asyncio.create_task(bridge._pli_loop())
    await asyncio.sleep(0.05)
    assert not task.done()
    assert rtcp_transport.sent == []

    bridge._video_rtp.datagram_received(_VP8_INTERFRAME_PKT, ("x", 1))
    await _wait_until(lambda: rtcp_transport.sent)
    task.cancel()
    failure.assert_not_called()


async def test_pli_loop_exhausts_burst_then_fires_video_failure():
    """RTP flows but no keyframe ever arrives → the loop sends the full PLI
    burst then asks CallManager for the audio-only re-INVITE."""
    failure = MagicMock()
    bridge, rtcp_transport = _bridge_with_video(on_video_failure=failure)
    # First RTP already seen (interframe) so the SSRC is known.
    bridge._video_rtp.datagram_received(_VP8_INTERFRAME_PKT, ("x", 1))
    with patch("custom_components.intratone.audio_bridge._PLI_INTERVAL_S", 0.001):
        await asyncio.wait_for(bridge._pli_loop(), timeout=2)
    assert bridge._video_rtcp.pli_sent == 10  # _PLI_MAX_SENDS
    # Every PLI targeted the gateway's RTCP address.
    assert all(addr == ("178.32.84.135", 52983) for _, addr in rtcp_transport.sent)
    failure.assert_called_once()


async def test_pli_loop_stops_burst_when_keyframe_arrives():
    """A keyframe mid-burst stops the PLI spam and the failure callback must
    never fire; the loop then parks in the periodic phase."""
    failure = MagicMock()
    bridge, _ = _bridge_with_video(on_video_failure=failure)
    bridge._video_rtp.datagram_received(_VP8_INTERFRAME_PKT, ("x", 1))
    with patch("custom_components.intratone.audio_bridge._PLI_INTERVAL_S", 0.01):
        task = asyncio.create_task(bridge._pli_loop())
        await _wait_until(lambda: bridge._video_rtcp.pli_sent >= 1)
        bridge._video_rtp.datagram_received(_VP8_KEYFRAME_PKT, ("x", 1))
        # The burst must settle (no more PLIs) instead of running to 10.
        await asyncio.sleep(0.05)
        settled = bridge._video_rtcp.pli_sent
        await asyncio.sleep(0.05)
        assert bridge._video_rtcp.pli_sent == settled < 10
        assert not task.done()  # periodic phase keeps running
        task.cancel()
    failure.assert_not_called()


async def test_pli_loop_periodic_resets_gate_and_reopens_on_keyframe():
    """Periodic PLI closes the forwarding gate; the fresh keyframe reopens it
    and interframes flow to ffmpeg again."""
    bridge, _ = _bridge_with_video()
    video = bridge._video_rtp
    video.datagram_received(_VP8_INTERFRAME_PKT, ("x", 1))
    with (
        patch("custom_components.intratone.audio_bridge._PLI_INTERVAL_S", 0.001),
        patch(
            "custom_components.intratone.audio_bridge._PLI_PERIODIC_INTERVAL_S",
            0.01,
        ),
    ):
        task = asyncio.create_task(bridge._pli_loop())
        await _wait_until(lambda: bridge._video_rtcp.pli_sent >= 1)
        video.datagram_received(_VP8_KEYFRAME_PKT, ("x", 1))
        burst_plis = bridge._video_rtcp.pli_sent
        # Wait for the periodic PLI — it must reset the keyframe gate.
        await _wait_until(lambda: bridge._video_rtcp.pli_sent > burst_plis)
        await _wait_until(lambda: video.keyframe_received is False)
        forwarded_before = video.rtp_packets_forwarded
        video.datagram_received(_VP8_INTERFRAME_PKT, ("x", 1))
        assert video.rtp_packets_forwarded == forwarded_before  # gate closed
        # Fresh keyframe reopens the gate.
        video.datagram_received(_VP8_KEYFRAME_PKT, ("x", 1))
        assert video.keyframe_received is True
        video.datagram_received(_VP8_INTERFRAME_PKT, ("x", 1))
        assert video.rtp_packets_forwarded == forwarded_before + 2
        task.cancel()


# --- diagnostics log levels -------------------------------------------------


async def test_audio_rx_summary_is_debug_only(caplog):
    """Per-call telemetry stays out of the default log: the aggregate
    `AUDIO_RX_SUMMARY` line and its per-source / per-PT / per-SSRC detail are
    all DEBUG (README: enable debug logs to troubleshoot)."""
    import logging

    proto = _RtpProtocol(
        remote_addr=("178.32.84.135", 12345),
        on_ulaw=lambda _: None,
        send_keepalives=False,
    )
    proto.connection_made(_FakeTransport())
    payload = _ULAW_SILENCE_BYTE * _SAMPLES_PER_PACKET
    pkt = _build_rtp_packet(seq=1, timestamp=160, ssrc=42, payload=payload)
    proto.datagram_received(pkt, ("178.32.84.135", 12345))

    with caplog.at_level(
        logging.DEBUG, logger="custom_components.intratone.audio_bridge"
    ):
        proto.dump_summary()
    proto.close()

    summary = [r for r in caplog.records if "AUDIO_RX_SUMMARY" in r.message]
    assert len(summary) > 1  # aggregate line + detail
    assert all(r.levelno == logging.DEBUG for r in summary)


async def test_video_rx_summary_is_debug_only(caplog):
    """Same contract for the video side: every `VIDEO_RX_SUMMARY` line is
    DEBUG."""
    import logging

    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    proto.connection_made(_FakeTransport())
    proto.datagram_received(_VP8_KEYFRAME_PKT, ("178.32.84.135", 52982))

    with caplog.at_level(
        logging.DEBUG, logger="custom_components.intratone.audio_bridge"
    ):
        proto.close()

    summary = [r for r in caplog.records if "VIDEO_RX_SUMMARY" in r.message]
    assert len(summary) > 1  # aggregate line + detail
    assert all(r.levelno == logging.DEBUG for r in summary)


# --- helpers for the edge-case tests below ----------------------------------

_LOG = "custom_components.intratone.audio_bridge"
_GATEWAY = ("178.32.84.135", 52982)
_STUN_TXID = bytes(range(12))
_STUN_BINDING_REQUEST = struct.pack(">HHI12s", 0x0001, 0, 0x2112A442, _STUN_TXID)
_SILENCE = _ULAW_SILENCE_BYTE * _SAMPLES_PER_PACKET


def _audio_rtp(
    seq: int = 1,
    ssrc: int = 42,
    pt: int = _PCMU_PAYLOAD_TYPE,
    payload: bytes = _SILENCE,
    *,
    version: int = 2,
    csrcs: tuple[int, ...] = (),
    ext_words: tuple[int, ...] | None = None,
) -> bytes:
    """RTP packet with an optional CSRC list / header extension (RFC 3550)."""
    b0 = (version << 6) | (0x10 if ext_words is not None else 0) | len(csrcs)
    header = struct.pack(">BBHII", b0, pt, seq, seq * _SAMPLES_PER_PACKET, ssrc)
    header += b"".join(struct.pack(">I", c) for c in csrcs)
    if ext_words is not None:
        header += struct.pack(">HH", 0xBEDE, len(ext_words))
        header += b"".join(struct.pack(">I", w) for w in ext_words)
    return header + payload


async def _yield_until(cond, max_ticks: int = 200) -> None:
    """Spin bare event-loop ticks (no wall-clock wait) until `cond()` holds."""
    for _ in range(max_ticks):
        if cond():
            return
        await asyncio.sleep(0)
    assert cond(), "condition not met within the tick budget"


def _messages(caplog, marker: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if marker in r.getMessage()]


def _live_tasks(coro_name: str) -> list[asyncio.Task]:
    return [
        t
        for t in asyncio.all_tasks()
        if not t.done() and t.get_coro().__name__ == coro_name
    ]


class _RaisingSendTransport(_FakeTransport):
    """sendto() always fails — unreachable gateway, dead ffmpeg port."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc
        self.attempts = 0

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.attempts += 1
        raise self._exc


class _LoopTransport(_FakeTransport):
    """`_FakeTransport` plus asyncio's close() contract: the protocol's
    connection_lost() runs on the next loop iteration."""

    def __init__(self, protocol) -> None:
        super().__init__()
        self.protocol = protocol

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            asyncio.get_running_loop().call_soon(
                self.protocol.connection_lost, None
            )


class _Endpoints:
    """Fake `loop.create_datagram_endpoint`. Wraps are numbered in start()
    order — 1 audio RTP, 2 video RTP, 3 video RTCP: wrap `park_on` waits for
    `release`, wrap `fail_on` raises EADDRINUSE."""

    def __init__(self, park_on: int | None = None, fail_on: int | None = None):
        self.park_on = park_on
        self.fail_on = fail_on
        self.parked = asyncio.Event()
        self.release = asyncio.Event()
        self.transports: list[_LoopTransport] = []
        self.calls = 0

    async def _create(self, protocol_factory, **_kwargs):
        self.calls += 1
        if self.calls == self.fail_on:
            raise OSError(errno.EADDRINUSE, "Address already in use")
        if self.calls == self.park_on:
            self.parked.set()
            await self.release.wait()
        proto = protocol_factory()
        transport = _LoopTransport(proto)
        self.transports.append(transport)
        proto.connection_made(transport)
        return transport, proto

    def patch(self):
        return patch.object(
            asyncio.get_running_loop(),
            "create_datagram_endpoint",
            side_effect=self._create,
        )


def _video_start_kwargs(
    rtp_socket=None, video_socket=None, video_rtcp_socket=None
) -> dict:
    return {
        "rtp_socket": rtp_socket if rtp_socket is not None else _fake_rtp_socket(16384),
        "remote_rtp_ip": "178.32.84.135",
        "remote_rtp_port": 20000,
        "video_socket": (
            video_socket if video_socket is not None else _fake_rtp_socket(16386)
        ),
        "remote_video_rtp_ip": "178.32.84.135",
        "remote_video_rtp_port": 52982,
        "video_rtcp_socket": (
            video_rtcp_socket
            if video_rtcp_socket is not None
            else _fake_rtp_socket(16387)
        ),
    }


# --- _RtpProtocol: STUN, non-RTP, header parsing, stats ---------------------


async def test_audio_stun_binding_request_is_answered_not_forwarded(
    rtp_setup, caplog
):
    """Asterisk withholds the real downstream audio until our audio port
    answers its STUN Binding Requests: each gets a Binding Response back to
    the sender and none reaches ffmpeg as audio."""
    proto, transport, payloads = rtp_setup
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        for _ in range(4):
            proto.datagram_received(_STUN_BINDING_REQUEST, _GATEWAY)

    assert payloads == []
    assert proto.packets_received == 0
    assert proto.stun_count == 4
    assert len(transport.sent) == 4
    for data, addr in transport.sent:
        msg_type, _, magic, txid = struct.unpack(">HHI12s", data[:20])
        assert (msg_type, magic, txid) == (0x0101, 0x2112A442, _STUN_TXID)
        assert addr == _GATEWAY
    assert len(_messages(caplog, "AUDIO_RX_STUN")) == 3  # log capped


async def test_audio_stun_response_failure_is_contained(caplog):
    payloads: list[bytes] = []
    proto = _RtpProtocol(
        remote_addr=_GATEWAY, on_ulaw=payloads.append, send_keepalives=False
    )
    proto.connection_made(
        _RaisingSendTransport(OSError(errno.ENETUNREACH, "Network is unreachable"))
    )
    proto.datagram_received(_STUN_BINDING_REQUEST, _GATEWAY)
    proto.datagram_received(_audio_rtp(), _GATEWAY)

    assert proto.stun_count == 1
    assert payloads == [_SILENCE]  # audio keeps flowing
    assert "AUDIO_STUN response build/send failed" in caplog.text
    proto.close()


async def test_audio_non_rtp_datagrams_are_dropped_and_counted(rtp_setup, caplog):
    proto, _, payloads = rtp_setup
    not_v2 = _audio_rtp(version=1)
    runt = b"\x80\x00\x00"
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        for pkt in [not_v2] * 3 + [runt] * 6 + [not_v2]:
            proto.datagram_received(pkt, _GATEWAY)

    assert proto.non_rtp_count == 10
    assert proto.packets_received == 0
    assert payloads == []
    assert len(_messages(caplog, "AUDIO_RX_NONRTP")) == 5  # log capped


async def test_audio_rtp_csrc_list_and_extension_are_skipped(rtp_setup):
    """RFC 3550 §5.1/§5.3.1: the payload starts after the CSRC list and the
    header extension — forwarding those bytes as µ-law would be noise."""
    proto, _, payloads = rtp_setup
    payload = bytes(range(160))
    proto.datagram_received(
        _audio_rtp(payload=payload, csrcs=(0x1111, 0x2222), ext_words=(0xAAAAAAAA,)),
        _GATEWAY,
    )
    assert payloads == [payload]


async def test_audio_rtp_truncated_headers_are_dropped(rtp_setup):
    proto, _, payloads = rtp_setup
    # X=1, but the packet ends inside the extension header.
    truncated_ext = _audio_rtp(payload=b"", ext_words=())[:14]
    # CC=15 announces 60 bytes of CSRCs that aren't there.
    missing_csrcs = struct.pack(">BBHII", (2 << 6) | 15, 0, 1, 160, 42) + bytes(20)
    for pkt in (truncated_ext, missing_csrcs):
        proto.datagram_received(pkt, _GATEWAY)
    assert payloads == []
    assert proto.packets_received == 0


async def test_audio_rx_stats_track_payload_types_ssrcs_and_seq_gaps(rtp_setup):
    """The troubleshooting stats (README): PT histogram (PCMU vs comfort
    noise), SSRCs and sequence gaps — a real gap counts once; wraparound,
    duplicates and reset-sized jumps don't."""
    proto, _, payloads = rtp_setup
    for seq in (65534, 65535, 0, 3, 3, 2003):
        proto.datagram_received(_audio_rtp(seq=seq), _GATEWAY)
    proto.datagram_received(_audio_rtp(seq=500, ssrc=7, pt=13, payload=b"\x40"), _GATEWAY)

    assert proto.packets_received == 7
    assert len(payloads) == 7
    assert proto.seq_gaps == 1
    assert {pt: st["count"] for pt, st in proto.pt_stats.items()} == {0: 6, 13: 1}
    assert {s: st["count"] for s, st in proto.ssrc_stats.items()} == {42: 6, 7: 1}
    assert proto.unique_sources == {_GATEWAY}


async def test_audio_rx_snapshot_logged_every_50_packets_at_debug(rtp_setup, caplog):
    proto, _, _ = rtp_setup
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        for seq in range(1, 51):
            proto.datagram_received(_audio_rtp(seq=seq), _GATEWAY)

    snapshots = [r for r in caplog.records if r.getMessage().startswith("AUDIO_RX[#")]
    assert [r.getMessage().split(":")[0] for r in snapshots] == ["AUDIO_RX[#50]"]
    assert all(r.levelno == logging.DEBUG for r in snapshots)


async def test_audio_consumer_error_is_contained(caplog):
    on_ulaw = MagicMock(side_effect=RuntimeError("consumer gone"))
    proto = _RtpProtocol(remote_addr=_GATEWAY, on_ulaw=on_ulaw, send_keepalives=False)
    proto.connection_made(_FakeTransport())
    proto.datagram_received(_audio_rtp(seq=1), _GATEWAY)
    proto.datagram_received(_audio_rtp(seq=2), _GATEWAY)

    assert on_ulaw.call_count == 2
    assert proto.packets_received == 2
    assert "on_ulaw callback raised" in caplog.text
    proto.close()


async def test_connection_lost_stops_keepalive():
    proto = _RtpProtocol(
        remote_addr=_GATEWAY, on_ulaw=lambda _: None, send_keepalives=True
    )
    transport = _FakeTransport()
    proto.connection_made(transport)
    task = proto._keepalive_task
    await asyncio.sleep(0)  # the first silence packet goes out right away
    assert len(transport.sent) == 1

    proto.connection_lost(None)
    await _yield_until(task.done)

    assert task.cancelled()
    assert len(transport.sent) == 1


async def test_keepalive_survives_transient_send_errors():
    """A failed keepalive send (ENOBUFS) must not end the NAT punching: the
    loop moves on to the next packet."""
    proto = _RtpProtocol(
        remote_addr=_GATEWAY, on_ulaw=lambda _: None, send_keepalives=True
    )
    transport = _FakeTransport()
    record = transport.sendto
    failures = [OSError(errno.ENOBUFS, "No buffer space available")]

    def flaky_sendto(data: bytes, addr: tuple[str, int]) -> None:
        if failures:
            raise failures.pop()
        record(data, addr)

    transport.sendto = flaky_sendto
    with patch.object(bridge_mod, "_PACKET_INTERVAL_S", 0):
        proto.connection_made(transport)
        task = proto._keepalive_task
        await _yield_until(lambda: len(transport.sent) >= 3)
        proto.close()
        await _yield_until(task.done)

    seqs = [
        struct.unpack(_RTP_HEADER_FMT, p[:_RTP_HEADER_SIZE])[2]
        for p, _ in transport.sent
    ]
    assert seqs[:3] == [1, 2, 3]  # packet 0 was lost; the loop carried on
    assert proto.packets_sent == len(transport.sent)


# --- _VideoRtpProtocol / _VideoRtcpProtocol edge cases ----------------------


async def test_video_stun_binding_request_is_answered_not_forwarded(caplog):
    """The gateway withholds VP8 until our video port answers its STUN
    probes. Answers go back to the gateway, nothing reaches ffmpeg, and STUN
    doesn't count as the first RTP packet the PLI loop waits for."""
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    transport = _FakeTransport()
    proto.connection_made(transport)
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        for _ in range(4):
            proto.datagram_received(_STUN_BINDING_REQUEST, _GATEWAY)

    assert proto.stun_requests == 4
    assert [addr for _, addr in transport.sent] == [_GATEWAY] * 4
    assert all(data[:2] == b"\x01\x01" for data, _ in transport.sent)
    assert proto.rtp_packets_forwarded == 0
    assert not proto.first_rtp_event.is_set()
    assert len(_messages(caplog, "VIDEO_RX_STUN")) == 3  # log capped
    proto.close()


async def test_video_stun_response_failure_is_contained(caplog):
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    proto.connection_made(
        _RaisingSendTransport(OSError(errno.ENETUNREACH, "Network is unreachable"))
    )
    proto.datagram_received(_STUN_BINDING_REQUEST, _GATEWAY)

    assert proto.stun_requests == 1
    assert "VIDEO_STUN: response build/send failed" in caplog.text
    proto.close()


async def test_video_non_rtp_datagrams_are_dropped(caplog):
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    transport = _FakeTransport()
    proto.connection_made(transport)
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        for _ in range(3):
            proto.datagram_received(b"\x80\x60", _GATEWAY)  # runt
        for _ in range(3):
            proto.datagram_received(bytes(20), _GATEWAY)  # V=0, not STUN

    assert proto.non_rtp_count == 6
    assert transport.sent == []
    assert not proto.first_rtp_event.is_set()
    assert len(_messages(caplog, "VIDEO_RX_NONRTP")) == 5  # log capped
    proto.close()


async def test_video_protocol_is_inert_after_connection_lost():
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    transport = _FakeTransport()
    proto.connection_made(transport)
    proto.connection_lost(None)

    proto.datagram_received(_VP8_KEYFRAME_PKT, _GATEWAY)
    proto.start_placeholder()

    assert transport.sent == []
    assert proto.placeholder_frames_sent == 0
    assert not proto.keyframe_received
    assert not proto.first_rtp_event.is_set()
    proto.close()  # nothing left to close
    assert not transport.closed


async def test_video_forwarding_survives_ffmpeg_send_errors():
    """ffmpeg died (its loopback port answers ECONNREFUSED): placeholder and
    real-VP8 forwarding carry on instead of raising into the event loop."""
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    transport = _RaisingSendTransport(
        OSError(errno.ECONNREFUSED, "Connection refused")
    )
    proto.connection_made(transport)
    proto.start_placeholder()
    proto.datagram_received(_vp8_pkt(1, 5000, _REAL_KEYFRAME), _GATEWAY)

    assert proto.placeholder_frames_sent == 1
    assert proto.rtp_packets_forwarded == 1
    assert transport.attempts == _PLACEHOLDER_PKTS + 1
    proto.close()


async def test_video_rx_snapshot_logged_every_50_forwarded_packets(caplog):
    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    proto.connection_made(_FakeTransport())
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        proto.datagram_received(_vp8_pkt(1, 0, _REAL_KEYFRAME), _GATEWAY)
        for seq in range(2, 51):
            proto.datagram_received(
                _vp8_pkt(seq, seq * 3000, _REAL_INTERFRAME), _GATEWAY
            )

    assert proto.rtp_packets_forwarded == 50
    snapshots = [
        m for m in (r.getMessage() for r in caplog.records)
        if m.startswith("VIDEO_RX[#")
    ]
    assert [m.split(":")[0] for m in snapshots] == ["VIDEO_RX[#50]"]
    proto.close()


async def test_video_rtcp_incoming_packets_are_counted(caplog):
    proto = _VideoRtcpProtocol(remote_addr=("178.32.84.135", 52983))
    proto.connection_made(_FakeTransport())
    sender_report = bytes([0x80, 200, 0x00, 0x06]) + bytes(24)
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        for _ in range(4):
            proto.datagram_received(sender_report, ("178.32.84.135", 52983))
        proto.datagram_received(b"\x80", ("178.32.84.135", 52983))  # runt

    assert proto.rtcp_received == 5
    assert _messages(caplog, "VIDEO_RTCP_RX") == [
        f"VIDEO_RTCP_RX[#{i}]: PT=200 len=28 from 178.32.84.135:52983"
        for i in (1, 2, 3)
    ]
    proto.close()


async def test_video_rtcp_pli_send_error_is_not_counted():
    proto = _VideoRtcpProtocol(remote_addr=("178.32.84.135", 52983))
    transport = _RaisingSendTransport(
        OSError(errno.ENETUNREACH, "Network is unreachable")
    )
    proto.connection_made(transport)

    proto.send_pli(media_ssrc=0xDEADBEEF)  # must not raise

    assert transport.attempts == 1
    assert proto.pli_sent == 0


async def test_video_rtcp_is_inert_after_connection_lost():
    proto = _VideoRtcpProtocol(remote_addr=("178.32.84.135", 52983))
    transport = _FakeTransport()
    proto.connection_made(transport)
    proto.connection_lost(None)

    proto.send_pli(media_ssrc=0xDEADBEEF)
    proto.close()

    assert transport.sent == []
    assert proto.pli_sent == 0
    assert not transport.closed


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # I=1, M=1: 15-bit PictureID (2 bytes) — a 7-bit read lands on 0x43.
        (bytes([0x90, 0x80, 0x80, 0x43, 0x00]), True),
        # L=1: a TL0PICIDX byte precedes the frame tag.
        (bytes([0x90, 0x40, 0x01, 0x00]), True),
        # T=1 / K=1: a TID|Y|KEYIDX byte precedes the frame tag.
        (bytes([0x90, 0x20, 0x01, 0x00]), True),
        (bytes([0x90, 0x10, 0x01, 0x00]), True),
        # Every optional field: 1 + 1 + 2 + 1 + 1 descriptor bytes.
        (bytes([0x90, 0xF0, 0x80, 0x01, 0x01, 0x01, 0x00]), True),
        (bytes([0x90, 0xF0, 0x80, 0x01, 0x01, 0x01, 0x01]), False),
        # The descriptor ends where the frame tag should start.
        (bytes([0x90, 0xF0, 0x80, 0x01, 0x01, 0x01]), False),
        # PID != 0: not the frame's first partition.
        (bytes([0x11, 0x00, 0x00, 0x00]), False),
    ],
    ids=[
        "16bit-picture-id",
        "tl0picidx",
        "tid",
        "keyidx",
        "all-fields-key",
        "all-fields-inter",
        "truncated-after-descriptor",
        "non-zero-partition",
    ],
)
def test_is_vp8_keyframe_skips_optional_descriptor_fields(payload, expected):
    """RFC 7741 §4.2: the frame tag sits after every optional descriptor
    field — misreading its offset opens the forwarding gate on a P-frame (or
    keeps it shut on a keyframe)."""
    assert _is_vp8_keyframe(payload) is expected


# --- AudioBridge: counters, ffmpeg stdin, stop() edge cases -----------------


async def test_packet_counters_follow_the_live_audio_endpoint(
    mock_subprocess, fake_datagram_endpoint
):
    bridge = AudioBridge()
    assert (bridge.packets_received, bridge.packets_sent) == (0, 0)
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    bridge._rtp.datagram_received(_audio_rtp(), ("178.32.84.135", 20000))
    await _yield_until(lambda: bridge.packets_sent >= 1)  # NAT keepalive

    assert bridge.packets_received == 1
    await bridge.stop()
    assert (bridge.packets_received, bridge.packets_sent) == (0, 0)


async def test_audio_is_dropped_once_ffmpeg_stdin_is_closing(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    """ffmpeg exiting closes its stdin pipe: late RTP is dropped instead of
    written into a dead pipe, and stop() doesn't re-close it (still
    SIGTERMs the process)."""
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    fake_process.stdin.is_closing.return_value = True
    bridge._rtp.datagram_received(_audio_rtp(), ("178.32.84.135", 20000))
    fake_process.stdin.write.assert_not_called()

    await bridge.stop()
    fake_process.stdin.close.assert_not_called()
    assert fake_process._last_signal == signal.SIGTERM


async def test_audio_forwarding_survives_ffmpeg_broken_pipe(
    mock_subprocess, fake_process, fake_datagram_endpoint, caplog
):
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    fake_process.stdin.write.side_effect = BrokenPipeError
    for seq in (1, 2):
        bridge._rtp.datagram_received(_audio_rtp(seq=seq), ("178.32.84.135", 20000))

    assert fake_process.stdin.write.call_count == 2
    assert bridge._bytes_written_to_ffmpeg == 0
    assert "on_ulaw callback raised" not in caplog.text
    await bridge.stop()


async def test_audio_bytes_written_to_ffmpeg_accumulate(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    for seq in (1, 2):
        bridge._rtp.datagram_received(_audio_rtp(seq=seq), ("178.32.84.135", 20000))

    written = [c.args[0] for c in fake_process.stdin.write.call_args_list]
    assert written == [_SILENCE, _SILENCE]
    assert bridge._bytes_written_to_ffmpeg == 2 * _SAMPLES_PER_PACKET
    await bridge.stop()


async def test_stop_tolerates_ffmpeg_stdin_close_error(
    mock_subprocess, fake_process, fake_datagram_endpoint
):
    bridge = AudioBridge()
    await bridge.start(
        rtp_socket=_fake_rtp_socket(16384),
        remote_rtp_ip="178.32.84.135",
        remote_rtp_port=20000,
    )
    fake_process.stdin.close.side_effect = BrokenPipeError

    await bridge.stop()  # must not raise

    assert fake_process._last_signal == signal.SIGTERM
    assert not bridge.is_running


# --- AudioBridge: prewarm edge cases -----------------------------------------


async def test_cancel_prewarm_without_prewarm_is_noop():
    bridge = AudioBridge()
    bridge.cancel_prewarm()
    assert bridge._prewarm_cleanup_task is None


async def test_cancel_prewarm_after_failed_spawn_is_quiet():
    """A prewarm whose spawn failed has no ffmpeg to reap: the cleanup
    finishes quietly and stop() stays safe."""
    with _patch_spawn([FileNotFoundError("ffmpeg")]), _patch_video_port():
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        bridge.cancel_prewarm()
        await asyncio.wait_for(bridge._prewarm_cleanup_task, timeout=1)
        await bridge.stop()
    assert not bridge.is_running


async def test_cancel_prewarm_tolerates_already_reaped_ffmpeg():
    proc = _make_fake_process()
    proc.kill.side_effect = ProcessLookupError
    with _patch_spawn([proc]), _patch_video_port():
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await _await_prewarm(bridge)
        bridge.cancel_prewarm()
        await asyncio.wait_for(bridge._prewarm_cleanup_task, timeout=1)
        await bridge.stop()
    proc.kill.assert_called_once()


async def test_stop_waits_for_cancelled_prewarm_to_be_reaped():
    """Call died before the 200 OK while the prewarm spawn was still in
    flight: stop() must not return until that ffmpeg is reaped, or it would
    outlive the entry unload."""
    proc = _make_fake_process()
    spawn_gate = asyncio.Event()

    async def slow_spawn(*_args, **_kwargs):
        await spawn_gate.wait()
        return proc

    with (
        patch(
            "custom_components.intratone.audio_bridge.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=slow_spawn),
        ),
        _patch_video_port(),
    ):
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await asyncio.sleep(0)  # prewarm parked inside the spawn
        bridge.cancel_prewarm()
        stop_task = asyncio.create_task(bridge.stop())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not stop_task.done()

        spawn_gate.set()
        await asyncio.wait_for(stop_task, timeout=1)
    proc.kill.assert_called_once()


async def test_dead_prewarmed_ffmpeg_is_replaced_not_adopted(fake_datagram_endpoint):
    """The prewarmed ffmpeg crashed while the INVITE was in flight: start()
    must spawn a fresh one instead of adopting a corpse (a bridge that looks
    started but never pushes)."""
    prewarm_proc = _make_fake_process()
    call_proc = _make_fake_process()
    with _patch_spawn([prewarm_proc, call_proc]) as spawn, _patch_video_port():
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await _await_prewarm(bridge)
        prewarm_proc.returncode = 1

        await bridge.start(**_video_start_kwargs())

        assert spawn.await_count == 2
        assert bridge.is_running
        prewarm_proc.kill.assert_not_called()  # already dead
        await bridge.stop()


# --- AudioBridge.start(): stop() races and wrap failures --------------------


async def test_stop_during_ffmpeg_spawn_aborts_start_and_reaps_late_ffmpeg():
    """stop() while ffmpeg is still spawning: the process that shows up
    afterwards must be killed and the RTP socket closed — nothing wrapped,
    nothing published."""
    proc = _make_fake_process()
    spawning = asyncio.Event()
    spawn_gate = asyncio.Event()

    async def slow_spawn(*_args, **_kwargs):
        spawning.set()
        await spawn_gate.wait()
        return proc

    rtp_sock = _fake_rtp_socket(16384)
    endpoints = _Endpoints()
    with (
        patch(
            "custom_components.intratone.audio_bridge.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=slow_spawn),
        ),
        endpoints.patch(),
    ):
        bridge = AudioBridge()
        start_task = asyncio.create_task(
            bridge.start(
                rtp_socket=rtp_sock,
                remote_rtp_ip="178.32.84.135",
                remote_rtp_port=20000,
            )
        )
        await asyncio.wait_for(spawning.wait(), timeout=1)
        await bridge.stop()
        spawn_gate.set()
        with pytest.raises(BridgeStoppedError):
            await asyncio.wait_for(start_task, timeout=1)

    assert endpoints.calls == 0
    rtp_sock.close.assert_called_once()
    proc.kill.assert_called_once()
    assert not bridge.is_running


async def test_stop_during_audio_rtp_wrap_aborts_start_without_orphans(
    mock_subprocess,
):
    """stop() while the audio socket is being wrapped: the transport that
    wrap returns afterwards is closed (its keepalive with it), the video
    sockets are released unwrapped, and start() aborts."""
    endpoints = _Endpoints(park_on=1)
    video_sock = _fake_rtp_socket(16386)
    rtcp_sock = _fake_rtp_socket(16387)
    bridge = AudioBridge()
    with endpoints.patch(), _patch_video_port():
        start_task = asyncio.create_task(
            bridge.start(
                **_video_start_kwargs(
                    video_socket=video_sock, video_rtcp_socket=rtcp_sock
                )
            )
        )
        await asyncio.wait_for(endpoints.parked.wait(), timeout=1)
        await bridge.stop()
        endpoints.release.set()
        with pytest.raises(BridgeStoppedError):
            await asyncio.wait_for(start_task, timeout=1)
    await _yield_until(lambda: not _live_tasks("_silence_loop"))

    [audio_transport] = endpoints.transports  # no video wrap after the stop
    assert audio_transport.closed
    video_sock.close.assert_called_once()
    rtcp_sock.close.assert_called_once()
    assert bridge._rtp_transport is None
    assert not bridge.is_running


async def test_stop_during_video_rtcp_wrap_cancels_the_fresh_pli_loop(
    mock_subprocess,
):
    """stop() during the last wrap (video RTCP): start() creates the PLI
    task right after that wrap — it must be cancelled, not left pinging the
    old gateway every 30 s for the rest of HA's uptime."""
    endpoints = _Endpoints(park_on=3)
    bridge = AudioBridge()
    with endpoints.patch(), _patch_video_port():
        start_task = asyncio.create_task(bridge.start(**_video_start_kwargs()))
        await asyncio.wait_for(endpoints.parked.wait(), timeout=1)
        await bridge.stop()
        endpoints.release.set()
        with pytest.raises(BridgeStoppedError):
            await asyncio.wait_for(start_task, timeout=1)
    await _yield_until(lambda: not _live_tasks("_pli_loop"))

    assert len(endpoints.transports) == 3
    assert all(t.closed for t in endpoints.transports)
    assert bridge._pli_task is None
    assert not bridge.is_running


async def test_video_rtcp_wrap_failure_continues_without_pli(mock_subprocess):
    """RTCP only carries PLI keyframe requests: losing it costs a slower
    first frame, not the call — start() carries on with audio + video."""
    endpoints = _Endpoints(fail_on=3)
    rtcp_sock = _fake_rtp_socket(16387)
    rtcp_sock.close.side_effect = OSError(errno.EBADF, "Bad file descriptor")
    bridge = AudioBridge()
    with endpoints.patch(), _patch_video_port(55555):
        url = await bridge.start(**_video_start_kwargs(video_rtcp_socket=rtcp_sock))

        assert url == bridge.rtsp_url
        assert bridge.is_running
        assert bridge._pli_task is None
        rtcp_sock.close.assert_called_once()
        # The video path is live: the placeholder already reached ffmpeg.
        video_transport = endpoints.transports[1]
        assert ("127.0.0.1", 55555) in {addr for _, addr in video_transport.sent}
        await bridge.stop()
    await asyncio.sleep(0)


async def test_audio_wrap_failure_reports_wrap_error_despite_cleanup_errors(
    mock_subprocess, fake_process
):
    """Unwinding a failed start() is best effort (socket already closed,
    ffmpeg already reaped): the caller must still get the original wrap
    error, with nothing left running."""
    endpoints = _Endpoints(fail_on=1)
    rtp_sock = _fake_rtp_socket(16384)
    rtp_sock.close.side_effect = OSError(errno.EBADF, "Bad file descriptor")
    fake_process.kill.side_effect = ProcessLookupError
    bridge = AudioBridge()
    with (
        endpoints.patch(),
        pytest.raises(OSError, match="Address already in use") as excinfo,
    ):
        await bridge.start(
            rtp_socket=rtp_sock,
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
        )

    assert excinfo.type is OSError
    rtp_sock.close.assert_called_once()
    fake_process.kill.assert_called_once()
    assert bridge._process is None
    assert not bridge.is_running


# --- AudioBridge._log_stats_loop ---------------------------------------------


@pytest.mark.parametrize("video", [True, False], ids=["video", "audio-only"])
async def test_stats_loop_reports_pipeline_counters_at_debug(
    fake_datagram_endpoint, caplog, video
):
    start_kwargs = (
        _video_start_kwargs()
        if video
        else {
            "rtp_socket": _fake_rtp_socket(16384),
            "remote_rtp_ip": "178.32.84.135",
            "remote_rtp_port": 20000,
        }
    )
    with (
        _patch_spawn([_make_fake_process()]),
        _patch_video_port(),
        patch.object(bridge_mod, "_STATS_INTERVAL_S", 0),
        caplog.at_level(logging.DEBUG, logger=_LOG),
    ):
        bridge = AudioBridge()
        await bridge.start(**start_kwargs)
        bridge._rtp.datagram_received(_audio_rtp(), ("178.32.84.135", 20000))
        await _yield_until(lambda: _messages(caplog, "STATS: audio_rx=1 "))
        await bridge.stop()

    stats = [r for r in caplog.records if r.getMessage().startswith("STATS: ")]
    assert all(r.levelno == logging.DEBUG for r in stats)
    line = _messages(caplog, "STATS: audio_rx=1 ")[-1]
    assert "PT={0:1}" in line
    assert "ffmpeg_alive=True" in line
    assert ("video_stun=0 vp8=0 placeholder=" in line) is video


async def test_stats_loop_is_silent_below_debug(
    mock_subprocess, fake_datagram_endpoint, caplog
):
    with (
        patch.object(bridge_mod, "_STATS_INTERVAL_S", 0),
        caplog.at_level(logging.INFO, logger=_LOG),
    ):
        bridge = AudioBridge()
        await bridge.start(
            rtp_socket=_fake_rtp_socket(16384),
            remote_rtp_ip="178.32.84.135",
            remote_rtp_port=20000,
        )
        for _ in range(20):
            await asyncio.sleep(0)
        assert not bridge._stats_task.done()  # ticking, just not logging
        await bridge.stop()

    assert _messages(caplog, "STATS:") == []


# --- AudioBridge._pli_loop edge cases ----------------------------------------


class _KeyframeOnPliTransport(_FakeTransport):
    """RTCP transport of a gateway that honours PLI instantly: every PLI we
    send is answered by a VP8 keyframe on the video RTP port."""

    def __init__(self, video: _VideoRtpProtocol) -> None:
        super().__init__()
        self._video = video

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        super().sendto(data, addr)
        self._video.datagram_received(_VP8_KEYFRAME_PKT, _GATEWAY)


async def test_pli_loop_without_video_endpoint_returns_immediately():
    await asyncio.wait_for(AudioBridge()._pli_loop(), timeout=1)


async def test_pli_loop_without_rtcp_channel_sends_nothing():
    bridge, rtcp_transport = _bridge_with_video()
    bridge._video_rtcp = None
    bridge._video_rtp.datagram_received(_VP8_INTERFRAME_PKT, _GATEWAY)

    await asyncio.wait_for(bridge._pli_loop(), timeout=1)

    assert rtcp_transport.sent == []


async def test_pli_loop_keyframe_answering_the_last_pli_is_not_a_failure(caplog):
    """The keyframe answering the burst's final PLI lands during its wait:
    video works, so no audio-only downgrade — the loop moves on to the
    periodic phase."""
    failure = MagicMock()
    bridge, _ = _bridge_with_video(on_video_failure=failure)
    bridge._video_rtcp.connection_made(_KeyframeOnPliTransport(bridge._video_rtp))
    bridge._video_rtp.datagram_received(_VP8_INTERFRAME_PKT, _GATEWAY)
    with (
        patch.object(bridge_mod, "_PLI_MAX_SENDS", 1),
        caplog.at_level(logging.DEBUG, logger=_LOG),
    ):
        task = asyncio.create_task(bridge._pli_loop())
        await _yield_until(lambda: _messages(caplog, "keyframe arrived after 1 PLI"))
        assert not task.done()  # parked in the periodic phase
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    failure.assert_not_called()
    assert bridge._video_rtcp.pli_sent == 1


async def test_pli_loop_gives_up_quietly_without_failure_callback(caplog):
    bridge, _ = _bridge_with_video(on_video_failure=None)
    bridge._video_rtp.datagram_received(_VP8_INTERFRAME_PKT, _GATEWAY)
    with patch.object(bridge_mod, "_PLI_INTERVAL_S", 0):
        await asyncio.wait_for(bridge._pli_loop(), timeout=1)

    assert bridge._video_rtcp.pli_sent == 10  # _PLI_MAX_SENDS
    assert "gave up after 10 PLI(s)" in caplog.text


async def test_pli_loop_contains_video_failure_callback_errors(caplog):
    failure = MagicMock(side_effect=RuntimeError("SIP transport gone"))
    bridge, _ = _bridge_with_video(on_video_failure=failure)
    bridge._video_rtp.datagram_received(_VP8_INTERFRAME_PKT, _GATEWAY)
    with patch.object(bridge_mod, "_PLI_INTERVAL_S", 0):
        await asyncio.wait_for(bridge._pli_loop(), timeout=1)  # must not raise

    failure.assert_called_once()
    assert "on_video_failure callback raised" in caplog.text


async def test_pli_loop_periodic_keyframes_keep_the_gate_open(caplog):
    """Periodic phase against a gateway that honours PLI: every cycle closes
    the forwarding gate and the fresh keyframe reopens it — never the 1 s
    blackout fallback."""
    bridge, _ = _bridge_with_video()
    video = bridge._video_rtp
    rtcp_transport = _KeyframeOnPliTransport(video)
    bridge._video_rtcp.connection_made(rtcp_transport)
    video.datagram_received(_VP8_INTERFRAME_PKT, _GATEWAY)
    with (
        patch.object(bridge_mod, "_PLI_PERIODIC_INTERVAL_S", 0),
        caplog.at_level(logging.DEBUG, logger=_LOG),
    ):
        task = asyncio.create_task(bridge._pli_loop())
        await _yield_until(lambda: len(rtcp_transport.sent) >= 3)  # burst + 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert _messages(caplog, "periodic keyframe received")
    assert _messages(caplog, "re-opening gate") == []
    assert video.keyframe_received


async def test_pli_loop_crash_is_logged_not_raised(caplog):
    bridge, _ = _bridge_with_video()
    broken = _RaisingSendTransport(RuntimeError("transport in a bad state"))
    bridge._video_rtcp.connection_made(broken)
    bridge._video_rtp.datagram_received(_VP8_INTERFRAME_PKT, _GATEWAY)

    await asyncio.wait_for(bridge._pli_loop(), timeout=1)

    assert broken.attempts == 1
    assert "PLI_LOOP crashed" in caplog.text


# --- ffmpeg stderr drain -----------------------------------------------------


async def test_drain_stderr_without_pipe_returns_immediately():
    proc = _make_fake_process()
    proc.stderr = None
    ready = asyncio.Event()
    await asyncio.wait_for(AudioBridge()._drain_stderr(proc, ready), timeout=1)
    assert not ready.is_set()


async def test_drain_stderr_escalates_ffmpeg_errors_to_warning(caplog):
    proc = _make_fake_process()
    lines = iter(
        [
            b"Output #0, rtsp, to 'rtsp://127.0.0.1:8554/intratone':\n",
            b"[rtsp @ 0x7f] Error writing trailer: Broken pipe\n",
            b"frame=   25 fps=5.0 q=-1.0 size=N/A\n",
            b"",
        ]
    )
    proc.stderr.readline = AsyncMock(side_effect=lambda: next(lines))
    ready = asyncio.Event()
    with caplog.at_level(logging.DEBUG, logger=_LOG):
        await asyncio.wait_for(AudioBridge()._drain_stderr(proc, ready), timeout=1)

    assert ready.is_set()
    logged = {
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith("ffmpeg: ")
    }
    assert (
        logging.WARNING,
        "ffmpeg: [rtsp @ 0x7f] Error writing trailer: Broken pipe",
    ) in logged
    assert (logging.DEBUG, "ffmpeg: frame=   25 fps=5.0 q=-1.0 size=N/A") in logged


async def test_drain_stderr_stops_quietly_on_read_error():
    """StreamReader.readline raises ValueError on a line over its 64 KiB
    limit: the drainer ends quietly instead of crashing its task."""
    proc = _make_fake_process()
    proc.stderr.readline = AsyncMock(
        side_effect=ValueError("Separator is not found, and chunk exceed the limit")
    )
    ready = asyncio.Event()
    await asyncio.wait_for(AudioBridge()._drain_stderr(proc, ready), timeout=1)
    assert not ready.is_set()


# --- _pick_free_udp_port (no real sockets) -----------------------------------


def _fake_bridge_socket_module(sock) -> SimpleNamespace:
    return SimpleNamespace(
        AF_INET=socket.AF_INET,
        SOCK_DGRAM=socket.SOCK_DGRAM,
        socket=MagicMock(return_value=sock),
    )


def test_pick_free_udp_port_returns_kernel_port_and_releases_it():
    sock = MagicMock()
    sock.getsockname.return_value = ("127.0.0.1", 50123)
    with patch.object(bridge_mod, "socket", _fake_bridge_socket_module(sock)):
        assert _pick_free_udp_port() == 50123
    sock.bind.assert_called_once_with(("127.0.0.1", 0))
    sock.close.assert_called_once()


def test_pick_free_udp_port_releases_socket_on_bind_error():
    sock = MagicMock()
    sock.bind.side_effect = OSError(errno.EADDRNOTAVAIL, "Can't assign address")
    with (
        patch.object(bridge_mod, "socket", _fake_bridge_socket_module(sock)),
        pytest.raises(OSError),
    ):
        _pick_free_udp_port()
    sock.close.assert_called_once()
