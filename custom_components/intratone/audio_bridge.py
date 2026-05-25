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
import os
import signal
import socket
import struct
import tempfile
import time
from typing import Callable

from .stun import build_binding_response, is_stun_binding_request

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
# Upper bound on how long to wait for ffmpeg to ANNOUNCE+RECORD into go2rtc.
# Audio-only: 100-400 ms on localhost. With VP8 video, ffmpeg must receive a
# VP8 I-frame before it can start encoding H.264 output; Intratone typically
# sends P-frames for 8-12 s before the first keyframe, so we need meaningful
# slack. 15 s covers observed worst-case keyframe latency with margin.
_FFMPEG_PUSH_READY_TIMEOUT_S = 15.0

# RTCP PLI (RFC 4585 §6.3.1) — Picture Loss Indication feedback message. The
# Intratone gateway negotiates plain `RTP/AVP` (no AVPF feedback advertised),
# but Asterisk with `nortpproxy=yes` typically still forwards inbound RTCP to
# the media source, so the doorbell hardware re-encodes with a fresh I-frame.
# Saves 7-10 s of black screen at call start.
_RTCP_PLI_HEADER_BYTE_0 = (2 << 6) | 1  # V=2, P=0, FMT=1
_RTCP_PLI_PT = 206  # PSFB — Payload-Specific Feedback
_RTCP_PLI_LENGTH_WORDS = 2  # (12 bytes / 4) - 1
# Bound the PLI burst: we expect a keyframe within 1-2 packets if the gateway
# honours it; otherwise stop wasting RTCP traffic.
_PLI_MAX_SENDS = 10
_PLI_INTERVAL_S = 1.0
# How long to wait for the first VP8 RTP packet before launching the PLI loop
# (we need the media SSRC, which we observe on incoming RTP).
_PLI_WAIT_FIRST_RTP_S = 3.0
# After the initial keyframe is received, send a periodic PLI every N seconds
# to force a fresh I-frame and let the VP8 decoder resync. During the brief
# window between each periodic PLI and the gateway's I-frame response (~1-3 s),
# pre-keyframe P-frames are filtered so the decoder starts clean each cycle.
_PLI_PERIODIC_INTERVAL_S = 30.0


def _build_pli_packet(sender_ssrc: int, media_ssrc: int) -> bytes:
    """RFC 4585 PLI feedback message (12 bytes). `media_ssrc` is the SSRC of the
    VP8 stream we want a keyframe from; `sender_ssrc` identifies us (any unique
    32-bit value, doesn't have to match an RTP SSRC since we're recv-only)."""
    return struct.pack(
        ">BBHII",
        _RTCP_PLI_HEADER_BYTE_0,
        _RTCP_PLI_PT,
        _RTCP_PLI_LENGTH_WORDS,
        sender_ssrc & 0xFFFFFFFF,
        media_ssrc & 0xFFFFFFFF,
    )


