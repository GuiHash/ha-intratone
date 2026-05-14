"""Minimal SIP UAC for Intratone.

Flow per call (see `INTRATONE_API.md` §7 and `.context/plans/phase-2-audio-oneway.md`):

    INVITE (CSeq N)            → server
                               ← 407 Proxy-Auth (challenge)
    ACK    (CSeq N)            → server
    INVITE (CSeq N+1) + Auth   → server
                               ← 200 OK + SDP (visitor's RTP endpoint)
    ACK    (CSeq N+1)          → server
    [RTP G.711 audio flows; bridged elsewhere]
    BYE    (CSeq N+2)          → server   (or ← BYE from server)
                               ← 200 OK

We don't subclass `voip_utils.SipDatagramProtocol` because it hardcodes
CSeq 50 in ACKs and only emits Opus SDP — both incompatible with the
407-retry + PCMU path Intratone requires. We do reuse its `SipMessage`
parser and `get_rtp_info` SDP parser.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from voip_utils.sip import SipMessage, get_rtp_info

from .digest_auth import build_authorization, parse_challenge

_LOGGER = logging.getLogger(__name__)
_CRLF = "\r\n"
_USER_AGENT = "intratone-ha/0.1"
_INITIAL_CSEQ = 50

# Set INTRATONE_SIP_DEBUG=1 to dump the full SIP exchange. Authorization headers
# are masked to keep credentials out of logs.
_SIP_DEBUG = bool(int(os.environ.get("INTRATONE_SIP_DEBUG", "0") or "0"))
_AUTH_MASK_RE = re.compile(
    r"(?im)^((?:proxy-)?authorization):.*$", re.MULTILINE
)


def _redact_sip(message: bytes) -> str:
    text = message.decode("utf-8", errors="replace")
    return _AUTH_MASK_RE.sub(lambda m: f"{m.group(1)}: <redacted>", text)


class CallState(Enum):
    INVITING = "INVITING"
    AUTHENTICATING = "AUTHENTICATING"
    CONFIRMED = "CONFIRMED"
    TERMINATED = "TERMINATED"


@dataclass
class CallEstablished:
    """Emitted when the visitor's RTP endpoint is known (after 200 OK + SDP)."""

    call_id: str
    remote_rtp_ip: str
    remote_rtp_port: int
    local_rtp_port: int


@dataclass
class _PendingCall:
    call_id: str
    target_uri: str
    target_addr: tuple[str, int]
    local_host: str
    local_port: int
    local_rtp_port: int
    sip_username: str
    sip_password: str
    via_branch: str
    from_tag: str
    cseq: int
    state: CallState


CallEstablishedCb = Callable[[CallEstablished], None]
CallTerminatedCb = Callable[[str], None]


