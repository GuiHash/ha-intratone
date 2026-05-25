"""SIP UAC over TCP for Intratone.

The Cogelec / Intratone Android app (liblinphone + belle-sip 1.6.1) registers
TCP-only with the server (`Core.setUdpPort(0)` disables UDP entirely). The
INVITE, ACK, BYE and the in-dialog MESSAGE that triggers `opendoor:*` all ride
the same TCP connection — RFC 5923 connection reuse is implicit via the
server's Contact `;alias=...` hint. Our Python UAC mirrors that: one TCP
connection per call, dropped on BYE.

Flow per call:

    INVITE (CSeq N)            → server
                               ← 407 Proxy-Auth (challenge)
    ACK    (CSeq N)            → server
    INVITE (CSeq N+1) + Auth   → server
                               ← 200 OK + SDP (visitor's RTP endpoint)
    ACK    (CSeq N+1)          → server
    [RTP G.711 audio flows; bridged elsewhere]
    MESSAGE (CSeq N+2) opendoor:* → server   (user tapped Unlock)
                               ← 200 OK
    BYE    (CSeq N+3)          → server   (or ← BYE from server)
                               ← 200 OK
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from voip_utils.sip import SipMessage, get_rtp_info

from .digest_auth import build_authorization, parse_challenge

_LOGGER = logging.getLogger(__name__)
_CRLF = "\r\n"
# Mimic the official Cogelec app's User-Agent — belle-sip 1.6.1 servers don't
# inspect this in practice, but if any UA allow-list ever lands server-side
# we'd rather look like the legit client. The app emits
# `AndroidPhone/<appVersion>` plus belle-sip auto-appends its lib token.
_USER_AGENT = "AndroidPhone/2.6.0 (belle-sip/5.4.100)"
_INITIAL_CSEQ = 50
# Stable per-process GRUU instance ID. RFC 5626 §4.1: clients without a REGISTER
# binding can still emit `+sip.instance="<urn:uuid:...>"` to look like a normal
# belle-sip endpoint. One UUID per process is enough.
_SIP_INSTANCE = f'"<urn:uuid:{uuid.uuid4()}>"'
# RFC 4028 session timer — the Cogelec server BYEs short calls if we don't
# negotiate a session refresh. `refresher=uas` lets the server own the periodic
# re-INVITE refresh; we just answer 200 OK to its in-dialog INVITE.
_SESSION_EXPIRES_S = 1800

_AUTH_MASK_RE = re.compile(
    r"(?im)^((?:proxy-)?authorization):.*$", re.MULTILINE
)
# Extract every Via header from a raw SIP request — we need all of them in the
# response per RFC 3261 §17.2.1, but voip_utils' SipMessage uses a single-value
# dict that collapses multi-Via into one entry.
_VIA_RE = re.compile(r"^Via:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_CONTENT_LENGTH_RE = re.compile(
    r"^Content-Length:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE
)
# Record-Route headers (RFC 3261 §16.6.4) — server-side B2BUAs like blocip
# typically insert one or more so they stay in the dialog path. Multiple values
# may appear as separate Record-Route lines or comma-separated in one line.
_RECORD_ROUTE_RE = re.compile(
    r"^Record-Route:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
# `m=video <port> ...` in an SDP body. Port can legitimately be 0 (= media line
# rejected by the answerer per RFC 3264 §6) — caller must check for > 0.
_M_VIDEO_RE = re.compile(r"^m=video\s+(\d+)\b", re.MULTILINE)
# Connection IP at the session level (`c=IN IP4 1.2.3.4`). SDP allows a per-
# media `c=` override but Intratone's 200 OK has it only at session level.
_SDP_CONN_IP_RE = re.compile(r"^c=IN\s+IP4\s+(\S+)", re.MULTILINE)


def _extract_video_endpoint(sdp_body: str | bytes) -> tuple[str, int] | None:
    """Return (ip, port) from the m=video line, or None if absent / rejected."""
    text = sdp_body.decode("utf-8", errors="replace") if isinstance(sdp_body, bytes) else sdp_body
    port_match = _M_VIDEO_RE.search(text)
    if not port_match:
        return None
    port = int(port_match.group(1))
    if port == 0:
        return None
    ip_match = _SDP_CONN_IP_RE.search(text)
    if not ip_match:
        return None
    return ip_match.group(1), port


def _redact_sip(message: bytes) -> str:
    text = message.decode("utf-8", errors="replace")
    return _AUTH_MASK_RE.sub(lambda m: f"{m.group(1)}: <redacted>", text)


def _extract_via_headers(raw_data: bytes) -> list[str]:
    text = raw_data.decode("utf-8", errors="replace")
    return [m.group(1) for m in _VIA_RE.finditer(text)]


def _extract_record_routes(raw_data: bytes) -> list[str]:
    """Pull every Record-Route URI from a SIP message, preserving order.

    Returns the raw URI strings (e.g. `<sip:proxy@1.2.3.4;lr>`). Caller is
    responsible for reversing for client-side route set (RFC 3261 §12.1.2).
    """
    text = raw_data.decode("utf-8", errors="replace")
    routes: list[str] = []
    for line in _RECORD_ROUTE_RE.findall(text):
        # A single header line may carry multiple comma-separated values.
        for entry in _split_route_values(line):
            entry = entry.strip()
            if entry:
                routes.append(entry)
    return routes


def _split_route_values(line: str) -> list[str]:
    """Split a Record-Route/Route line on commas that are OUTSIDE angle
    brackets — URI parameters can contain commas inside `<...>`."""
    out: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(line):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            out.append(line[start:i])
            start = i + 1
    out.append(line[start:])
    return out


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
    # Video media endpoint, set only if the server accepted our m=video offer
    # (port > 0 in its 200 OK SDP). Same IP as audio in practice (Intratone's
    # gateway exposes both on 178.32.84.x) but parsed independently so a future
    # change won't surprise us.
    remote_video_rtp_ip: str | None = None
    remote_video_rtp_port: int | None = None
    local_video_rtp_port: int | None = None


@dataclass
class _PendingCall:
    call_id: str
    target_uri: str
    local_host: str
    local_port: int
    local_rtp_port: int
    sip_username: str
    sip_password: str
    via_branch: str
    from_tag: str
    cseq: int
    state: CallState
    # When set, the SDP offer adds an `m=video VP8` line. The server may
    # accept (m=video > 0 in 200 OK) or reject (port 0 / absent). CallManager
    # always allocates this socket so we can transparently fall back to
    # audio-only when an account doesn't have video.
    local_video_rtp_port: int | None = None
    # Server's tag from the To: header of the 200 OK on this dialog. Required
    # for the To header of in-dialog requests (MESSAGE, BYE).
    remote_to_header: str | None = None
    # Remote target URI from the Contact header of the 200 OK (RFC 3261
    # §12.2.1.1). In-dialog requests use this as their Request-URI.
    remote_target_uri: str | None = None
    # Route set = reversed Record-Route headers from the 200 OK (RFC 3261
    # §12.1.2). Emitted as `Route:` on every in-dialog request (ACK on 2xx,
    # MESSAGE, BYE). Without this, B2BUAs like blocip cannot route in-dialog
    # requests to the right downstream UAS — the MESSAGE silently goes to the
    # wrong endpoint and the door never opens.
    route_set: list[str] | None = None
    # True while an in-dialog re-INVITE is outstanding. Prevents stacking
    # multiple re-INVITEs (the bridge's PLI loop only fires once per call,
    # but defensive: a 491 Request Pending would loop forever otherwise).
    reinvite_in_progress: bool = False


CallEstablishedCb = Callable[[CallEstablished], None]
CallTerminatedCb = Callable[[str], None]


class IntratoneSipClient(asyncio.Protocol):
    """SIP UAC over a single TCP connection. One instance per call.

    The CallManager opens the TCP connection (`loop.create_connection`) and
    hands the protocol instance to us; we then send INVITE on
    `connection_made`. The same TCP socket carries every subsequent in-dialog
    request — that's how the official Cogelec app behaves (in-dialog
    `ChatRoom.send()` from `Call.getChatRoom()` reuses the dialog's transport).
    """

    def __init__(
        self,
        local_host: str,
        on_call_established: CallEstablishedCb,
        on_call_terminated: CallTerminatedCb,
    ) -> None:
        self._local_host = local_host
        self._on_call_established = on_call_established
        self._on_call_terminated = on_call_terminated
        self._transport: asyncio.Transport | None = None
        self._call: _PendingCall | None = None
        # TCP is a stream — accumulate until we have a full SIP message
        # (start-line + headers + Content-Length bytes of body), then dispatch.
        self._rx_buf = bytearray()

    # --- asyncio.Protocol -----------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def connection_lost(self, exc: BaseException | None) -> None:
        if exc is not None:
            _LOGGER.info("SIP TCP connection lost: %s", exc)
        if self._call is not None and self._call.state != CallState.TERMINATED:
            self._terminate(self._call)
        self._transport = None

    def data_received(self, data: bytes) -> None:
        self._rx_buf.extend(data)
        while True:
            msg = self._extract_one_message()
            if msg is None:
                return
            raw, parsed = msg
            _LOGGER.debug("SIP RX:\n%s", _redact_sip(raw))
            self._dispatch(parsed, raw)

    def _extract_one_message(self) -> tuple[bytes, SipMessage] | None:
        """Pop one complete SIP message off the buffer or return None.

        SIP-over-TCP framing (RFC 3261 §18.3): each message is a start-line +
        headers + CRLF + optional body of Content-Length bytes. Multiple
        messages may arrive in one TCP read.
        """
        header_end = self._rx_buf.find(b"\r\n\r\n")
        if header_end == -1:
            return None
        header_bytes = bytes(self._rx_buf[: header_end + 4])
        # Content-Length is mandatory in SIP/TCP per RFC 3261 §18.3, but defend
        # against absent header (treat as 0) since some intermediaries strip it.
        cl_match = _CONTENT_LENGTH_RE.search(header_bytes.decode(errors="replace"))
        body_len = int(cl_match.group(1)) if cl_match else 0
        total = header_end + 4 + body_len
        if len(self._rx_buf) < total:
            return None
        raw = bytes(self._rx_buf[:total])
        del self._rx_buf[:total]
        try:
            parsed = SipMessage.parse_sip(raw.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 — malformed input must not crash
            _LOGGER.exception("Failed to parse SIP message")
            return None
        return raw, parsed

    def _dispatch(self, msg: SipMessage, raw: bytes) -> None:
        call = self._call
        if call is None:
            return
        msg_call_id = msg.headers.get("call-id")
        if msg_call_id and msg_call_id != call.call_id:
            _LOGGER.debug(
                "Ignoring SIP message for unknown Call-ID %s", msg_call_id
            )
            return
        if msg.method is None:
            self._handle_response(call, msg, raw)
        elif msg.method.lower() == "bye":
            self._handle_bye(call, msg, raw)
        elif msg.method.lower() == "invite":
            # In-dialog INVITE = RFC 4028 session-timer refresh. We respond
            # 200 OK with the original SDP to keep the media path unchanged.
            self._handle_reinvite(call, msg, raw)
        elif msg.method.lower() == "message":
            # Server-originated in-dialog MESSAGE — just 200 OK it so the
            # server doesn't retransmit. We never act on the body.
            self._handle_server_message(call, msg, raw)
        else:
            _LOGGER.warning(
                "Call %s: unhandled in-dialog %s — ignoring",
                call.call_id,
                msg.method,
            )

    # --- public API -----------------------------------------------------------

    def call(
        self,
        target_uri: str,
        local_rtp_port: int,
        sip_username: str,
        sip_password: str,
        local_video_rtp_port: int | None = None,
    ) -> str:
        """Send INVITE on the already-established TCP connection."""
        if self._transport is None:
            raise RuntimeError("Transport not connected")
        if self._call is not None:
            raise RuntimeError("Client is single-call; a call is already in progress")

        token = secrets.token_hex(8)
        # Numeric-only Call-ID: Intratone rejected hex-suffix IDs with a 500.
        suffix = int(token[:8], 16)
        call = _PendingCall(
            call_id=f"{time.monotonic_ns()}{suffix:010d}@{self._local_host}",
            target_uri=target_uri,
            local_host=self._local_host,
            local_port=self._local_tcp_port(),
            local_rtp_port=local_rtp_port,
            sip_username=sip_username,
            sip_password=sip_password,
            via_branch=f"z9hG4bK-{token}",
            from_tag=token[:8],
            cseq=_INITIAL_CSEQ,
            state=CallState.INVITING,
            local_video_rtp_port=local_video_rtp_port,
        )
        self._call = call
        self._send(self._build_invite(call, auth_header=None))
        return call.call_id

    def send_open_door(self, call_id: str, code: str = "*") -> bool:
        """Send in-dialog SIP MESSAGE `opendoor:<code>` to trigger the door
        relay. This rides the same TCP connection that carried the INVITE,
        matching the Cogelec app's behavior (in-dialog ChatRoom.send())."""
        return self._send_in_dialog_message(
            call_id, body=f"opendoor:{code}", label=f"open-door (code={code})"
        )

    def send_mute_off(self, call_id: str) -> bool:
        """Send the in-dialog SIP MESSAGE body `MUTE_OFF`.

        Cogelec's app emits this exact body when the user picks up manually
        (`DECROCHER_AUTO=false`, the default state — see
        `CallManager.java:752-755` in the decompiled APK). The send happens
        before the mic is enabled and is independent of the audio
        permission, so it is an application-level signal to the server
        ("user engaged, keep the full-duplex channel alive"), not a mic
        state toggle. We forward it to extend the server-side call window:
        worst case the server ignores it (no-op), best case we get more
        seconds before BYE so the user has more time to tap Unlock."""
        return self._send_in_dialog_message(
            call_id, body="MUTE_OFF", label="MUTE_OFF"
        )

    def send_reinvite_audio_only(self, call_id: str) -> bool:
        """Send an in-dialog re-INVITE that drops the `m=video` media line.

        Mirrors the iOS app's behaviour when VP8 keyframes never arrive
        (string `[LinphoneManager][ERROR] Failed to update call to
        audio-only:` in the decrypted binary). Keeps audio alive while
        telling the gateway to stop sending dead video bytes.

        Idempotent: returns False without sending anything if the call is
        already audio-only or not in the right state. A non-2xx response
        does NOT tear the call down — see `_handle_response`.
        """
        call = self._call
        if call is None or call.call_id != call_id:
            _LOGGER.warning("re-INVITE: unknown call %s", call_id)
            return False
        if call.state != CallState.CONFIRMED:
            _LOGGER.warning(
                "re-INVITE: call %s not CONFIRMED (state=%s)",
                call_id, call.state,
            )
            return False
        if call.local_video_rtp_port is None:
            _LOGGER.debug(
                "re-INVITE: call %s already audio-only — skipping",
                call_id,
            )
            return False
        if call.reinvite_in_progress:
            _LOGGER.debug(
                "re-INVITE: call %s already has a re-INVITE pending",
                call_id,
            )
            return False
        # Drop the video offer from subsequent SDP builds. Even if the
        # re-INVITE response is delayed or lost, the call falls back to a
        # consistent audio-only view of itself.
        call.local_video_rtp_port = None
        call.reinvite_in_progress = True
        call.cseq += 1
        call.via_branch = f"z9hG4bK-{secrets.token_hex(8)}"
        self._send(self._build_reinvite(call))
        _LOGGER.info(
            "Call %s: sent audio-only re-INVITE (CSeq=%d)",
            call_id, call.cseq,
        )
        return True

    def send_backlight(self, call_id: str) -> bool:
        """Send the in-dialog SIP MESSAGE body `contrast`.

        Cogelec's app calls this from the (hidden by default) `btnContrast`
        UI element — strings.xml `call_overlay_contrast` describes the
        feature as: "In poor lighting situations, you can enable the
        backlight mode to better see your visitor. This is reset after
        each call." The signal asks the doorbell hardware to turn on its
        front illuminator / switch to a high-gain camera mode for the rest
        of the call. The body is the literal text `contrast` (no value).
        Harmless if the hardware doesn't support it (server simply
        ignores the MESSAGE)."""
        return self._send_in_dialog_message(
            call_id, body="contrast", label="backlight"
        )

    def _send_in_dialog_message(
        self, call_id: str, *, body: str, label: str
    ) -> bool:
        """Common path for in-dialog SIP MESSAGE primitives — validates the
        dialog state and bumps the CSeq before serializing."""
        call = self._call
        if call is None or call.call_id != call_id:
            _LOGGER.warning("%s: unknown call %s", label, call_id)
            return False
        if call.state != CallState.CONFIRMED:
            _LOGGER.warning(
                "%s: call %s not in CONFIRMED state (state=%s)",
                label, call_id, call.state,
            )
            return False
        if not call.remote_to_header:
            _LOGGER.warning(
                "%s: call %s has no dialog state captured", label, call_id
            )
            return False
        call.cseq += 1
        self._send(self._build_message(call, body=body))
        _LOGGER.info(
            "Call %s: sent %s SIP MESSAGE (CSeq=%d)",
            call_id, label, call.cseq,
        )
        return True

    def hang_up(self, call_id: str) -> None:
        """End the call. CANCELs an in-flight INVITE or BYEs a confirmed one,
        then closes the TCP connection."""
        call = self._call
        if call is None or call.call_id != call_id:
            return
        if call.state == CallState.CONFIRMED:
            call.cseq += 1
            self._send(self._build_bye(call))
        elif call.state in (CallState.INVITING, CallState.AUTHENTICATING):
            # RFC 3261 §9: CANCEL the pending INVITE so the server stops ringing.
            self._send(self._build_cancel(call))
        self._terminate(call)

    # --- response routing -----------------------------------------------------

    def _handle_response(
        self, call: _PendingCall, msg: SipMessage, raw: bytes
    ) -> None:
        code = int(msg.code) if msg.code else 0

        if code < 200:
            _LOGGER.debug("Call %s: %s %s", call.call_id, code, msg.reason)
            return

        if code in (401, 407) and call.state == CallState.INVITING:
            self._handle_auth_challenge(call, msg)
            return

        if 200 <= code < 300:
            if call.state in (CallState.INVITING, CallState.AUTHENTICATING):
                self._handle_ok(call, msg, raw)
            elif call.state == CallState.CONFIRMED:
                # Two indistinguishable cases land here:
                #  - Retransmission of an earlier 2xx (our ACK got lost) —
                #    RFC 3261 §13.2.2.4 says re-ACK silently.
                #  - 2xx for an in-dialog re-INVITE we sent — same handling,
                #    just clear the pending flag.
                cseq_method = msg.headers.get("cseq", "").split()
                if len(cseq_method) == 2 and cseq_method[1].upper() == "INVITE":
                    if call.reinvite_in_progress:
                        _LOGGER.info(
                            "Call %s: re-INVITE accepted (200 OK)",
                            call.call_id,
                        )
                        call.reinvite_in_progress = False
                    else:
                        _LOGGER.debug(
                            "Call %s: 2xx retransmission — re-ACK",
                            call.call_id,
                        )
                    request_uri = call.remote_target_uri or call.target_uri
                    self._send(
                        self._build_ack(call, msg, request_uri=request_uri)
                    )
            return

        # Non-2xx final on an in-dialog re-INVITE we sent ourselves: gateway
        # rejected our SDP renegotiation (e.g. 488 Not Acceptable Here, 491
        # Request Pending). We MUST ACK per RFC 3261 §17.1.1.3, but the
        # established dialog is unaffected — keep audio alive instead of
        # tearing down the call entirely. Mirrors the iOS error log
        # `Failed to update call to audio-only:` which doesn't kill the call.
        if call.state == CallState.CONFIRMED and call.reinvite_in_progress:
            _LOGGER.warning(
                "Call %s: re-INVITE rejected %s %s — keeping call alive",
                call.call_id, code, msg.reason,
            )
            self._send(
                self._build_ack(
                    call, msg, request_uri=call.remote_target_uri or call.target_uri
                )
            )
            call.reinvite_in_progress = False
            return

        # Non-2xx final → ACK on the original transaction (request-URI =
        # original INVITE target per RFC 3261 §17.1.1.3), then terminate.
        _LOGGER.warning("Call %s: %s %s — terminating", call.call_id, code, msg.reason)
        self._send(self._build_ack(call, msg, request_uri=call.target_uri))
        self._terminate(call)

    def _handle_auth_challenge(self, call: _PendingCall, msg: SipMessage) -> None:
        # ACK the non-2xx final response on the original transaction
        # (Request-URI = original INVITE target per RFC 3261 §17.1.1.3).
        self._send(self._build_ack(call, msg, request_uri=call.target_uri))

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

        call.cseq += 1
        call.via_branch = f"z9hG4bK-{secrets.token_hex(8)}"
        call.state = CallState.AUTHENTICATING

        header_name = "Proxy-Authorization" if is_proxy else "Authorization"
        self._send(self._build_invite(call, auth_header=(header_name, auth_value)))

    def _handle_ok(
        self, call: _PendingCall, msg: SipMessage, raw: bytes
    ) -> None:
        # Capture dialog state BEFORE sending ACK — the 2xx-ACK Request-URI
        # is the dialog's remote target (Contact from 200 OK) per RFC 3261
        # §13.2.2.4, not the original INVITE target. Same goes for Route:
        # headers from any Record-Route entries.
        call.remote_to_header = msg.headers.get("to")
        contact = msg.headers.get("contact", "")
        call.remote_target_uri = _extract_uri_from_contact(contact) or call.target_uri
        # Record-Route is reversed for the client-side route set (§12.1.2).
        rr = _extract_record_routes(raw)
        call.route_set = list(reversed(rr)) if rr else None

        _LOGGER.info("Call %s: 200 OK — sending ACK", call.call_id)
        self._send(
            self._build_ack(call, msg, request_uri=call.remote_target_uri)
        )

        try:
            rtp_info = get_rtp_info(msg.body)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Call %s: SDP parse failed", call.call_id)
            self._terminate(call)
            return

        video_endpoint = _extract_video_endpoint(msg.body) if call.local_video_rtp_port else None
        if call.local_video_rtp_port and video_endpoint is None:
            _LOGGER.info(
                "Call %s: server rejected video media (no m=video > 0 in 200 OK)",
                call.call_id,
            )

        call.state = CallState.CONFIRMED
        self._on_call_established(
            CallEstablished(
                call_id=call.call_id,
                remote_rtp_ip=rtp_info.rtp_ip,
                remote_rtp_port=rtp_info.rtp_port,
                local_rtp_port=call.local_rtp_port,
                remote_video_rtp_ip=video_endpoint[0] if video_endpoint else None,
                remote_video_rtp_port=video_endpoint[1] if video_endpoint else None,
                local_video_rtp_port=call.local_video_rtp_port if video_endpoint else None,
            )
        )

    def _handle_reinvite(
        self, call: _PendingCall, msg: SipMessage, raw_data: bytes
    ) -> None:
        """In-dialog INVITE = RFC 4028 session-timer refresh. 200 OK with the
        original SDP keeps the media path unchanged."""
        _LOGGER.info("Call %s: session-timer re-INVITE — extending session", call.call_id)
        sdp = self._build_sdp(call).encode("utf-8")
        via_headers = _extract_via_headers(raw_data)
        lines = ["SIP/2.0 200 OK"]
        lines.extend(f"Via: {v}" for v in via_headers)
        lines.extend(
            [
                f"From: {msg.headers.get('from', '')}",
                f"To: {msg.headers.get('to', '')}",
                f"Call-ID: {msg.headers.get('call-id', '')}",
                f"CSeq: {msg.headers.get('cseq', '')}",
                f"Contact: <sip:{call.sip_username}@{call.local_host}:{call.local_port};transport=tcp>;+sip.instance={_SIP_INSTANCE}",
                f"User-Agent: {_USER_AGENT}",
                "Supported: timer",
                f"Session-Expires: {_SESSION_EXPIRES_S};refresher=uas",
                "Content-Type: application/sdp",
                f"Content-Length: {len(sdp)}",
                "",
            ]
        )
        self._send((_CRLF.join(lines) + _CRLF).encode("utf-8") + sdp)

    def _handle_bye(
        self, call: _PendingCall, msg: SipMessage, raw_data: bytes
    ) -> None:
        _LOGGER.info("Call %s: BYE received — terminating", call.call_id)
        self._send(self._build_200_for(msg, raw_data))
        self._terminate(call)

    def _handle_server_message(
        self, call: _PendingCall, msg: SipMessage, raw_data: bytes
    ) -> None:
        _LOGGER.debug("Call %s: server MESSAGE — 200 OK echo", call.call_id)
        self._send(self._build_200_for(msg, raw_data))

    def _terminate(self, call: _PendingCall) -> None:
        if call.state == CallState.TERMINATED:
            return
        call.state = CallState.TERMINATED
        self._call = None
        self._on_call_terminated(call.call_id)
        if self._transport is not None and not self._transport.is_closing():
            self._transport.close()

    # --- message builders -----------------------------------------------------

    def _local_tcp_port(self) -> int:
        if self._transport is None:
            return 0
        sock = self._transport.get_extra_info("sockname")
        return sock[1] if sock else 0

    def _build_invite(
        self,
        call: _PendingCall,
        auth_header: tuple[str, str] | None,
    ) -> bytes:
        sdp = self._build_sdp(call).encode("utf-8")
        head_lines = [
            f"INVITE {call.target_uri} SIP/2.0",
            f"Via: SIP/2.0/TCP {call.local_host}:{call.local_port};branch={call.via_branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{call.sip_username}@{call.local_host}>;tag={call.from_tag}",
            f"To: <{call.target_uri}>",
            f"Call-ID: {call.call_id}",
            f"CSeq: {call.cseq} INVITE",
            f"Contact: <sip:{call.sip_username}@{call.local_host}:{call.local_port};transport=tcp>;+sip.instance={_SIP_INSTANCE}",
            f"User-Agent: {_USER_AGENT}",
            "Allow: INVITE, ACK, CANCEL, BYE, MESSAGE",
            "Supported: timer",
            f"Session-Expires: {_SESSION_EXPIRES_S};refresher=uas",
            "Min-SE: 90",
            "Content-Type: application/sdp",
        ]
        if auth_header:
            head_lines.append(f"{auth_header[0]}: {auth_header[1]}")
        head_lines.append(f"Content-Length: {len(sdp)}")
        head_lines.append("")
        return (_CRLF.join(head_lines) + _CRLF).encode("utf-8") + sdp

    def _build_reinvite(self, call: _PendingCall) -> bytes:
        """In-dialog INVITE (RFC 3261 §14) — target = dialog remote URI, To
        carries the server's tag, route set is honoured.

        Currently the only caller is `send_reinvite_audio_only`, which has
        already dropped `local_video_rtp_port` from the call state so the
        SDP rebuild emits audio-only. Generalising to other re-INVITE
        purposes (hold, codec switch) only needs additional helpers; the
        wire framing here is identical.
        """
        sdp = self._build_sdp(call).encode("utf-8")
        request_uri = call.remote_target_uri or call.target_uri
        lines = [
            f"INVITE {request_uri} SIP/2.0",
            f"Via: SIP/2.0/TCP {call.local_host}:{call.local_port};branch={call.via_branch};rport",
            "Max-Forwards: 70",
        ]
        if call.route_set:
            for r in call.route_set:
                lines.append(f"Route: {r}")
        lines.extend(
            [
                f"From: <sip:{call.sip_username}@{call.local_host}>;tag={call.from_tag}",
                f"To: {call.remote_to_header or f'<{call.target_uri}>'}",
                f"Call-ID: {call.call_id}",
                f"CSeq: {call.cseq} INVITE",
                f"Contact: <sip:{call.sip_username}@{call.local_host}:{call.local_port};transport=tcp>;+sip.instance={_SIP_INSTANCE}",
                f"User-Agent: {_USER_AGENT}",
                "Allow: INVITE, ACK, CANCEL, BYE, MESSAGE",
                "Supported: timer",
                f"Session-Expires: {_SESSION_EXPIRES_S};refresher=uas",
                "Min-SE: 90",
                "Content-Type: application/sdp",
                f"Content-Length: {len(sdp)}",
                "",
            ]
        )
        return (_CRLF.join(lines) + _CRLF).encode("utf-8") + sdp

    def _build_sdp(self, call: _PendingCall) -> str:
        session_id = call.call_id.split("@", 1)[0]
        lines = [
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
        ]
        if call.local_video_rtp_port is not None:
            # Offer VP8 video, mirroring the official Cogelec app's Linphone
            # config (display=true, capture=false, VP8 whitelist; APK
            # CallManager.java:354-452). We send `sendrecv` like the app does
            # even though we never transmit — some Intratone servers reject
            # `recvonly` in offers. PT 96 is the conventional dynamic value.
            lines += [
                f"m=video {call.local_video_rtp_port} RTP/AVP 96",
                "a=rtpmap:96 VP8/90000",
                "a=sendrecv",
            ]
        lines.append("")
        return _CRLF.join(lines)

    def _build_ack(
        self,
        call: _PendingCall,
        response: SipMessage,
        *,
        request_uri: str,
    ) -> bytes:
        """Build an ACK.

        - Non-2xx (401/407/4xx/5xx/6xx): same transaction as INVITE → use the
          original INVITE Request-URI and the INVITE's Via branch.
        - 2xx: new transaction in the established dialog → Request-URI is the
          dialog's remote target (Contact from 200 OK) and the route set is
          honored.
        Caller controls which by passing `request_uri` explicitly.
        """
        lines = [
            f"ACK {request_uri} SIP/2.0",
            f"Via: SIP/2.0/TCP {call.local_host}:{call.local_port};branch={call.via_branch};rport",
            "Max-Forwards: 70",
        ]
        # Route headers (only meaningful for the 2xx-ACK; on non-2xx the
        # route_set isn't established yet — but if it is, including it is
        # harmless because the request stays in-dialog).
        if call.route_set:
            for r in call.route_set:
                lines.append(f"Route: {r}")
        lines.extend(
            [
                f"From: <sip:{call.sip_username}@{call.local_host}>;tag={call.from_tag}",
                f"To: {response.headers.get('to', f'<{call.target_uri}>')}",
                f"Call-ID: {call.call_id}",
                f"CSeq: {call.cseq} ACK",
                f"User-Agent: {_USER_AGENT}",
                "Content-Length: 0",
                "",
            ]
        )
        return (_CRLF.join(lines) + _CRLF).encode("utf-8")

    def _build_message(self, call: _PendingCall, body: str) -> bytes:
        """In-dialog SIP MESSAGE (RFC 3428) carrying a text body. Request-URI
        is the dialog's remote target (Contact URI from the 200 OK); Route
        headers come from the captured Record-Route reverse list."""
        body_bytes = body.encode("utf-8")
        new_branch = f"z9hG4bK-{secrets.token_hex(8)}"
        request_uri = call.remote_target_uri or call.target_uri
        lines = [
            f"MESSAGE {request_uri} SIP/2.0",
            f"Via: SIP/2.0/TCP {call.local_host}:{call.local_port};branch={new_branch};rport",
            "Max-Forwards: 70",
        ]
        if call.route_set:
            for r in call.route_set:
                lines.append(f"Route: {r}")
        lines.extend(
            [
                f"From: <sip:{call.sip_username}@{call.local_host}>;tag={call.from_tag}",
                f"To: {call.remote_to_header}",
                f"Call-ID: {call.call_id}",
                f"CSeq: {call.cseq} MESSAGE",
                f"User-Agent: {_USER_AGENT}",
                "Content-Type: text/plain;charset=UTF-8",
                f"Content-Length: {len(body_bytes)}",
                "",
            ]
        )
        return (_CRLF.join(lines) + _CRLF).encode("utf-8") + body_bytes

    def _build_cancel(self, call: _PendingCall) -> bytes:
        lines = [
            f"CANCEL {call.target_uri} SIP/2.0",
            f"Via: SIP/2.0/TCP {call.local_host}:{call.local_port};branch={call.via_branch};rport",
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
        request_uri = call.remote_target_uri or call.target_uri
        lines = [
            f"BYE {request_uri} SIP/2.0",
            f"Via: SIP/2.0/TCP {call.local_host}:{call.local_port};branch=z9hG4bK-{secrets.token_hex(8)};rport",
            "Max-Forwards: 70",
        ]
        if call.route_set:
            for r in call.route_set:
                lines.append(f"Route: {r}")
        lines.extend(
            [
                f"From: <sip:{call.sip_username}@{call.local_host}>;tag={call.from_tag}",
                f"To: {call.remote_to_header or f'<{call.target_uri}>'}",
                f"Call-ID: {call.call_id}",
                f"CSeq: {call.cseq} BYE",
                f"User-Agent: {_USER_AGENT}",
                "Content-Length: 0",
                "",
            ]
        )
        return (_CRLF.join(lines) + _CRLF).encode("utf-8")

    def _build_200_for(self, request: SipMessage, raw_data: bytes) -> bytes:
        """RFC 3261 §17.2.1: a response MUST echo every Via header from the
        request, in the same order. voip_utils' single-value headers dict
        collapses multi-Via so we re-parse the raw bytes here."""
        via_headers = _extract_via_headers(raw_data)
        lines = ["SIP/2.0 200 OK"]
        lines.extend(f"Via: {v}" for v in via_headers)
        lines.extend(
            [
                f"From: {request.headers.get('from', '')}",
                f"To: {request.headers.get('to', '')}",
                f"Call-ID: {request.headers.get('call-id', '')}",
                f"CSeq: {request.headers.get('cseq', '')}",
                f"User-Agent: {_USER_AGENT}",
                "Content-Length: 0",
                "",
            ]
        )
        return (_CRLF.join(lines) + _CRLF).encode("utf-8")

    def _send(self, data: bytes) -> None:
        assert self._transport is not None
        _LOGGER.debug("SIP TX:\n%s", _redact_sip(data))
        self._transport.write(data)


def _extract_uri_from_contact(contact: str) -> str | None:
    """Pull the URI out of a Contact header value like `<sip:foo@bar;param>` or
    `"Display" <sip:foo@bar>`. Returns None if no `<sip:...>` is found."""
    if not contact:
        return None
    start = contact.find("<")
    end = contact.find(">", start + 1) if start != -1 else -1
    if start == -1 or end == -1:
        # No angle brackets — the value itself is the URI (possibly with params).
        return contact.split(";", 1)[0].strip() or None
    return contact[start + 1 : end]