def _is_vp8_keyframe(payload: bytes) -> bool:
    """RFC 7741 VP8 RTP payload: a keyframe arrives in a packet where the
    payload descriptor has S=1 and PID=0, and the VP8 frame tag's first byte
    has its LSB clear (RFC 6386 §9.1: 0=key, 1=interframe)."""
    if len(payload) < 4:
        return False
    desc = payload[0]
    s_bit = (desc >> 4) & 1
    pid = desc & 0x07
    if not s_bit or pid != 0:
        return False
    offset = 1
    if (desc >> 7) & 1:  # X: extension byte present
        if offset >= len(payload):
            return False
        ext = payload[offset]
        offset += 1
        if (ext >> 7) & 1:  # I: PictureID present
            if offset >= len(payload):
                return False
            offset += 2 if (payload[offset] >> 7) & 1 else 1  # M bit = 16-bit PID
        if (ext >> 6) & 1:  # L: TL0PICIDX
            offset += 1
        if ((ext >> 5) & 1) or ((ext >> 4) & 1):  # T or K
            offset += 1
    if offset >= len(payload):
        return False
    return (payload[offset] & 0x01) == 0


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
        # All these are exposed to the bridge stats loop + summary on close().
        self.packets_received = 0
        self.packets_sent = 0
        self.bytes_received_total = 0
        self.stun_count = 0
        self.non_rtp_count = 0
        self.seq_gaps = 0
        self._start_time: float | None = None
        # PT → dict(count, sizes:set, ssrcs:set, first16:str)
        self.pt_stats: dict[int, dict] = {}
        # SSRC → dict(count, pts:set)
        self.ssrc_stats: dict[int, dict] = {}
        self.unique_sources: set[tuple[str, int]] = set()
        self._last_seq_by_ssrc: dict[int, int] = {}
        self._first_packet_logged = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        if self._send_keepalives:
            self._keepalive_task = asyncio.create_task(self._silence_loop())

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._start_time is None:
            self._start_time = time.monotonic()
        self.bytes_received_total += len(data)
        self.unique_sources.add(addr)

        # STUN detection + response. Intratone's Asterisk requires STUN
        # Binding responses on the audio port too (not just video) — without
        # them the server only sends comfort-noise µ-law silence (0xff*160)
        # for ~8s then stops, never sending the real downstream audio.
        # Validated 2026-05-16: spike captured 2 Binding Requests + 390
        # silence packets; with no real audio at all.
        if is_stun_binding_request(data):
            self.stun_count += 1
            if self._transport is not None:
                try:
                    response = build_binding_response(data, addr)
                    self._transport.sendto(response, addr)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("AUDIO_STUN response build/send failed")
            if self.stun_count <= 3:
                _LOGGER.debug(
                    "AUDIO_RX_STUN[#%d]: Binding Request from %s:%d total_len=%d — responded",
                    self.stun_count, addr[0], addr[1], len(data),
                )
            return

        if len(data) < _RTP_HEADER_SIZE:
            self.non_rtp_count += 1
            if self.non_rtp_count <= 5:
                _LOGGER.debug(
                    "AUDIO_RX_NONRTP[#%d]: short packet from %s:%d len=%d hex=%s",
                    self.non_rtp_count,
                    addr[0],
                    addr[1],
                    len(data),
                    data.hex(),
                )
            return

        # RFC 3550 §5.1: real header length depends on CC and X flags.
        b0 = data[0]
        b1 = data[1]
        version = (b0 >> 6) & 0x03
        if version != 2:
            self.non_rtp_count += 1
            if self.non_rtp_count <= 5:
                _LOGGER.debug(
                    "AUDIO_RX_NONRTP[#%d]: V=%d (not RTP) from %s:%d len=%d hex=%s",
                    self.non_rtp_count,
                    version,
                    addr[0],
                    addr[1],
                    len(data),
                    data[:32].hex(),
                )
            return

        cc = b0 & 0x0F
        x = (b0 >> 4) & 0x01
        marker = (b1 >> 7) & 0x01
        pt = b1 & 0x7F
        seq = int.from_bytes(data[2:4], "big")
        ts = int.from_bytes(data[4:8], "big")
        ssrc = int.from_bytes(data[8:12], "big")
        header_len = _RTP_HEADER_SIZE + cc * 4
        if x:
            if len(data) < header_len + 4:
                return
            ext_len_words = int.from_bytes(data[header_len + 2 : header_len + 4], "big")
            header_len += 4 + ext_len_words * 4
        if len(data) <= header_len:
            return
        payload = data[header_len:]
        self.packets_received += 1

        # PT stats: log on first occurrence of each PT.
        if pt not in self.pt_stats:
            self.pt_stats[pt] = {
                "count": 0,
                "sizes": set(),
                "ssrcs": set(),
                "first_seen_packet": self.packets_received,
            }
            _LOGGER.debug(
                "AUDIO_RX_NEW_PT[%d]: first packet #%d M=%d cc=%d x=%d "
                "header_len=%d payload_len=%d ssrc=0x%08x src=%s:%d first16=%s",
                pt, self.packets_received, marker, cc, x, header_len,
                len(payload), ssrc, addr[0], addr[1], payload[:16].hex(),
            )
        st = self.pt_stats[pt]
        st["count"] += 1
        st["sizes"].add(len(payload))
        st["ssrcs"].add(ssrc)

        # SSRC stats: log on first occurrence.
        if ssrc not in self.ssrc_stats:
            self.ssrc_stats[ssrc] = {"count": 0, "pts": set()}
            _LOGGER.debug(
                "AUDIO_RX_NEW_SSRC[0x%08x]: first packet #%d PT=%d payload_len=%d",
                ssrc, self.packets_received, pt, len(payload),
            )
        ss = self.ssrc_stats[ssrc]
        ss["count"] += 1
        ss["pts"].add(pt)

        # Sequence-gap detection per SSRC (small gaps only — wraparound or
        # reset SSRCs would be huge deltas).
        if ssrc in self._last_seq_by_ssrc:
            expected = (self._last_seq_by_ssrc[ssrc] + 1) & 0xFFFF
            if seq != expected:
                delta = (seq - expected) & 0xFFFF
                if 0 < delta < 1000:
                    self.seq_gaps += 1
        self._last_seq_by_ssrc[ssrc] = seq

        # First packet: full diagnostic, header + 64 byte raw hex.
        if not self._first_packet_logged:
            self._first_packet_logged = True
            _LOGGER.debug(
                "AUDIO_RX_FIRST: total=%d header=%d payload=%d V=2 PT=%d M=%d "
                "cc=%d x=%d seq=%d ts=%d ssrc=0x%08x src=%s:%d remote_advertised=%s:%d "
                "first64=%s",
                len(data), header_len, len(payload), pt, marker, cc, x,
                seq, ts, ssrc, addr[0], addr[1],
                self._remote_addr[0], self._remote_addr[1],
                data[:64].hex(),
            )

        # Every 50 packets: condensed per-packet snapshot.
        if self.packets_received % 50 == 0 and self.packets_received > 0:
            sample_min = min(payload)
            sample_max = max(payload)
            unique = len(set(payload))
            _LOGGER.debug(
                "AUDIO_RX[#%d]: PT=%d payload=%d ssrc=0x%08x src=%s:%d "
                "min=0x%02x max=0x%02x unique=%d/%d first16=%s",
                self.packets_received, pt, len(payload), ssrc, addr[0], addr[1],
                sample_min, sample_max, unique, len(payload),
                payload[:16].hex(),
            )

        try:
            self._on_ulaw(payload)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("on_ulaw callback raised")

    def dump_summary(self) -> None:
        """One-shot dump of all aggregated stats — call on close/stop."""
        elapsed = (time.monotonic() - self._start_time) if self._start_time else 0.0
        _LOGGER.info(
            "AUDIO_RX_SUMMARY: %d packets / %d bytes over %.1fs (avg %.0f B/s) "
            "from %d source(s); STUN=%d nonRTP=%d seq_gaps=%d",
            self.packets_received, self.bytes_received_total, elapsed,
            (self.bytes_received_total / elapsed) if elapsed > 0 else 0,
            len(self.unique_sources), self.stun_count, self.non_rtp_count,
            self.seq_gaps,
        )
        for src in self.unique_sources:
            _LOGGER.info("AUDIO_RX_SUMMARY: source %s:%d", src[0], src[1])
        for pt, st in sorted(self.pt_stats.items()):
            _LOGGER.info(
                "AUDIO_RX_SUMMARY: PT=%d count=%d sizes=%s ssrcs=%s (first at packet #%d)",
                pt, st["count"],
                sorted(st["sizes"]),
                [f"0x{s:08x}" for s in st["ssrcs"]],
                st["first_seen_packet"],
            )
        for ssrc, ss in self.ssrc_stats.items():
            _LOGGER.info(
                "AUDIO_RX_SUMMARY: ssrc=0x%08x count=%d pts=%s",
                ssrc, ss["count"], sorted(ss["pts"]),
            )

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