class IntratoneSipClient(asyncio.DatagramProtocol):
    """SIP UAC: send INVITE, handle 407 Digest, expose RTP endpoint on 200 OK."""

    def __init__(
        self,
        local_host: str,
        local_port: int,
        on_call_established: CallEstablishedCb,
        on_call_terminated: CallTerminatedCb,
    ) -> None:
        self._local_host = local_host
        self._local_port = local_port
        self._on_call_established = on_call_established
        self._on_call_terminated = on_call_terminated
        self._transport: asyncio.DatagramTransport | None = None
        self._calls: dict[str, _PendingCall] = {}

    # --- asyncio.DatagramProtocol ---------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if _SIP_DEBUG:
            _LOGGER.info("SIP RX from %s:\n%s", addr, _redact_sip(data))
        try:
            msg = SipMessage.parse_sip(data.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 — malformed input must not crash the protocol
            _LOGGER.exception("Failed to parse SIP datagram from %s", addr)
            return

        call_id = msg.headers.get("call-id")
        if call_id is None:
            return
        call = self._calls.get(call_id)
        if call is None:
            return  # stray retransmission for a torn-down call

        if msg.method is None:
            self._handle_response(call, msg, addr)
        elif msg.method.lower() == "bye":
            self._handle_bye(call, msg, addr)

    # --- public API -----------------------------------------------------------

    def call(
        self,
        target_uri: str,
        target_host: str,
        target_port: int,
        local_rtp_port: int,
        sip_username: str,
        sip_password: str,
    ) -> str:
        """Send INVITE. Returns the Call-ID for later `hang_up`."""
        if self._transport is None:
            raise RuntimeError("Transport not connected")

        token = secrets.token_hex(8)
        # Call-ID has to stay numeric-looking (digits + '@' + IP): Intratone's
        # Asterisk rejected an INVITE with `timestamp-hexsuffix@host` with a
        # 500 even though the format is RFC-valid. We pre-multiply the
        # nanosecond timestamp with a random 32-bit suffix so collisions on
        # the same ns are statistically impossible while the ID stays digits.
        suffix = int(token[:8], 16)
        call = _PendingCall(
            call_id=f"{time.monotonic_ns()}{suffix:010d}@{self._local_host}",
            target_uri=target_uri,
            target_addr=(target_host, target_port),
            local_host=self._local_host,
            local_port=self._local_port,
            local_rtp_port=local_rtp_port,
            sip_username=sip_username,
            sip_password=sip_password,
            via_branch=f"z9hG4bK-{token}",
            from_tag=token[:8],
            cseq=_INITIAL_CSEQ,
            state=CallState.INVITING,
        )
        self._calls[call.call_id] = call
        self._send(self._build_invite(call, auth_header=None), call.target_addr)
        return call.call_id

    def hang_up(self, call_id: str) -> None:
        """End a call. Works in any state — CANCELs an in-flight INVITE or
        BYEs a confirmed one, but always tears down so the manager doesn't
        wedge (next ring would be silently dropped by the "already active"
        guard otherwise)."""
        call = self._calls.get(call_id)
        if call is None:
            return
        if call.state == CallState.CONFIRMED:
            call.cseq += 1
            self._send(self._build_bye(call), call.target_addr)
        elif call.state in (CallState.INVITING, CallState.AUTHENTICATING):
            # RFC 3261 §9: CANCEL the pending INVITE so the server stops ringing.
            self._send(self._build_cancel(call), call.target_addr)
        self._terminate(call)

    # --- response routing -----------------------------------------------------

    def _handle_response(
        self, call: _PendingCall, msg: SipMessage, source_addr: tuple[str, int]
    ) -> None:
        code = int(msg.code) if msg.code else 0

        if code < 200:
            _LOGGER.debug("Call %s: %s %s", call.call_id, code, msg.reason)
            return

        if code in (401, 407) and call.state == CallState.INVITING:
            self._handle_auth_challenge(call, msg, source_addr)
            return

        if 200 <= code < 300:
            if call.state in (CallState.INVITING, CallState.AUTHENTICATING):
                self._handle_ok(call, msg, source_addr)
            elif call.state == CallState.CONFIRMED:
                # 2xx retransmission (we didn't ACK fast enough, or Asterisk lost
                # our ACK). RFC 3261 §13.2.2.4: re-ACK silently, same dialog —
                # do NOT tear down the call.
                _LOGGER.debug(
                    "Call %s: %s retransmission — re-ACK", call.call_id, code
                )
                self._send(self._build_ack(call, msg), source_addr)
            return

        _LOGGER.warning("Call %s: %s %s — terminating", call.call_id, code, msg.reason)
        self._send(self._build_ack(call, msg), source_addr)
        self._terminate(call)

    def _handle_auth_challenge(
        self, call: _PendingCall, msg: SipMessage, source_addr: tuple[str, int]
    ) -> None:
        # RFC 3261 §17.1.1.3: ACK the non-2xx final response on same transaction.
        self._send(self._build_ack(call, msg), source_addr)

        is_proxy = "proxy-authenticate" in msg.headers
        challenge_value = msg.headers.get("proxy-authenticate") or msg.headers.get(
            "www-authenticate"
        )
        if not challenge_value:
            _LOGGER.warning("Call %s: %s without auth header", call.call_id, msg.code)
            self._terminate(call)
            return

        try:
            challenge = parse_challenge(challenge_value)
            auth_value = build_authorization(
                challenge,
                username=call.sip_username,
                password=call.sip_password,
                method="INVITE",
                uri=call.target_uri,
            )
        except ValueError:
            _LOGGER.exception("Call %s: invalid Digest challenge", call.call_id)
            self._terminate(call)
            return

        # Start a new transaction for the re-INVITE: bump CSeq AND branch.
        call.cseq += 1
        call.via_branch = f"z9hG4bK-{secrets.token_hex(8)}"
        call.state = CallState.AUTHENTICATING

        header_name = "Proxy-Authorization" if is_proxy else "Authorization"
        self._send(
            self._build_invite(call, auth_header=(header_name, auth_value)),
            call.target_addr,
        )

    def _handle_ok(
        self, call: _PendingCall, msg: SipMessage, source_addr: tuple[str, int]
    ) -> None:
        _LOGGER.info("Call %s: 200 OK — sending ACK to %s", call.call_id, source_addr)
        self._send(self._build_ack(call, msg), source_addr)

        try:
            rtp_info = get_rtp_info(msg.body)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Call %s: SDP parse failed", call.call_id)
            self._terminate(call)
            return

        call.state = CallState.CONFIRMED
        self._on_call_established(
            CallEstablished(
                call_id=call.call_id,
                remote_rtp_ip=rtp_info.rtp_ip,
                remote_rtp_port=rtp_info.rtp_port,
                local_rtp_port=call.local_rtp_port,
            )
        )

    def _handle_bye(
        self, call: _PendingCall, msg: SipMessage, source_addr: tuple[str, int]
    ) -> None:
        self._send(self._build_200_for(msg), source_addr)
        self._terminate(call)

    def _terminate(self, call: _PendingCall) -> None:
        if call.state == CallState.TERMINATED:
            return
        call.state = CallState.TERMINATED
        self._calls.pop(call.call_id, None)
        self._on_call_terminated(call.call_id)

    # --- message builders -----------------------------------------------------

    def _build_invite(
        self,
        call: _PendingCall,
        auth_header: tuple[str, str] | None,
    ) -> bytes:
        sdp = self._build_sdp(call).encode("utf-8")
        head_lines = [
            f"INVITE {call.target_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {call.local_host}:{call.local_port};branch={call.via_branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{call.sip_username}@{call.local_host}>;tag={call.from_tag}",
            f"To: <{call.target_uri}>",
            f"Call-ID: {call.call_id}",
            f"CSeq: {call.cseq} INVITE",
            f"Contact: <sip:{call.sip_username}@{call.local_host}:{call.local_port}>",
            f"User-Agent: {_USER_AGENT}",
            "Allow: INVITE, ACK, CANCEL, BYE",
            "Content-Type: application/sdp",
        ]
        if auth_header:
            head_lines.append(f"{auth_header[0]}: {auth_header[1]}")
        head_lines.append(f"Content-Length: {len(sdp)}")
        head_lines.append("")
        return (_CRLF.join(head_lines) + _CRLF).encode("utf-8") + sdp

    def _build_sdp(self, call: _PendingCall) -> str:
        session_id = call.call_id.split("@", 1)[0]
        return _CRLF.join(
            [
                "v=0",
                f"o={call.sip_username} {session_id} {session_id} IN IP4 {call.local_host}",
                "s=Intratone Call",
                f"c=IN IP4 {call.local_host}",
                "t=0 0",
                f"m=audio {call.local_rtp_port} RTP/AVP 0 8 101",
                "a=rtpmap:0 PCMU/8000",
                "a=rtpmap:8 PCMA/8000",
                "a=rtpmap:101 telephone-event/8000",
                "a=fmtp:101 0-15",
                "a=ptime:20",
                "a=sendrecv",
                "",
            ]
        )

    def _build_ack(self, call: _PendingCall, response: SipMessage) -> bytes:
        # ACK uses the same CSeq number as the responded-to request (RFC 3261 §17.1.1.3).
        lines = [
            f"ACK {call.target_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {call.local_host}:{call.local_port};branch={call.via_branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{call.sip_username}@{call.local_host}>;tag={call.from_tag}",
            f"To: {response.headers.get('to', f'<{call.target_uri}>')}",
            f"Call-ID: {call.call_id}",
            f"CSeq: {call.cseq} ACK",
            f"User-Agent: {_USER_AGENT}",
            "Content-Length: 0",
            "",
        ]
        return (_CRLF.join(lines) + _CRLF).encode("utf-8")

    def _build_cancel(self, call: _PendingCall) -> bytes:
        """Cancel a still-pending INVITE. RFC 3261 §9.1: same CSeq number as
        the INVITE being cancelled, but with CANCEL method; same Via branch."""
        lines = [
            f"CANCEL {call.target_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {call.local_host}:{call.local_port};branch={call.via_branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{call.sip_username}@{call.local_host}>;tag={call.from_tag}",
            f"To: <{call.target_uri}>",
            f"Call-ID: {call.call_id}",
            f"CSeq: {call.cseq} CANCEL",
            f"User-Agent: {_USER_AGENT}",
            "Content-Length: 0",
            "",
        ]
        return (_CRLF.join(lines) + _CRLF).encode("utf-8")

    def _build_bye(self, call: _PendingCall) -> bytes:
        lines = [
            f"BYE {call.target_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {call.local_host}:{call.local_port};branch=z9hG4bK-{secrets.token_hex(8)};rport",
            "Max-Forwards: 70",
            f"From: <sip:{call.sip_username}@{call.local_host}>;tag={call.from_tag}",
            f"To: <{call.target_uri}>",
            f"Call-ID: {call.call_id}",
            f"CSeq: {call.cseq} BYE",
            f"User-Agent: {_USER_AGENT}",
            "Content-Length: 0",
            "",
        ]
        return (_CRLF.join(lines) + _CRLF).encode("utf-8")

    def _build_200_for(self, request: SipMessage) -> bytes:
        lines = [
            "SIP/2.0 200 OK",
            f"Via: {request.headers.get('via', '')}",
            f"From: {request.headers.get('from', '')}",
            f"To: {request.headers.get('to', '')}",
            f"Call-ID: {request.headers.get('call-id', '')}",
            f"CSeq: {request.headers.get('cseq', '')}",
            f"User-Agent: {_USER_AGENT}",
            "Content-Length: 0",
            "",
        ]
        return (_CRLF.join(lines) + _CRLF).encode("utf-8")

    def _send(self, data: bytes, addr: tuple[str, int]) -> None:
        assert self._transport is not None
        if _SIP_DEBUG:
            _LOGGER.info("SIP TX to %s:\n%s", addr, _redact_sip(data))
        self._transport.sendto(data, addr)
