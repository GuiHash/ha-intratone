"""RTP RX/TX bridge + ffmpeg subprocess for RTSP exposure.

Owns the local UDP RTP socket so we can both receive visitor audio AND emit
µ-law silence packets to punch NAT — without that keepalive, Asterisk on the
public internet cannot route RTP back to our private IP advertised in the SIP
SDP, the call gets BYE'd within seconds, and `/calls/{id}/answer` becomes a
no-op (returns ok but the door relay never fires).

Pipeline per call:

  Asterisk ──[RTP G.711 µ-law]──► UDP socket (ours) ─strip RTP header
                                          │
  µ-law silence ◄────[RTP keepalive]──────┘
                                          │
                                          ▼
                                  ffmpeg stdin (µ-law 8 kHz mono)
                                          │
                                          ▼
                                  rtsp://127.0.0.1:8556/intratone
                                          │
                                          ▼
                                    HomeKit Bridge

ffmpeg consumes raw µ-law directly (`-f mulaw -ar 8000 -ac 1`) so we don't
need the deprecated `audioop` module (removed from Python 3.13).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import struct
from typing import Callable

_LOGGER = logging.getLogger(__name__)

# RTP constants for G.711 µ-law @ 8000 Hz
_PCMU_PAYLOAD_TYPE = 0
_SAMPLES_PER_PACKET = 160  # 20 ms at 8000 Hz
_PACKET_INTERVAL_S = 0.020
_ULAW_SILENCE_BYTE = b"\xff"  # zero-amplitude µ-law sample
_SILENCE_PAYLOAD = _ULAW_SILENCE_BYTE * _SAMPLES_PER_PACKET
_RTP_HEADER_FMT = ">BBHII"  # version+flags, marker+PT, seq, timestamp, SSRC
_RTP_HEADER_SIZE = 12

_FFMPEG_TERMINATE_TIMEOUT = 3.0
_STATS_INTERVAL_S = 5.0  # how often to log RTP rx/tx counts during a call


def _build_rtp_packet(seq: int, timestamp: int, ssrc: int, payload: bytes) -> bytes:
    return (
        struct.pack(
            _RTP_HEADER_FMT,
            0b10000000,  # V=2, no padding/extension/CC
            _PCMU_PAYLOAD_TYPE,
            seq & 0xFFFF,
            timestamp & 0xFFFFFFFF,
            ssrc & 0xFFFFFFFF,
        )
        + payload
    )


class _RtpProtocol(asyncio.DatagramProtocol):
    """UDP endpoint for one call. Receives µ-law RTP, forwards payload, sends silence keepalives."""

    def __init__(
        self,
        remote_addr: tuple[str, int],
        on_ulaw: Callable[[bytes], None],
        ssrc: int = 0x12345678,
        send_keepalives: bool = True,
    ) -> None:
        self._remote_addr = remote_addr
        self._on_ulaw = on_ulaw
        self._ssrc = ssrc
        self._send_keepalives = send_keepalives
        self._transport: asyncio.DatagramTransport | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._send_seq = 0
        self._send_ts = 0
        self.packets_received = 0
        self.packets_sent = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        if self._send_keepalives:
            self._keepalive_task = asyncio.create_task(self._silence_loop())

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < _RTP_HEADER_SIZE:
            return
        payload = data[_RTP_HEADER_SIZE:]
        if not payload:
            return
        self.packets_received += 1
        try:
            self._on_ulaw(payload)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("on_ulaw callback raised")

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_keepalive()

    def close(self) -> None:
        self._cancel_keepalive()
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _cancel_keepalive(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    async def _silence_loop(self) -> None:
        """Send µ-law silence every 20 ms to punch NAT and trigger Asterisk
        comedia learning."""
        try:
            while self._transport is not None:
                packet = _build_rtp_packet(
                    self._send_seq, self._send_ts, self._ssrc, _SILENCE_PAYLOAD
                )
                try:
                    self._transport.sendto(packet, self._remote_addr)
                    self.packets_sent += 1
                except OSError:
                    pass  # transient send errors — keep looping
                self._send_seq = (self._send_seq + 1) & 0xFFFF
                self._send_ts = (self._send_ts + _SAMPLES_PER_PACKET) & 0xFFFFFFFF
                await asyncio.sleep(_PACKET_INTERVAL_S)
        except asyncio.CancelledError:
            raise


class AudioBridge:
    """Per-call ffmpeg + RTP pipeline. One bridge serves one call at a time."""

    def __init__(
        self,
        ffmpeg_binary: str = "ffmpeg",
        rtsp_path: str = "intratone",
        rtsp_relay_url: str = "rtsp://127.0.0.1:8554",
    ) -> None:
        self._ffmpeg_binary = ffmpeg_binary
        self._rtsp_path = rtsp_path
        # External RTSP server (go2rtc by default) we PUSH to. HomeKit pulls
        # from the same URL. ffmpeg's `-rtsp_flags listen` mode is broken in
        # recent builds (tries to connect instead of listen), so we relay
        # through go2rtc which handles the (re)connect lifecycle cleanly.
        self._rtsp_relay_url = rtsp_relay_url.rstrip("/")
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stats_task: asyncio.Task | None = None
        self._rtp: _RtpProtocol | None = None
        self._rtp_transport: asyncio.DatagramTransport | None = None

    @property
    def rtsp_url(self) -> str:
        return f"{self._rtsp_relay_url}/{self._rtsp_path}"

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def packets_received(self) -> int:
        return self._rtp.packets_received if self._rtp else 0

    @property
    def packets_sent(self) -> int:
        return self._rtp.packets_sent if self._rtp else 0

    async def start(
        self,
        rtp_socket,
        remote_rtp_ip: str,
        remote_rtp_port: int,
    ) -> str:
        """Spawn ffmpeg + wrap a pre-bound RTP socket in an asyncio endpoint.

        Caller (CallManager) is responsible for binding the socket so the
        bind/probe race is impossible. We take ownership: on stop() or on
        bind/spawn failure the socket is closed.

        Idempotent: if already running, returns the URL without restarting.
        """
        if self.is_running:
            return self.rtsp_url

        self._process = await self._spawn_ffmpeg()
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process))
        assert self._process.stdin is not None
        ffmpeg_stdin = self._process.stdin

        def _forward_ulaw(payload: bytes) -> None:
            if ffmpeg_stdin.is_closing():
                return
            try:
                ffmpeg_stdin.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        self._rtp = _RtpProtocol(
            remote_addr=(remote_rtp_ip, remote_rtp_port),
            on_ulaw=_forward_ulaw,
        )
        loop = asyncio.get_running_loop()
        try:
            self._rtp_transport, _ = await loop.create_datagram_endpoint(
                lambda: self._rtp,  # type: ignore[return-value]
                sock=rtp_socket,
            )
        except OSError:
            _LOGGER.exception("RTP wrap failed — killing ffmpeg + closing socket")
            await self._kill_ffmpeg_now()
            self._rtp = None
            try:
                rtp_socket.close()
            except OSError:
                pass
            raise
        local_port = rtp_socket.getsockname()[1]

        _LOGGER.info(
            "RTP endpoint bound on :%d, peer %s:%d, keepalive started",
            local_port,
            remote_rtp_ip,
            remote_rtp_port,
        )
        self._stats_task = asyncio.create_task(self._log_stats_loop())
        return self.rtsp_url

    async def stop(self) -> None:
        """Tear down ffmpeg + RTP socket + keepalive. Always safe."""
        rtp = self._rtp
        transport = self._rtp_transport
        process = self._process
        stderr_task = self._stderr_task
        stats_task = self._stats_task
        self._rtp = None
        self._rtp_transport = None
        self._process = None
        self._stderr_task = None
        self._stats_task = None

        if stats_task is not None and not stats_task.done():
            stats_task.cancel()

        if rtp is not None:
            rtp.close()
        if transport is not None and not transport.is_closing():
            transport.close()

        if process is not None and process.returncode is None:
            if process.stdin is not None and not process.stdin.is_closing():
                try:
                    process.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(
                    process.wait(), timeout=_FFMPEG_TERMINATE_TIMEOUT
                )
            except asyncio.TimeoutError:
                _LOGGER.warning("ffmpeg did not exit on SIGTERM; killing")
                process.kill()
                await process.wait()

        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _kill_ffmpeg_now(self) -> None:
        """Cleanup path for partial start failures — no SIGTERM grace."""
        process = self._process
        stderr_task = self._stderr_task
        self._process = None
        self._stderr_task = None
        if process is not None and process.returncode is None:
            try:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()

    async def _spawn_ffmpeg(self) -> asyncio.subprocess.Process:
        # HomeKit's Camera service requires a video stream alongside audio,
        # even if the underlying intercom has no camera (Intratone is
        # audio-only over SIP). We synthesize a dark placeholder frame at low
        # fps/bitrate so HomeKit's ffmpeg pipeline can still `-map 0:v:0`.
        args = [
            "-hide_banner",
            "-loglevel",
            "warning",
            # Input 0: raw µ-law (G.711) at 8 kHz mono from stdin.
            "-f",
            "mulaw",
            "-ar",
            "8000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            # Input 1: synthetic dark-gray placeholder video at 10 fps, 640x360.
            "-f",
            "lavfi",
            "-i",
            "color=color=0x202020:size=640x360:rate=10",
            # Map: audio from input 0, video from input 1.
            "-map",
            "0:a",
            "-c:a",
            "libopus",
            "-application",
            "voip",
            "-b:a",
            "24k",
            # Per-stream specifiers (`:a:0`) force libopus to obey: without
            # the stream specifier ffmpeg's libopus wrapper silently emits
            # 48 kHz stereo regardless of `-ac 1`.
            "-ar:a:0",
            "16000",
            "-ac:a:0",
            "1",
            "-channel_layout:a:0",
            "mono",
            "-map",
            "1:v",
            "-c:v",
            "libx264",
            "-tune",
            "zerolatency",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "baseline",
            "-level",
            "3.1",
            "-g",
            "20",
            "-rtsp_transport",
            "tcp",
            "-f",
            "rtsp",
            self.rtsp_url,
        ]
        _LOGGER.debug("Spawning ffmpeg: %s %s", self._ffmpeg_binary, " ".join(args))
        return await asyncio.create_subprocess_exec(
            self._ffmpeg_binary,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _log_stats_loop(self) -> None:
        """Periodic visibility into RTP traffic. If `received` stays at 0 while
        `sent` grows, NAT punching has not yet succeeded — Asterisk hasn't
        learned our public endpoint."""
        try:
            while self._rtp is not None:
                await asyncio.sleep(_STATS_INTERVAL_S)
                if self._rtp is None:
                    return
                _LOGGER.info(
                    "RTP stats: received=%d, sent=%d",
                    self._rtp.packets_received,
                    self._rtp.packets_sent,
                )
        except asyncio.CancelledError:
            raise

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        """Read ffmpeg's stderr line by line and log it. Without this the pipe
        eventually fills (~64 KB) and ffmpeg blocks on write, stalling audio."""
        if process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                _LOGGER.warning("ffmpeg: %s", line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.debug("ffmpeg stderr reader stopped", exc_info=True)