class _VideoRtpProtocol(asyncio.DatagramProtocol):
    """UDP endpoint for the video media port.

    Intratone's media gateway sends STUN Binding Requests as keepalives /
    NAT-traversal probes; we respond so the gateway considers the path valid
    and starts pushing the VP8 RTP stream. RTP packets are then forwarded
    verbatim to ffmpeg's local UDP listen port.
    """

    def __init__(self, ffmpeg_target: tuple[str, int]) -> None:
        self._ffmpeg_target = ffmpeg_target
        self._transport: asyncio.DatagramTransport | None = None
        self.stun_requests = 0
        self.rtp_packets_forwarded = 0
        self.non_rtp_count = 0
        self.unique_sources: set[tuple[str, int]] = set()
        self.pt_stats: dict[int, dict] = {}  # pt → {count, sizes:set, ssrcs:set}
        self.ssrc_stats: dict[int, dict] = {}  # ssrc → {count, pts:set}
        self._first_rtp_logged = False
        self._start_time: float | None = None
        # Set when the first VP8 keyframe (frame_tag LSB == 0) is observed in
        # the incoming stream — the PLI loop watches this to stop spamming
        # feedback once the gateway has actually delivered a fresh I-frame.
        self.keyframe_received = False
        self.first_rtp_at: float | None = None
        self.first_keyframe_at: float | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        _LOGGER.debug(
            "VIDEO_RTP_LISTEN: bound, will forward to ffmpeg at %s:%d",
            self._ffmpeg_target[0], self._ffmpeg_target[1],
        )

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._transport is None:
            return
        if self._start_time is None:
            self._start_time = time.monotonic()
        self.unique_sources.add(addr)

        if is_stun_binding_request(data):
            self.stun_requests += 1
            try:
                response = build_binding_response(data, addr)
                self._transport.sendto(response, addr)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("VIDEO_STUN: response build/send failed")
            if self.stun_requests <= 3:
                _LOGGER.debug(
                    "VIDEO_RX_STUN[#%d]: Binding Request from %s:%d total_len=%d — responded",
                    self.stun_requests, addr[0], addr[1], len(data),
                )
            return

        # Anything else needs V=2 (RTP). Non-RTP non-STUN = unexpected.
        if len(data) < _RTP_HEADER_SIZE or (data[0] >> 6) & 0x03 != 2:
            self.non_rtp_count += 1
            if self.non_rtp_count <= 5:
                _LOGGER.debug(
                    "VIDEO_RX_NONRTP[#%d]: from %s:%d len=%d hex=%s",
                    self.non_rtp_count, addr[0], addr[1], len(data),
                    data[:32].hex(),
                )
            return

        # Valid RTP — parse for diagnostics + forward to ffmpeg.
        b1 = data[1]
        marker = (b1 >> 7) & 0x01
        pt = b1 & 0x7F
        ssrc = int.from_bytes(data[8:12], "big")
        payload_len = len(data) - 12  # ignore cc/x for stat purposes

        if pt not in self.pt_stats:
            self.pt_stats[pt] = {"count": 0, "sizes": set(), "ssrcs": set()}
            _LOGGER.debug(
                "VIDEO_RX_NEW_PT[%d]: first VP8 packet #%d M=%d ssrc=0x%08x "
                "payload_len=%d src=%s:%d first16=%s",
                pt, self.rtp_packets_forwarded + 1, marker, ssrc, payload_len,
                addr[0], addr[1], data[12:28].hex(),
            )
        st = self.pt_stats[pt]
        st["count"] += 1
        st["sizes"].add(payload_len)
        st["ssrcs"].add(ssrc)

        if ssrc not in self.ssrc_stats:
            self.ssrc_stats[ssrc] = {"count": 0, "pts": set()}
            _LOGGER.debug(
                "VIDEO_RX_NEW_SSRC[0x%08x]: first packet #%d PT=%d",
                ssrc, self.rtp_packets_forwarded + 1, pt,
            )
        ss = self.ssrc_stats[ssrc]
        ss["count"] += 1
        ss["pts"].add(pt)

        if not self._first_rtp_logged:
            self._first_rtp_logged = True
            self.first_rtp_at = time.monotonic()
            _LOGGER.debug(
                "VIDEO_RX_FIRST: total=%d V=2 PT=%d M=%d ssrc=0x%08x src=%s:%d "
                "first48=%s",
                len(data), pt, marker, ssrc, addr[0], addr[1],
                data[:48].hex(),
            )

        # Keyframe detection — payload starts after the RTP header. We ignore
        # CC and X for simplicity (Intratone uses no extensions / contributing
        # sources). Stop the PLI loop once we observe the first I-frame.
        if not self.keyframe_received and _is_vp8_keyframe(data[12:]):
            self.keyframe_received = True
            self.first_keyframe_at = time.monotonic()
            elapsed_ms = (
                (self.first_keyframe_at - self._start_time) * 1000
                if self._start_time is not None
                else 0
            )
            _LOGGER.info(
                "VIDEO_KEYFRAME: VP8 I-frame received %.0fms after first RTP",
                elapsed_ms,
            )

        # Don't forward VP8 to ffmpeg until a keyframe has been received.
        # Pre-keyframe P-frames cause the VP8 decoder to enter an error state
        # with no reference frame; once stuck, the decoder rejects all
        # subsequent P-frames even after the keyframe arrives. Note: for the
        # keyframe's own first RTP packet, keyframe_received is set to True
        # above *before* we reach this check, so the keyframe itself always
        # passes through.
        if not self.keyframe_received:
            return

        try:
            if self.rtp_packets_forwarded == 0:
                _LOGGER.info(
                    "VIDEO_FORWARD_FIRST: first VP8 RTP forwarded to ffmpeg :%d",
                    self._ffmpeg_target[1],
                )
            self._transport.sendto(data, self._ffmpeg_target)
            self.rtp_packets_forwarded += 1
            if self.rtp_packets_forwarded % 50 == 0:
                _LOGGER.debug(
                    "VIDEO_RX[#%d]: PT=%d M=%d payload_len=%d ssrc=0x%08x src=%s:%d",
                    self.rtp_packets_forwarded, pt, marker, payload_len, ssrc,
                    addr[0], addr[1],
                )
        except OSError:
            pass

    def connection_lost(self, exc: Exception | None) -> None:
        self._transport = None

    def close(self) -> None:
        # Dump video summary before closing — mirrors AUDIO_RX_SUMMARY pattern.
        elapsed = (
            time.monotonic() - self._start_time
            if self._start_time is not None
            else 0.0
        )
        _LOGGER.info(
            "VIDEO_RX_SUMMARY: stun_requests=%d vp8_forwarded=%d non_rtp=%d "
            "over %.1fs from %d source(s)",
            self.stun_requests, self.rtp_packets_forwarded, self.non_rtp_count,
            elapsed, len(self.unique_sources),
        )
        for src in self.unique_sources:
            _LOGGER.info("VIDEO_RX_SUMMARY: source %s:%d", src[0], src[1])
        for pt, st in sorted(self.pt_stats.items()):
            _LOGGER.info(
                "VIDEO_RX_SUMMARY: PT=%d count=%d sizes=%s ssrcs=%s",
                pt, st["count"], sorted(st["sizes"]),
                [f"0x{s:08x}" for s in st["ssrcs"]],
            )
        if self._transport is not None:
            self._transport.close()
            self._transport = None


