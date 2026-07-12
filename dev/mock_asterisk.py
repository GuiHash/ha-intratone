#!/usr/bin/env python3
"""Tiny mock Asterisk for Phase 2 end-to-end testing without the real intercom.

Plays the role of `sip:LOGIN_TO_CALL@127.0.0.1:5060` — accepts an INVITE,
responds 100/180/200 + SDP, then streams a sine 440Hz over RTP G.711 µ-law
to the address the client advertised in its SDP. Logs the silence keepalive
RTP packets it receives (which is what NAT comedia would normally trigger).

Usage:
    python3 dev/mock_asterisk.py [--sip-port 5060] [--rtp-port 16500] [--digest]

Then in HA dev: trigger `intratone.simulate_ring` with `sip_server_ip:
127.0.0.1` and the mock server takes over.

Limitations:
- One call at a time
- No transaction layer (matches stay open until BYE or process exit)
- Digest auth is optional (--digest sends 407 first)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import re
import socket
import struct
import time

_LOGGER = logging.getLogger("mock_asterisk")
_CRLF = "\r\n"

_RTP_HEADER_FMT = ">BBHII"
_RTP_HEADER_SIZE = 12
_PCMU_PAYLOAD_TYPE = 0
_SAMPLES_PER_PACKET = 160  # 20 ms at 8 kHz
_PACKET_INTERVAL_S = 0.020

# Bias values from G.711 µ-law encoding spec.
_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635


def _lin2ulaw(sample: int) -> int:
    """Encode a single 16-bit signed PCM sample to a µ-law byte. Small and
    self-contained so we don't depend on `audioop` (removed in Python 3.13)."""
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    if sample > _ULAW_CLIP:
        sample = _ULAW_CLIP
    sample += _ULAW_BIAS
    exponent = 7
    mask = 0x4000
    while exponent and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _sine_ulaw_frame(start_sample: int, frequency_hz: int = 440) -> bytes:
    """Return 160 µ-law bytes (20 ms @ 8 kHz) of a sine at `frequency_hz`."""
    out = bytearray(_SAMPLES_PER_PACKET)
    for i in range(_SAMPLES_PER_PACKET):
        t = (start_sample + i) / 8000.0
        sample = int(0.5 * 32767 * math.sin(2 * math.pi * frequency_hz * t))
        out[i] = _lin2ulaw(sample)
    return bytes(out)


def _parse_sdp_audio_endpoint(body: str) -> tuple[str, int] | None:
    """Extract (host, port) for the client's RTP audio from an SDP body."""
    host = None
    port = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            host = line.split()[-1]
        elif line.startswith("m=audio "):
            try:
                port = int(line.split()[1])
            except (IndexError, ValueError):
                return None
    if host and port:
        return host, port
    return None


