"""AudioBridge tests — RTP protocol + ffmpeg subprocess lifecycle.

We test the `_RtpProtocol` directly with a fake transport (no real sockets,
pytest-socket blocks them) and the ffmpeg lifecycle with mocked subprocess.
"""

from __future__ import annotations

import asyncio
import os
import signal
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.intratone.audio_bridge import (
    _PCMU_PAYLOAD_TYPE,
    _RTCP_PLI_PT,
    _RTP_HEADER_FMT,
    _RTP_HEADER_SIZE,
    _SAMPLES_PER_PACKET,
    _ULAW_SILENCE_BYTE,
    AudioBridge,
    BridgeStoppedError,
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

    assert getattr(fake_process, "_last_signal") == signal.SIGTERM
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
    spawned with `-f sdp -i <path>` + `-map 1:v`, so without VP8 packets the
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
    assert bridge._video_sdp_path is None  # temp SDP cleaned up
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


async def test_prewarm_ffmpeg_is_reused_by_video_start(fake_datagram_endpoint):
    """prewarm() spawns the video-SDP ffmpeg during SIP negotiation; a video
    start() must reuse that process instead of spawning a second one."""
    with _patch_spawn([_make_fake_process()]) as spawn, _patch_video_port():
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await asyncio.sleep(0)
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
        assert bridge._video_sdp_path is None  # temp SDP cleaned up


async def test_prewarm_discarded_when_server_rejects_video(fake_datagram_endpoint):
    """If the 200 OK carries no video, the prewarmed video-SDP ffmpeg must be
    killed and the lavfi-placeholder variant spawned instead."""
    prewarm_proc = _make_fake_process()
    call_proc = _make_fake_process()
    sdp_paths: list[str] = []
    orig_write_sdp = AudioBridge._write_video_sdp

    def _capture_sdp(self, port):
        path = orig_write_sdp(self, port)
        sdp_paths.append(path)
        return path

    with (
        _patch_spawn([prewarm_proc, call_proc]) as spawn,
        _patch_video_port(),
        patch.object(AudioBridge, "_write_video_sdp", _capture_sdp),
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
        assert spawn.await_count == 2
        prewarm_proc.kill.assert_called_once()
        second_args = " ".join(spawn.await_args_list[1].args[1:])
        assert "-f lavfi" in second_args
        assert "-f sdp" not in second_args
        # The prewarm's temp SDP file must not leak.
        assert sdp_paths and not os.path.exists(sdp_paths[0])
        await bridge.stop()


async def test_stop_discards_unused_prewarm():
    """A call that dies before 200 OK never consumes the prewarm — stop()
    must kill the process and clean the temp SDP file."""
    proc = _make_fake_process()
    sdp_paths: list[str] = []
    orig_write_sdp = AudioBridge._write_video_sdp

    def _capture_sdp(self, port):
        path = orig_write_sdp(self, port)
        sdp_paths.append(path)
        return path

    with (
        _patch_spawn([proc]),
        _patch_video_port(),
        patch.object(AudioBridge, "_write_video_sdp", _capture_sdp),
    ):
        bridge = AudioBridge()
        bridge.prewarm(video=True)
        await asyncio.sleep(0)
        await bridge.stop()

    proc.kill.assert_called_once()
    assert not bridge.is_running
    assert bridge._prewarm_task is None
    assert sdp_paths and not os.path.exists(sdp_paths[0])


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
        for _ in range(10):
            await asyncio.sleep(0)
            if proc.kill.called:
                break
        proc.kill.assert_called_once()
        assert bridge._prewarm_task is None
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


async def test_pli_loop_gives_up_without_rtp_and_sends_nothing():
    """No VP8 RTP within the first-RTP window → loop exits without sending a
    single PLI and without firing the video-failure callback."""
    failure = MagicMock()
    bridge, rtcp_transport = _bridge_with_video(on_video_failure=failure)
    with patch(
        "custom_components.intratone.audio_bridge._PLI_WAIT_FIRST_RTP_S", 0.01
    ):
        await asyncio.wait_for(bridge._pli_loop(), timeout=2)
    assert rtcp_transport.sent == []
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


async def test_audio_rx_summary_detail_lines_are_debug(caplog):
    """README promises diagnostics at debug level. Keep ONE `AUDIO_RX_SUMMARY`
    aggregate line at INFO (troubleshooting docs grep for the marker) and
    demote the per-source / per-PT / per-SSRC detail loops to DEBUG."""
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
    assert len([r for r in summary if r.levelno == logging.INFO]) == 1
    assert [r for r in summary if r.levelno == logging.DEBUG]


async def test_video_rx_summary_detail_lines_are_debug(caplog):
    """Same contract for the video side: one `VIDEO_RX_SUMMARY` marker line
    at INFO, per-source / per-PT detail at DEBUG."""
    import logging

    proto = _VideoRtpProtocol(ffmpeg_target=("127.0.0.1", 12345))
    proto.connection_made(_FakeTransport())
    proto.datagram_received(_VP8_KEYFRAME_PKT, ("178.32.84.135", 52982))

    with caplog.at_level(
        logging.DEBUG, logger="custom_components.intratone.audio_bridge"
    ):
        proto.close()

    summary = [r for r in caplog.records if "VIDEO_RX_SUMMARY" in r.message]
    assert len([r for r in summary if r.levelno == logging.INFO]) == 1
    assert [r for r in summary if r.levelno == logging.DEBUG]