class _VideoRtcpProtocol(asyncio.DatagramProtocol):
    """UDP endpoint for the video RTCP channel.

    Used to send RFC 4585 PLI feedback to Intratone's gateway, asking it to
    re-emit a VP8 I-frame immediately instead of waiting for the natural
    keyframe cycle (8-12 s). Also receives any incoming RTCP (SR/SDES/RR) —
    we log it at DEBUG for diagnostics but don't act on it.
    """

    def __init__(
        self, remote_addr: tuple[str, int], local_ssrc: int = 0xDEAD1234
    ) -> None:
        self._remote_addr = remote_addr
        self._local_ssrc = local_ssrc
        self._transport: asyncio.DatagramTransport | None = None
        self.pli_sent = 0
        self.rtcp_received = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.rtcp_received += 1
        if self.rtcp_received <= 3 and len(data) >= 4:
            _LOGGER.debug(
                "VIDEO_RTCP_RX[#%d]: PT=%d len=%d from %s:%d",
                self.rtcp_received, data[1], len(data), addr[0], addr[1],
            )

    def send_pli(self, media_ssrc: int) -> None:
        if self._transport is None:
            return
        pkt = _build_pli_packet(self._local_ssrc, media_ssrc)
        try:
            self._transport.sendto(pkt, self._remote_addr)
            self.pli_sent += 1
        except OSError:
            _LOGGER.debug("VIDEO_RTCP_TX: PLI sendto failed", exc_info=True)

    def connection_lost(self, exc: Exception | None) -> None:
        self._transport = None

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