def _parse_headers(message: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in message.split(_CRLF):
        if ":" in line:
            k, _, v = line.partition(":")
            headers.setdefault(k.strip().lower(), v.strip())
    return headers


def _parse_request_method(message: str) -> str | None:
    first_line = message.split(_CRLF, 1)[0]
    match = re.match(r"^([A-Z]+)\s", first_line)
    return match.group(1) if match else None


def _split_message(data: bytes) -> tuple[str, str]:
    text = data.decode("utf-8", errors="replace")
    head, _, body = text.partition(_CRLF * 2)
    return head, body


class MockAsterisk(asyncio.DatagramProtocol):
    def __init__(self, sip_port: int, rtp_port: int, require_digest: bool) -> None:
        self._sip_port = sip_port
        self._rtp_port = rtp_port
        self._require_digest = require_digest
        self._transport: asyncio.DatagramTransport | None = None
        self._rtp_task: asyncio.Task | None = None
        self._rtp_socket: socket.socket | None = None
        self._rtp_packets_received = 0
        self._challenge_issued: dict[str, bool] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        _LOGGER.info("Mock Asterisk SIP listening on :%d", self._sip_port)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        head, body = _split_message(data)
        method = _parse_request_method(head)
        if method is None:
            _LOGGER.debug("Ignoring non-request from %s:\n%s", addr, head)
            return
        headers = _parse_headers(head)
        _LOGGER.info("← %s from %s (call-id=%s)", method, addr, headers.get("call-id", "?"))

        if method == "INVITE":
            asyncio.create_task(self._handle_invite(head, headers, body, addr))
        elif method == "ACK":
            _LOGGER.info("✓ ACK received — call confirmed")
        elif method == "BYE":
            self._send_200_for_request(headers, addr)
            self._stop_rtp()
            _LOGGER.info("◀ BYE received — RTP stopped")
        elif method == "CANCEL":
            self._send_response(
                addr, 200, "OK", headers, extra={"CSeq": headers.get("cseq", "0 CANCEL")}
            )
            # Per RFC also send 487 to the original INVITE — skipped for simplicity.
            self._stop_rtp()
        else:
            _LOGGER.debug("Unhandled method %s", method)

    async def _handle_invite(
        self,
        head: str,
        headers: dict[str, str],
        body: str,
        addr: tuple[str, int],
    ) -> None:
        call_id = headers.get("call-id", "")
        # 100 Trying immediately
        self._send_response(addr, 100, "Trying", headers)
        await asyncio.sleep(0.05)

        if self._require_digest and not self._challenge_issued.get(call_id):
            # 407 Proxy Authentication Required (mock with a fixed challenge)
            self._challenge_issued[call_id] = True
            self._send_response(
                addr,
                407,
                "Proxy Authentication Required",
                headers,
                extra={
                    "Proxy-Authenticate": 'Digest realm="asterisk", nonce="N0NCE-MOCK"',
                },
            )
            return

        # 180 Ringing
        await asyncio.sleep(0.1)
        self._send_response(addr, 180, "Ringing", headers)
        await asyncio.sleep(0.2)

        # Parse the client's RTP endpoint from its SDP, then 200 OK with our SDP.
        client_rtp = _parse_sdp_audio_endpoint(body)
        if client_rtp is None:
            _LOGGER.warning("INVITE has no parseable audio endpoint — refusing")
            self._send_response(addr, 488, "Not Acceptable Here", headers)
            return

        sdp_body = _CRLF.join(
            [
                "v=0",
                "o=mock 1 1 IN IP4 127.0.0.1",
                "s=Mock Asterisk",
                "c=IN IP4 127.0.0.1",
                "t=0 0",
                f"m=audio {self._rtp_port} RTP/AVP 0",
                "a=rtpmap:0 PCMU/8000",
                "a=sendrecv",
                "",
            ]
        )
        self._send_response(
            addr,
            200,
            "OK",
            headers,
            body=sdp_body,
            extra={"Content-Type": "application/sdp"},
        )

        # Start sending RTP toward the client's declared endpoint.
        if self._rtp_task is None:
            self._rtp_task = asyncio.create_task(self._stream_sine(client_rtp))

    def _send_response(
        self,
        addr: tuple[str, int],
        code: int,
        reason: str,
        request_headers: dict[str, str],
        *,
        body: str = "",
        extra: dict[str, str] | None = None,
    ) -> None:
        lines = [f"SIP/2.0 {code} {reason}"]
        for header in ("via", "from", "to", "call-id", "cseq"):
            value = request_headers.get(header)
            if value:
                # Capitalize header for cosmetics
                lines.append(f"{header.title()}: {value}")
        if extra:
            for k, v in extra.items():
                lines.append(f"{k}: {v}")
        body_bytes = body.encode("utf-8")
        lines.append(f"Content-Length: {len(body_bytes)}")
        lines.append("")
        msg = (_CRLF.join(lines) + _CRLF).encode("utf-8") + body_bytes
        assert self._transport is not None
        self._transport.sendto(msg, addr)
        _LOGGER.info("→ %d %s to %s", code, reason, addr)

    def _send_200_for_request(
        self, request_headers: dict[str, str], addr: tuple[str, int]
    ) -> None:
        self._send_response(addr, 200, "OK", request_headers)

    async def _stream_sine(self, target: tuple[str, int]) -> None:
        """Send a sine 440 Hz µ-law RTP stream + listen for the client's keepalive."""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(("0.0.0.0", self._rtp_port))
        self._rtp_socket = sock

        seq = 0
        timestamp = 0
        ssrc = 0xDEADBEEF
        sample_pos = 0
        last_stat_log = time.monotonic()

        async def _receiver() -> None:
            while self._rtp_socket is not None:
                try:
                    data = await loop.sock_recv(sock, 2048)
                except (ConnectionResetError, OSError):
                    return
                if data:
                    self._rtp_packets_received += 1

        recv_task = asyncio.create_task(_receiver())

        _LOGGER.info("▶ Streaming sine 440Hz to %s", target)
        try:
            while True:
                payload = _sine_ulaw_frame(sample_pos)
                packet = (
                    struct.pack(
                        _RTP_HEADER_FMT,
                        0b10000000,
                        _PCMU_PAYLOAD_TYPE,
                        seq & 0xFFFF,
                        timestamp & 0xFFFFFFFF,
                        ssrc,
                    )
                    + payload
                )
                try:
                    sock.sendto(packet, target)
                except OSError:
                    pass
                seq += 1
                timestamp += _SAMPLES_PER_PACKET
                sample_pos += _SAMPLES_PER_PACKET
                now = time.monotonic()
                if now - last_stat_log > 5:
                    _LOGGER.info(
                        "RTP: sent=%d, recv (from client)=%d",
                        seq,
                        self._rtp_packets_received,
                    )
                    last_stat_log = now
                await asyncio.sleep(_PACKET_INTERVAL_S)
        except asyncio.CancelledError:
            recv_task.cancel()
            raise
        finally:
            sock.close()
            self._rtp_socket = None

    def _stop_rtp(self) -> None:
        if self._rtp_task is not None and not self._rtp_task.done():
            self._rtp_task.cancel()
        self._rtp_task = None


async def _main(sip_port: int, rtp_port: int, require_digest: bool) -> None:
    loop = asyncio.get_running_loop()
    proto = MockAsterisk(sip_port, rtp_port, require_digest)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: proto, local_addr=("0.0.0.0", sip_port)
    )
    try:
        await asyncio.Event().wait()  # run forever
    finally:
        transport.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sip-port", type=int, default=5060)
    parser.add_argument("--rtp-port", type=int, default=16500)
    parser.add_argument(
        "--digest",
        action="store_true",
        help="Send 407 Proxy-Authenticate on first INVITE (to exercise the auth retry path).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_main(args.sip_port, args.rtp_port, args.digest))
    except KeyboardInterrupt:
        pass