def _pick_free_udp_port() -> int:
    """Bind UDP(127.0.0.1, 0), return the assigned port, release.

    Race window between release and ffmpeg's subsequent bind is sub-millisecond;
    acceptable for single-call usage. Used to pick a private loopback port for
    ffmpeg's RTP receiver."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


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
        self._pli_task: asyncio.Task | None = None
        self._rtp: _RtpProtocol | None = None
        self._rtp_transport: asyncio.DatagramTransport | None = None
        self._video_rtp: _VideoRtpProtocol | None = None
        self._video_rtp_transport: asyncio.DatagramTransport | None = None
        self._video_rtcp: _VideoRtcpProtocol | None = None
        self._video_rtcp_transport: asyncio.DatagramTransport | None = None
        self._video_sdp_path: str | None = None
        # Total µ-law bytes written to ffmpeg stdin during the call. Compared
        # against the expected 8000 B/s consumption rate, it tells us whether
        # the bottleneck is upstream (we don't have data to push) or
        # downstream (ffmpeg/go2rtc/HomeKit/iPhone).
        self._bytes_written_to_ffmpeg = 0
        # Set by `_drain_stderr` once ffmpeg has emitted the `Output #0, rtsp,
        # to '...'` line — proof that ANNOUNCE+RECORD succeeded against go2rtc.
        # `start()` awaits this before returning, so callers (Coordinator,
        # camera entity) only hand HomeKit a URL that resolves to 200 on
        # DESCRIBE, not 404 (which HomeKit doesn't retry).
        self._ffmpeg_push_ready: asyncio.Event | None = None
        # Optional sync callback fired by `_pli_loop` when it exhausts the PLI
        # budget without seeing a keyframe — CallManager wires this to a SIP
        # re-INVITE that drops the dead video media line.
        self._on_video_failure = None

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
        video_socket=None,
        remote_video_rtp_ip: str | None = None,
        remote_video_rtp_port: int | None = None,
        video_rtcp_socket=None,
        on_video_failure=None,
    ) -> str:
        """Spawn ffmpeg + wrap a pre-bound RTP socket in an asyncio endpoint.

        Caller (CallManager) is responsible for binding sockets so the
        bind/probe race is impossible. We take ownership: on stop() or on
        bind/spawn failure the sockets are closed.

        If `video_socket` is provided AND `remote_video_rtp_port` is set, real
        VP8 video is plumbed through (STUN-responded + RTP-forwarded to ffmpeg
        over loopback). Otherwise ffmpeg falls back to a synthetic placeholder.

        Idempotent: if already running, returns the URL without restarting.
        """
        if self.is_running:
            return self.rtsp_url

        # CallManager hands us this so the PLI loop can ask for an audio-only
        # re-INVITE when VP8 keyframes never arrive — mirrors the iOS app's
        # downgrade-on-failure pattern. Sync callable, fired once per call.
        self._on_video_failure = on_video_failure

        video_enabled = (
            video_socket is not None
            and remote_video_rtp_ip is not None
            and remote_video_rtp_port is not None
        )

        ffmpeg_video_port: int | None = None
        if video_enabled:
            ffmpeg_video_port = _pick_free_udp_port()
            self._video_sdp_path = self._write_video_sdp(ffmpeg_video_port)

        self._ffmpeg_push_ready = asyncio.Event()
        self._process = await self._spawn_ffmpeg(video_sdp_path=self._video_sdp_path)
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process))
        assert self._process.stdin is not None
        ffmpeg_stdin = self._process.stdin

        spawn_t0 = time.monotonic()

        def _forward_ulaw(payload: bytes) -> None:
            if ffmpeg_stdin.is_closing():
                return
            try:
                if self._bytes_written_to_ffmpeg == 0:
                    _LOGGER.info(
                        "AUDIO_STDIN_FIRST: first µ-law byte written %.0fms after ffmpeg spawn",
                        (time.monotonic() - spawn_t0) * 1000,
                    )
                ffmpeg_stdin.write(payload)
                self._bytes_written_to_ffmpeg += len(payload)
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
            for sock in (video_socket, video_rtcp_socket):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._cleanup_video_sdp()
            raise

        if video_enabled:
            self._video_rtp = _VideoRtpProtocol(
                ffmpeg_target=("127.0.0.1", ffmpeg_video_port),
            )
            try:
                self._video_rtp_transport, _ = await loop.create_datagram_endpoint(
                    lambda: self._video_rtp,  # type: ignore[return-value]
                    sock=video_socket,
                )
            except OSError:
                _LOGGER.exception(
                    "Video RTP wrap failed — continuing audio-only"
                )
                self._video_rtp = None
                try:
                    video_socket.close()
                except OSError:
                    pass
                self._cleanup_video_sdp()

            # RTCP for video — RFC 3550 convention puts it on `rtp_port + 1`
            # for the remote side. Used to send PLI to the gateway.
            if self._video_rtp is not None and video_rtcp_socket is not None:
                remote_rtcp_addr = (remote_video_rtp_ip, remote_video_rtp_port + 1)
                self._video_rtcp = _VideoRtcpProtocol(remote_addr=remote_rtcp_addr)
                try:
                    (
                        self._video_rtcp_transport,
                        _,
                    ) = await loop.create_datagram_endpoint(
                        lambda: self._video_rtcp,  # type: ignore[return-value]
                        sock=video_rtcp_socket,
                    )
                    self._pli_task = asyncio.create_task(self._pli_loop())
                except OSError:
                    _LOGGER.exception(
                        "Video RTCP wrap failed — continuing without PLI"
                    )
                    self._video_rtcp = None
                    try:
                        video_rtcp_socket.close()
                    except OSError:
                        pass
            elif video_rtcp_socket is not None:
                # Video RTP wrap failed above — close the now-unusable RTCP sock.
                try:
                    video_rtcp_socket.close()
                except OSError:
                    pass

        local_port = rtp_socket.getsockname()[1]
        _LOGGER.info(
            "RTP audio :%d ↔ %s:%d ; video %s",
            local_port,
            remote_rtp_ip,
            remote_rtp_port,
            (
                f":{video_socket.getsockname()[1]} ↔ {remote_video_rtp_ip}:"
                f"{remote_video_rtp_port} → ffmpeg :{ffmpeg_video_port}"
                if video_enabled and self._video_rtp is not None
                else "disabled (placeholder)"
            ),
        )
        # Wait until our ffmpeg has actually pushed to go2rtc — otherwise
        # HomeKit's pull races us and fails 404, then never retries.
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                self._ffmpeg_push_ready.wait(),
                timeout=_FFMPEG_PUSH_READY_TIMEOUT_S,
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
            _LOGGER.info(
                "FFMPEG_PUSH_READY: %s consumable (waited %.0fms after spawn)",
                self.rtsp_url,
                elapsed_ms,
            )
        except asyncio.TimeoutError:
            # If the process died, surface that — much more actionable than a
            # generic timeout warning.
            if self._process is not None and self._process.returncode is not None:
                _LOGGER.error(
                    "FFMPEG_PUSH_READY: ffmpeg exited before push (rc=%s) — "
                    "check stderr lines above for the cause",
                    self._process.returncode,
                )
            else:
                _LOGGER.warning(
                    "FFMPEG_PUSH_READY: timeout after %ss — process still alive but "
                    "never emitted 'Output #0, rtsp'",
                    _FFMPEG_PUSH_READY_TIMEOUT_S,
                )
        self._stats_task = asyncio.create_task(self._log_stats_loop())
        return self.rtsp_url

    async def _pli_loop(self) -> None:
        """Send RFC 4585 PLI to the gateway until the first VP8 I-frame
        arrives or we hit the burst limit. Asterisk advertises plain RTP/AVP
        (no `a=rtcp-fb`) so this is a deviation from RFC 4585; in practice
        Cogelec's gateway forwards inbound RTCP to the doorbell hardware,
        which re-encodes with a keyframe ~50-200 ms after the first PLI.

        After the initial keyframe is received, this loop continues sending
        periodic PLIs every _PLI_PERIODIC_INTERVAL_S seconds. Each periodic
        PLI resets the forwarding gate (keyframe_received=False) so that
        pre-keyframe P-frames are dropped until the gateway responds with a
        fresh I-frame. This keeps the VP8 decoder from drifting into a stuck
        state mid-call if the reference frame is ever lost."""
        try:
            # Wait for the first RTP packet so we know the media SSRC. Without
            # it our PLI has nothing to reference and the gateway would ignore
            # it. Cap the wait — if no RTP comes in, the call is dead anyway.
            deadline = time.monotonic() + _PLI_WAIT_FIRST_RTP_S
            while (
                self._video_rtp is not None
                and not self._video_rtp.ssrc_stats
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.05)
            if self._video_rtp is None or not self._video_rtp.ssrc_stats:
                _LOGGER.debug("PLI_LOOP: no VP8 RTP within %ss — giving up",
                              _PLI_WAIT_FIRST_RTP_S)
                return
            # Pick the most-active SSRC (gateway might be a single source but
            # some Asterisk setups multiplex SSRCs over the lifetime of a call).
            media_ssrc = max(
                self._video_rtp.ssrc_stats.items(),
                key=lambda kv: kv[1]["count"],
            )[0]

            # --- Initial PLI burst to get the first keyframe ---
            got_keyframe = False
            t0 = time.monotonic()
            for i in range(_PLI_MAX_SENDS):
                if self._video_rtp is None or self._video_rtcp is None:
                    return
                if self._video_rtp.keyframe_received:
                    elapsed_ms = (time.monotonic() - t0) * 1000
                    _LOGGER.info(
                        "PLI_LOOP: keyframe arrived after %d PLI(s) in %.0fms",
                        i, elapsed_ms,
                    )
                    got_keyframe = True
                    break
                self._video_rtcp.send_pli(media_ssrc)
                if i == 0:
                    _LOGGER.info(
                        "PLI_LOOP: first PLI sent to %s for media_ssrc=0x%08x",
                        self._video_rtcp._remote_addr, media_ssrc,
                    )
                await asyncio.sleep(_PLI_INTERVAL_S)

            if not got_keyframe:
                _LOGGER.warning(
                    "PLI_LOOP: gave up after %d PLI(s) without a keyframe (gateway "
                    "may not honour RTCP feedback on RTP/AVP)", _PLI_MAX_SENDS,
                )
                # Ask CallManager to renegotiate the dialog as audio-only so
                # the gateway stops sending VP8 we can't decode.
                if self._on_video_failure is not None:
                    try:
                        self._on_video_failure()
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("on_video_failure callback raised")
                return

            # --- Periodic PLI to keep the VP8 decoder fresh ---
            # Sending a fresh I-frame every _PLI_PERIODIC_INTERVAL_S lets the
            # VP8 decoder resync if it drifted. We reset keyframe_received so
            # the forwarding gate filters P-frames until the I-frame arrives.
            while True:
                await asyncio.sleep(_PLI_PERIODIC_INTERVAL_S)
                if self._video_rtp is None or self._video_rtcp is None:
                    return
                # Reset the gate — datagram_received will drop P-frames until
                # the gateway responds with the fresh keyframe.
                self._video_rtp.keyframe_received = False
                self._video_rtcp.send_pli(media_ssrc)
                _LOGGER.debug(
                    "PLI_LOOP: periodic PLI sent (media_ssrc=0x%08x) — "
                    "waiting for fresh keyframe", media_ssrc,
                )
                # Gateway typically responds to PLI in 50-200 ms; 1 s is
                # already very conservative. If no keyframe after 1 s the
                # gateway is not honouring RTCP feedback — re-open the gate
                # immediately rather than blacking out for the full interval.
                timeout = time.monotonic() + 1.0
                while (
                    self._video_rtp is not None
                    and not self._video_rtp.keyframe_received
                    and time.monotonic() < timeout
                ):
                    await asyncio.sleep(0.05)
                if self._video_rtp is not None and self._video_rtp.keyframe_received:
                    _LOGGER.debug("PLI_LOOP: periodic keyframe received — resuming")
                else:
                    if self._video_rtp is not None:
                        self._video_rtp.keyframe_received = True
                    _LOGGER.warning(
                        "PLI_LOOP: periodic keyframe not received within 1s — "
                        "re-opening gate to avoid extended blackout"
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("PLI_LOOP crashed")

    def _write_video_sdp(self, ffmpeg_listen_port: int) -> str:
        """Write a minimal SDP describing the VP8 RTP stream we feed ffmpeg.

        ffmpeg uses this to know how to demux the loopback UDP packets we push
        from `_VideoRtpProtocol`. Returns the absolute file path."""
        sdp = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 127.0.0.1\r\n"
            "s=intratone-video\r\n"
            "c=IN IP4 127.0.0.1\r\n"
            "t=0 0\r\n"
            f"m=video {ffmpeg_listen_port} RTP/AVP 96\r\n"
            "a=rtpmap:96 VP8/90000\r\n"
        )
        fd, path = tempfile.mkstemp(prefix="intratone-video-", suffix=".sdp")
        with os.fdopen(fd, "w") as f:
            f.write(sdp)
        _LOGGER.debug(
            "VIDEO_SDP_FILE: wrote %s for ffmpeg input — listens on 127.0.0.1:%d for VP8",
            path, ffmpeg_listen_port,
        )
        return path

    def _cleanup_video_sdp(self) -> None:
        if self._video_sdp_path is not None:
            try:
                os.unlink(self._video_sdp_path)
            except OSError:
                pass
            self._video_sdp_path = None

    async def stop(self) -> None:
        """Tear down ffmpeg + RTP socket(s) + keepalive. Always safe."""
        rtp = self._rtp
        if rtp is not None:
            rtp.dump_summary()
            _LOGGER.info(
                "AUDIO_FFMPEG_SUMMARY: wrote %d bytes to stdin during call "
                "(expected ~8000 B/s for G.711 µ-law 20ms)",
                self._bytes_written_to_ffmpeg,
            )
        transport = self._rtp_transport
        video_rtp = self._video_rtp
        video_transport = self._video_rtp_transport
        video_rtcp = self._video_rtcp
        video_rtcp_transport = self._video_rtcp_transport
        process = self._process
        stderr_task = self._stderr_task
        stats_task = self._stats_task
        pli_task = self._pli_task
        if video_rtcp is not None:
            _LOGGER.info(
                "VIDEO_RTCP_SUMMARY: %d PLI sent, %d incoming RTCP received",
                video_rtcp.pli_sent, video_rtcp.rtcp_received,
            )
        self._rtp = None
        self._rtp_transport = None
        self._video_rtp = None
        self._video_rtp_transport = None
        self._video_rtcp = None
        self._video_rtcp_transport = None
        self._process = None
        self._stderr_task = None
        self._stats_task = None
        self._pli_task = None
        self._ffmpeg_push_ready = None
        self._bytes_written_to_ffmpeg = 0

        if stats_task is not None and not stats_task.done():
            stats_task.cancel()
        if pli_task is not None and not pli_task.done():
            pli_task.cancel()

        if rtp is not None:
            rtp.close()
        if transport is not None and not transport.is_closing():
            transport.close()
        if video_rtp is not None:
            video_rtp.close()
        if video_transport is not None and not video_transport.is_closing():
            video_transport.close()
        if video_rtcp is not None:
            video_rtcp.close()
        if video_rtcp_transport is not None and not video_rtcp_transport.is_closing():
            video_rtcp_transport.close()
        self._cleanup_video_sdp()

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

    async def _spawn_ffmpeg(
        self, video_sdp_path: str | None = None
    ) -> asyncio.subprocess.Process:
        # HomeKit's Camera service requires a video stream alongside audio.
        # When the Intratone server negotiated VP8 video (m=video > 0 in 200
        # OK), `video_sdp_path` points at an SDP file describing the RTP VP8
        # stream we receive over loopback; ffmpeg transcodes VP8 → H.264 for
        # HomeKit. Otherwise we synthesize a dark placeholder frame.
        video_input: list[str]
        if video_sdp_path is not None:
            video_input = [
                # `-protocol_whitelist` is required since SDP triggers UDP+RTP
                # demuxers that aren't in ffmpeg's default safe list.
                "-protocol_whitelist",
                "file,udp,rtp",
                # Force ffmpeg to start the demuxer on the first packet. The
                # SDP already declares `a=rtpmap:96 VP8/90000`, so analysis is
                # redundant. Intratone runs at ~2-5 fps and ~50 kbps; the
                # previous 50 ms / 32 KB thresholds translated into ~5 s of
                # wall-clock waiting because the low rate never filled them.
                "-analyzeduration",
                "0",
                "-probesize",
                "32",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-f",
                "sdp",
                "-i",
                video_sdp_path,
            ]
        else:
            video_input = [
                "-f",
                "lavfi",
                "-i",
                "color=color=0x202020:size=640x360:rate=10",
            ]
        args = [
            "-hide_banner",
            # `info` is needed so the `Output #0, rtsp` line reaches our stderr
            # drain (that's our "push established" signal). Real warnings are
            # still in the stream and we log everything verbatim.
            "-loglevel",
            "info",
            # Input 0: raw µ-law (G.711) at 8 kHz mono from stdin.
            # Without these flags ffmpeg falls back to the defaults
            # (analyzeduration=5s, probesize=5MB) for the stdin input. At
            # µ-law's 8 KB/s rate that's an enforced 5 s wall-clock wait
            # before stream params are confirmed — the dominant contributor
            # to the observed ~14 s startup latency before "Output #0, rtsp"
            # emits. Raw format means no analysis is required.
            "-analyzeduration",
            "0",
            "-probesize",
            "32",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "mulaw",
            "-ar",
            "8000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            # Input 1: real VP8 over RTP (via SDP) OR synthetic placeholder.
            *video_input,
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
            # 48 kHz stereo regardless of `-ac 1`. HomeKit negotiates 16 kHz
            # mono with the iPhone so encoding at the source rate keeps the
            # re-encode on the HomeKit pull side cheap.
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
            # Smaller keyframe interval (was 20) gives HomeKit a fresh I-frame
            # faster after the pull starts, so the iPhone tile shows the first
            # frame ~½ s sooner. Trade-off: ~10 % more bandwidth on the H.264
            # output (acceptable on localhost relay).
            "-g",
            "10",
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
        """Periodic visibility into the whole audio pipeline."""
        try:
            while self._rtp is not None:
                await asyncio.sleep(_STATS_INTERVAL_S)
                if self._rtp is None:
                    return
                rtp = self._rtp
                ff_alive = (
                    self._process is not None
                    and self._process.returncode is None
                )
                pt_dist = ",".join(
                    f"{pt}:{st['count']}" for pt, st in sorted(rtp.pt_stats.items())
                ) or "-"
                elapsed = (
                    time.monotonic() - rtp._start_time
                    if rtp._start_time is not None
                    else 0.0
                )
                bps = (self._bytes_written_to_ffmpeg / elapsed) if elapsed > 0 else 0
                video_info = ""
                if self._video_rtp is not None:
                    video_info = (
                        f"; video_stun={self._video_rtp.stun_requests}"
                        f" vp8={self._video_rtp.rtp_packets_forwarded}"
                    )
                _LOGGER.info(
                    "STATS: audio_rx=%d (bytes=%d, PT={%s}, ssrcs=%d, stun=%d, nonRTP=%d, "
                    "seq_gaps=%d) "
                    "audio_tx_keepalive=%d ffmpeg_stdin_wrote=%d (%.0f B/s over %.1fs) "
                    "ffmpeg_alive=%s%s",
                    rtp.packets_received, rtp.bytes_received_total, pt_dist,
                    len(rtp.ssrc_stats), rtp.stun_count, rtp.non_rtp_count, rtp.seq_gaps,
                    rtp.packets_sent,
                    self._bytes_written_to_ffmpeg, bps, elapsed,
                    ff_alive, video_info,
                )
        except asyncio.CancelledError:
            raise

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        """Read ffmpeg's stderr line by line and log it. Without this the pipe
        eventually fills (~64 KB) and ffmpeg blocks on write, stalling audio.

        Also watches for the `Output #0, rtsp, ...` marker that confirms the
        RTSP ANNOUNCE+RECORD handshake against go2rtc succeeded — once seen,
        signals `_ffmpeg_push_ready` so `start()` can return a URL HomeKit
        can actually consume."""
        if process.stderr is None:
            return
        ready_event = self._ffmpeg_push_ready
        # ffmpeg emits a lot of noise at -loglevel info (frame= progress, codec
        # init, mapping). Real problems include words like "Error", "Invalid",
        # "Failed", "Broken pipe". Forward those at WARNING so prod users see
        # them without enabling debug; everything else stays at DEBUG.
        warn_keywords = ("Error", "Invalid", "Failed", "Broken pipe", "fatal")
        # Surface a small set of ffmpeg startup milestones at INFO so a quick
        # log scan can confirm spawn / input parsing / output mux init timing
        # without enabling DEBUG. Validated 2026-05-24: with the low-latency
        # flags on both inputs, "Press [q]" emits ~280 ms after spawn (vs
        # ~14 s with the previous defaults).
        startup_markers = ("Stream mapping", "Input #0", "Input #1", "Press [q]")
        startup_t0 = time.monotonic()
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if (
                    ready_event is not None
                    and not ready_event.is_set()
                    and "Output #0" in text
                    and "rtsp" in text
                ):
                    ready_event.set()
                if any(kw in text for kw in warn_keywords):
                    _LOGGER.warning("ffmpeg: %s", text)
                elif any(m in text for m in startup_markers):
                    _LOGGER.info(
                        "FFMPEG_STARTUP[+%.0fms]: %s",
                        (time.monotonic() - startup_t0) * 1000, text,
                    )
                else:
                    _LOGGER.debug("ffmpeg: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.debug("ffmpeg stderr reader stopped", exc_info=True)
