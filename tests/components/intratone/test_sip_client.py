"""Tests for IntratoneSipClient (TCP-only UAC) — full call flow with a mocked
asyncio Transport.

Verifies:
- INVITE bytes (PCMU SDP, correct CSeq, Call-ID, Via branch, TCP transport)
- 407 retry: ACK sent on same CSeq, then INVITE on CSeq+1 with Proxy-Authorization
- 200 OK: ACK sent, on_call_established called with parsed RTP info
- Server BYE: 200 OK sent, on_call_terminated called
- In-dialog MESSAGE for `opendoor:*`
- hang_up: BYE sent on CONFIRMED call

The transport is mocked because asyncio TCP would require a real listener;
all SIP framing/parsing is exercised via `client.data_received(bytes)`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest

from custom_components.intratone.sip_client import (
    CallEstablished,
    CallState,
    IntratoneSipClient,
)

LOCAL_HOST = "192.168.1.50"
LOCAL_PORT = 54321  # ephemeral port the OS chose for our TCP socket
LOCAL_RTP = 10000
TARGET_URI = "sip:2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36@178.32.84.135"
USERNAME = "cogelecTest"
PASSWORD = "CogeleC"


class FakeTcpTransport:
    """Records every write() so tests can assert on outgoing SIP bytes."""

    def __init__(self, local_port: int = LOCAL_PORT) -> None:
        self.sent: list[bytes] = []
        self._closed = False
        self._local_port = local_port

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self._closed = True

    def is_closing(self) -> bool:
        return self._closed

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "sockname":
            return ("0.0.0.0", self._local_port)
        return default


@pytest.fixture
def client_setup() -> tuple[IntratoneSipClient, FakeTcpTransport, list, list]:
    transport = FakeTcpTransport()
    established: list[CallEstablished] = []
    terminated: list[str] = []
    client = IntratoneSipClient(
        local_host=LOCAL_HOST,
        on_call_established=established.append,
        on_call_terminated=terminated.append,
    )
    client.connection_made(transport)  # type: ignore[arg-type]
    return client, transport, established, terminated


def _parse(message: bytes) -> tuple[str, dict[str, str], bytes]:
    """Tiny SIP parser for assertions: returns (start_line, headers_lower, body)."""
    head, _, body = message.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return lines[0], headers, body


def _expected_digest_response(nonce: str, realm: str = "asterisk") -> str:
    ha1 = hashlib.md5(f"{USERNAME}:{realm}:{PASSWORD}".encode()).hexdigest()
    ha2 = hashlib.md5(f"INVITE:{TARGET_URI}".encode()).hexdigest()
    return hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()


# --- INVITE ----------------------------------------------------------------


def test_call_sends_invite_with_pcmu_sdp(client_setup):
    client, transport, _, _ = client_setup
    client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)

    assert len(transport.sent) == 1
    data = transport.sent[0]

    start, headers, body = _parse(data)
    assert start == f"INVITE {TARGET_URI} SIP/2.0"
    assert headers["cseq"] == "50 INVITE"
    assert headers["content-type"] == "application/sdp"
    assert "proxy-authorization" not in headers
    assert "authorization" not in headers
    # Via declares TCP transport — INVITE rides the same TCP socket as
    # every subsequent in-dialog request (matches the Cogelec app).
    assert "SIP/2.0/TCP" in headers["via"]
    assert f"{LOCAL_HOST}:{LOCAL_PORT}" in headers["via"]
    # Contact also advertises TCP so the server reuses our connection.
    assert "transport=tcp" in headers["contact"]
    # RFC 4028 session-timer headers — server BYEs short calls otherwise.
    assert "timer" in headers["supported"]
    assert "1800" in headers["session-expires"]
    assert "refresher=uas" in headers["session-expires"]

    sdp = body.decode()
    assert "m=audio 10000 RTP/AVP 0 8 101" in sdp
    assert "a=rtpmap:0 PCMU/8000" in sdp
    assert "a=rtpmap:8 PCMA/8000" in sdp
    assert "opus" not in sdp.lower()
    assert f"c=IN IP4 {LOCAL_HOST}" in sdp


def test_call_returns_call_id_matching_invite(client_setup):
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)

    _, headers, _ = _parse(transport.sent[0])
    assert headers["call-id"] == call_id


def test_call_raises_when_already_in_progress(client_setup):
    client, _, _, _ = client_setup
    client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    with pytest.raises(RuntimeError, match="single-call"):
        client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)


# --- 407 retry --------------------------------------------------------------


def _build_407(call_id: str, branch: str, from_tag: str) -> bytes:
    body = (
        "SIP/2.0 407 Proxy Authentication Required\r\n"
        f"Via: SIP/2.0/TCP {LOCAL_HOST}:{LOCAL_PORT};branch={branch};rport\r\n"
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"To: <{TARGET_URI}>;tag=server-tag-xyz\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 50 INVITE\r\n"
        'Proxy-Authenticate: Digest realm="asterisk", nonce="N0NCE-001"\r\n'
        "Content-Length: 0\r\n"
        "\r\n"
    )
    return body.encode()


def _branch_and_tag_from_invite(invite: bytes) -> tuple[str, str]:
    _, headers, _ = _parse(invite)
    branch = re.search(r"branch=([^;]+)", headers["via"]).group(1)
    from_tag = re.search(r"tag=(\S+)", headers["from"]).group(1)
    return branch, from_tag


def test_407_triggers_ack_then_authenticated_reinvite(client_setup):
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    initial_invite = transport.sent[0]
    branch, from_tag = _branch_and_tag_from_invite(initial_invite)

    client.data_received(_build_407(call_id, branch, from_tag))

    # 3 messages total: INVITE 50, ACK 50, INVITE 51
    assert len(transport.sent) == 3
    ack_start, ack_headers, _ = _parse(transport.sent[1])
    reinvite_start, reinvite_headers, _ = _parse(transport.sent[2])

    assert ack_start == f"ACK {TARGET_URI} SIP/2.0"
    assert ack_headers["cseq"] == "50 ACK"
    assert ack_headers["call-id"] == call_id
    assert "tag=server-tag-xyz" in ack_headers["to"]

    assert reinvite_start == f"INVITE {TARGET_URI} SIP/2.0"
    assert reinvite_headers["cseq"] == "51 INVITE"
    assert reinvite_headers["call-id"] == call_id
    # New transaction → new Via branch.
    new_branch = re.search(r"branch=([^;]+)", reinvite_headers["via"]).group(1)
    assert new_branch != branch

    auth = reinvite_headers["proxy-authorization"]
    assert auth.startswith("Digest ")
    assert f'response="{_expected_digest_response("N0NCE-001")}"' in auth
    assert f'username="{USERNAME}"' in auth
    assert 'realm="asterisk"' in auth

    assert client._call.state == CallState.AUTHENTICATING


# --- 200 OK -----------------------------------------------------------------


def _build_200_ok(
    call_id: str,
    from_tag: str,
    cseq: int,
    contact: str = "<sip:server@178.32.84.99:5060;transport=tcp>",
) -> bytes:
    """200 OK with TCP Contact by default (matches Cogelec's blocip server)."""
    sdp = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 178.32.84.99\r\n"
        "s=-\r\n"
        "c=IN IP4 178.32.84.99\r\n"
        "t=0 0\r\n"
        "m=audio 20002 RTP/AVP 0 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
    )
    head = (
        "SIP/2.0 200 OK\r\n"
        f"Via: SIP/2.0/TCP {LOCAL_HOST}:{LOCAL_PORT};branch=ignored;rport\r\n"
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"To: <{TARGET_URI}>;tag=srv-confirmed\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} INVITE\r\n"
        f"Contact: {contact}\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n"
        f"\r\n{sdp}"
    )
    return head.encode()


def test_200_ok_acks_and_notifies_with_rtp_endpoint(client_setup):
    client, transport, established, _ = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    _, from_tag = _branch_and_tag_from_invite(transport.sent[0])
    branch_initial = _branch_and_tag_from_invite(transport.sent[0])[0]
    client.data_received(_build_407(call_id, branch_initial, from_tag))
    client.data_received(_build_200_ok(call_id, from_tag, 51))

    # 4 messages: INVITE 50, ACK 50, INVITE 51, ACK 51.
    assert len(transport.sent) == 4
    ack_start, ack_headers, _ = _parse(transport.sent[3])
    # 2xx-ACK Request-URI is the dialog's remote target (Contact from 200 OK),
    # NOT the original INVITE target — RFC 3261 §13.2.2.4.
    assert ack_start == "ACK sip:server@178.32.84.99:5060;transport=tcp SIP/2.0"
    assert ack_headers["cseq"] == "51 ACK"
    assert "tag=srv-confirmed" in ack_headers["to"]

    assert len(established) == 1
    assert established[0] == CallEstablished(
        call_id=call_id,
        remote_rtp_ip="178.32.84.99",
        remote_rtp_port=20002,
        local_rtp_port=LOCAL_RTP,
    )
    assert client._call.state == CallState.CONFIRMED


# --- TCP framing -----------------------------------------------------------


def test_data_received_handles_split_message(client_setup):
    """SIP/TCP is a stream — a single SIP message may arrive across multiple
    `data_received` calls. Buffering and Content-Length-based framing must
    reassemble it before parsing."""
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    _, from_tag = _branch_and_tag_from_invite(transport.sent[0])
    branch_initial = _branch_and_tag_from_invite(transport.sent[0])[0]
    response = _build_407(call_id, branch_initial, from_tag)

    # Feed it in two halves split mid-header.
    split = len(response) // 2
    client.data_received(response[:split])
    # Nothing dispatched yet — buffer incomplete.
    assert len(transport.sent) == 1
    client.data_received(response[split:])
    # Now the 407 was processed: ACK + re-INVITE went out.
    assert len(transport.sent) == 3


def test_data_received_handles_concatenated_messages(client_setup):
    """Two SIP messages arriving in one TCP read must both be parsed."""
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    _, from_tag = _branch_and_tag_from_invite(transport.sent[0])
    branch_initial = _branch_and_tag_from_invite(transport.sent[0])[0]
    # Server sends 407 immediately followed by something that's NOT for us —
    # use a 100 Trying that's safely ignored.
    msg407 = _build_407(call_id, branch_initial, from_tag)
    trying = (
        "SIP/2.0 100 Trying\r\n"
        f"Via: SIP/2.0/TCP {LOCAL_HOST}:{LOCAL_PORT};branch={branch_initial}\r\n"
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"To: <{TARGET_URI}>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 50 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    client.data_received(msg407 + trying)
    # The 407 triggered ACK + re-INVITE; the 100 Trying is a no-op.
    assert len(transport.sent) == 3


# --- BYE & hang_up ----------------------------------------------------------


def _confirm_call(
    client: IntratoneSipClient, transport: FakeTcpTransport
) -> tuple[str, str]:
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    branch, from_tag = _branch_and_tag_from_invite(transport.sent[0])
    client.data_received(_build_407(call_id, branch, from_tag))
    client.data_received(_build_200_ok(call_id, from_tag, 51))
    return call_id, from_tag


def test_incoming_bye_replies_200_and_terminates(client_setup):
    """BYE with multiple Via headers (typical Intratone proxy setup) must be
    answered with ALL Vias echoed back per RFC 3261 §17.2.1 — otherwise the
    upstream proxy can't route the response and the BYE gets retransmitted
    forever, leaving the call wedged."""
    client, transport, _, terminated = client_setup
    call_id, from_tag = _confirm_call(client, transport)
    sent_before = len(transport.sent)

    bye = (
        f"BYE <sip:{USERNAME}@{LOCAL_HOST}> SIP/2.0\r\n"
        f"Via: SIP/2.0/TCP 178.32.84.135;branch=z9hG4bK-proxy-9f01;i=569786\r\n"
        f"Via: SIP/2.0/TCP 10.197.105.144:57398;received=51.77.24.90;branch=z9hG4bK.xYbCuVD;rport=29843\r\n"
        f"From: <{TARGET_URI}>;tag=srv-confirmed\r\n"
        f"To: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 BYE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    client.data_received(bye)

    assert len(transport.sent) == sent_before + 1
    data = transport.sent[-1]
    start, headers, _ = _parse(data)
    assert start == "SIP/2.0 200 OK"
    assert headers["cseq"] == "1 BYE"
    assert headers["call-id"] == call_id
    via_count = data.count(b"\r\nVia:")
    assert via_count == 2, f"expected 2 Via headers in 200 OK, got {via_count}"
    assert b"proxy-9f01" in data
    assert b"xYbCuVD" in data
    assert terminated == [call_id]
    assert client._call is None
    # The TCP transport was closed on terminate.
    assert transport.is_closing()


def test_hang_up_sends_bye_with_bumped_cseq(client_setup):
    client, transport, _, terminated = client_setup
    call_id, _ = _confirm_call(client, transport)
    sent_before = len(transport.sent)

    client.hang_up(call_id)

    assert len(transport.sent) == sent_before + 1
    start, headers, _ = _parse(transport.sent[-1])
    # BYE goes to the dialog's remote target (Contact URI from 200 OK).
    assert start == "BYE sip:server@178.32.84.99:5060;transport=tcp SIP/2.0"
    # After 200 OK on CSeq 51, BYE bumps to 52.
    assert headers["cseq"] == "52 BYE"
    assert headers["call-id"] == call_id
    assert terminated == [call_id]


def test_send_open_door_emits_in_dialog_message(client_setup):
    """After 200 OK the dialog state (server To-tag, Contact URI) is captured
    and `send_open_door` builds a SIP MESSAGE in the same TCP dialog with the
    Contact URI as Request-URI (RFC 3261 §12.2.1.1)."""
    client, transport, _, _ = client_setup
    call_id, _ = _confirm_call(client, transport)
    sent_before = len(transport.sent)

    assert client.send_open_door(call_id, code="*") is True

    assert len(transport.sent) == sent_before + 1
    data = transport.sent[-1]
    start, headers, body = _parse(data)
    # Request-URI is the Contact URI from the 200 OK — NOT the LOGIN_TO_CALL
    # target we INVITE'd.
    assert start == "MESSAGE sip:server@178.32.84.99:5060;transport=tcp SIP/2.0"
    # Via declares TCP — same connection as the INVITE.
    assert "SIP/2.0/TCP" in headers["via"]
    # CSeq bumped past the ACK CSeq for the 200 OK (51 + 1 = 52).
    assert headers["cseq"] == "52 MESSAGE"
    assert headers["content-type"].startswith("text/plain")
    # To header echoes the server's tag from the 200 OK — required for in-dialog.
    assert "tag=srv-confirmed" in headers["to"]
    # Body is the actual door-open payload the server parses.
    assert body == b"opendoor:*"


def test_send_open_door_with_custom_code(client_setup):
    client, transport, _, _ = client_setup
    call_id, _ = _confirm_call(client, transport)

    assert client.send_open_door(call_id, code="5") is True

    _, _, body = _parse(transport.sent[-1])
    assert body == b"opendoor:5"


def test_send_open_door_fails_when_call_not_confirmed(client_setup):
    """Cannot send in-dialog MESSAGE if the dialog hasn't been established."""
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    sent_before = len(transport.sent)

    assert client.send_open_door(call_id) is False
    assert len(transport.sent) == sent_before  # nothing sent


def test_send_open_door_unknown_call_id_returns_false(client_setup):
    client, _, _, _ = client_setup
    assert client.send_open_door("nobody-knows-me") is False


def test_send_mute_off_emits_in_dialog_message(client_setup):
    """`send_mute_off` builds an in-dialog SIP MESSAGE with body `MUTE_OFF`
    on the same TCP connection as the INVITE — mirrors Cogelec's behaviour
    on manual pickup (`DECROCHER_AUTO=false`)."""
    client, transport, _, _ = client_setup
    call_id, _ = _confirm_call(client, transport)
    sent_before = len(transport.sent)

    assert client.send_mute_off(call_id) is True

    assert len(transport.sent) == sent_before + 1
    start, headers, body = _parse(transport.sent[-1])
    assert start.startswith("MESSAGE ")
    assert headers["cseq"] == "52 MESSAGE"
    assert headers["content-type"].startswith("text/plain")
    assert body == b"MUTE_OFF"


def test_send_mute_off_fails_when_call_not_confirmed(client_setup):
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    sent_before = len(transport.sent)

    assert client.send_mute_off(call_id) is False
    assert len(transport.sent) == sent_before  # nothing sent


def test_send_mute_off_unknown_call_id_returns_false(client_setup):
    client, _, _, _ = client_setup
    assert client.send_mute_off("nobody-knows-me") is False


def test_send_backlight_emits_in_dialog_message(client_setup):
    """`send_backlight` ships an in-dialog SIP MESSAGE with body `contrast`
    on the same TCP dialog — mirrors Cogelec's hidden btnContrast pattern
    that activates the doorbell hardware's front illuminator."""
    client, transport, _, _ = client_setup
    call_id, _ = _confirm_call(client, transport)
    sent_before = len(transport.sent)

    assert client.send_backlight(call_id) is True

    assert len(transport.sent) == sent_before + 1
    start, headers, body = _parse(transport.sent[-1])
    assert start.startswith("MESSAGE ")
    assert headers["cseq"] == "52 MESSAGE"
    assert headers["content-type"].startswith("text/plain")
    assert body == b"contrast"


def test_send_backlight_fails_when_call_not_confirmed(client_setup):
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    sent_before = len(transport.sent)

    assert client.send_backlight(call_id) is False
    assert len(transport.sent) == sent_before


def test_send_reinvite_audio_only_drops_video_and_keeps_dialog(client_setup):
    """When PLI exhausts without a keyframe, CallManager asks the SIP client
    for an in-dialog re-INVITE that renegotiates audio-only. Mirrors iOS
    `Failed to update call to audio-only:` log point — gateway stops
    sending dead VP8, audio remains intact."""
    client, transport, _, terminated = client_setup
    # Boot a video-enabled call: explicit local_video_rtp_port + confirm.
    call_id = client.call(
        TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD, local_video_rtp_port=20000
    )
    branch, from_tag = _branch_and_tag_from_invite(transport.sent[0])
    client.data_received(_build_407(call_id, branch, from_tag))
    client.data_received(_build_200_ok(call_id, from_tag, 51))
    sent_before = len(transport.sent)

    assert client.send_reinvite_audio_only(call_id) is True

    assert len(transport.sent) == sent_before + 1
    start, headers, body = _parse(transport.sent[-1])
    assert start.startswith("INVITE ")
    # CSeq bumped on the existing dialog (51 → 52). Method is INVITE.
    assert headers["cseq"] == "52 INVITE"
    assert headers["call-id"] == call_id
    # The defining property: no m=video in the new offer.
    assert b"m=audio" in body
    assert b"m=video" not in body
    # Dialog still alive — terminate would have called the callback.
    assert terminated == []
    assert client._call is not None
    assert client._call.state == CallState.CONFIRMED
    assert client._call.reinvite_in_progress is True


def test_send_reinvite_audio_only_clears_flag_on_200_ok(client_setup):
    """200 OK for the re-INVITE clears `reinvite_in_progress` and re-ACKs."""
    client, transport, _, _ = client_setup
    call_id = client.call(
        TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD, local_video_rtp_port=20000
    )
    branch, from_tag = _branch_and_tag_from_invite(transport.sent[0])
    client.data_received(_build_407(call_id, branch, from_tag))
    client.data_received(_build_200_ok(call_id, from_tag, 51))
    assert client.send_reinvite_audio_only(call_id) is True
    # Server accepts: 200 OK on the same CSeq we sent (52). ACK fires
    # silently and the flag clears.
    client.data_received(_build_200_ok(call_id, from_tag, 52))

    assert client._call is not None
    assert client._call.reinvite_in_progress is False
    # Last sent message is the 2xx-ACK with the new CSeq.
    start, headers, _ = _parse(transport.sent[-1])
    assert start.startswith("ACK ")
    assert headers["cseq"] == "52 ACK"


def test_send_reinvite_audio_only_survives_4xx_rejection(client_setup):
    """If the gateway rejects the re-INVITE (488 Not Acceptable, 491 Request
    Pending…), the established audio dialog must NOT be torn down. Only the
    pending flag is cleared so a later attempt can retry."""
    client, transport, _, terminated = client_setup
    call_id = client.call(
        TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD, local_video_rtp_port=20000
    )
    branch, from_tag = _branch_and_tag_from_invite(transport.sent[0])
    client.data_received(_build_407(call_id, branch, from_tag))
    client.data_received(_build_200_ok(call_id, from_tag, 51))
    assert client.send_reinvite_audio_only(call_id) is True

    reject = (
        "SIP/2.0 488 Not Acceptable Here\r\n"
        f"Via: SIP/2.0/TCP {LOCAL_HOST}:{LOCAL_PORT};branch=ignored;rport\r\n"
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"To: <{TARGET_URI}>;tag=srv-confirmed\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 52 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    client.data_received(reject)

    assert terminated == []
    assert client._call is not None
    assert client._call.state == CallState.CONFIRMED
    assert client._call.reinvite_in_progress is False
    # ACK was sent for the rejection — same Call-ID.
    start, headers, _ = _parse(transport.sent[-1])
    assert start.startswith("ACK ")
    assert headers["call-id"] == call_id


def test_send_reinvite_audio_only_is_noop_when_no_video(client_setup):
    """If the call was already audio-only (no `local_video_rtp_port` in the
    offer), there's nothing to renegotiate — short-circuit."""
    client, transport, _, _ = client_setup
    _confirm_call(client, transport)  # audio-only call
    sent_before = len(transport.sent)

    assert client.send_reinvite_audio_only(client._call.call_id) is False
    assert len(transport.sent) == sent_before


def test_send_reinvite_audio_only_fails_when_not_confirmed(client_setup):
    client, transport, _, _ = client_setup
    call_id = client.call(
        TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD, local_video_rtp_port=20000
    )
    sent_before = len(transport.sent)

    assert client.send_reinvite_audio_only(call_id) is False
    assert len(transport.sent) == sent_before


def test_session_timer_reinvite_is_answered_with_200_ok(client_setup):
    """The server refreshes the session by sending an in-dialog INVITE. We
    must respond 200 OK with our SDP so the call doesn't get BYE'd at refresh."""
    client, transport, _, _ = client_setup
    call_id, from_tag = _confirm_call(client, transport)
    sent_before = len(transport.sent)

    reinvite = (
        f"INVITE <sip:{USERNAME}@{LOCAL_HOST}> SIP/2.0\r\n"
        f"Via: SIP/2.0/TCP 178.32.84.135;branch=z9hG4bK-refresh\r\n"
        f"From: <{TARGET_URI}>;tag=srv-confirmed\r\n"
        f"To: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        "Supported: timer\r\n"
        "Session-Expires: 1800;refresher=uas\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    client.data_received(reinvite)

    assert len(transport.sent) == sent_before + 1
    start, headers, body = _parse(transport.sent[-1])
    assert start == "SIP/2.0 200 OK"
    assert headers["cseq"] == "1 INVITE"
    assert "1800" in headers["session-expires"]
    assert b"m=audio" in body  # SDP echoed
    # Call still active.
    assert client._call is not None
    assert client._call.state == CallState.CONFIRMED


def test_hang_up_on_pending_invite_sends_cancel_and_terminates(client_setup):
    """If hang_up runs while we're still waiting for 200 OK, we CANCEL the
    pending INVITE (RFC 3261 §9) and tear down."""
    client, transport, _, terminated = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    sent_before = len(transport.sent)

    client.hang_up(call_id)

    assert len(transport.sent) == sent_before + 1
    start, _, _ = _parse(transport.sent[-1])
    assert start == f"CANCEL {TARGET_URI} SIP/2.0"
    assert terminated == [call_id]


def test_hang_up_on_authenticating_call_sends_cancel(client_setup):
    client, transport, _, terminated = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    branch, from_tag = _branch_and_tag_from_invite(transport.sent[0])
    client.data_received(_build_407(call_id, branch, from_tag))
    # State is AUTHENTICATING here.
    sent_before = len(transport.sent)

    client.hang_up(call_id)

    assert len(transport.sent) == sent_before + 1
    start, _, _ = _parse(transport.sent[-1])
    assert start == f"CANCEL {TARGET_URI} SIP/2.0"
    assert terminated == [call_id]


def test_4xx_other_than_auth_terminates_call(client_setup):
    """A 486 Busy or 503 Service Unavailable terminates immediately (no retry)."""
    client, transport, _, terminated = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    branch, from_tag = _branch_and_tag_from_invite(transport.sent[0])

    busy = (
        "SIP/2.0 486 Busy Here\r\n"
        f"Via: SIP/2.0/TCP {LOCAL_HOST}:{LOCAL_PORT};branch={branch};rport\r\n"
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"To: <{TARGET_URI}>;tag=busy\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 50 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    client.data_received(busy)

    # Last sent was ACK for the 486.
    start, _, _ = _parse(transport.sent[-1])
    assert start == f"ACK {TARGET_URI} SIP/2.0"
    assert terminated == [call_id]
    assert client._call is None


def test_invalid_digest_challenge_terminates(client_setup):
    """If the 407 has a malformed Proxy-Authenticate, we terminate instead of
    retrying with a bogus header."""
    client, transport, _, terminated = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    branch, from_tag = _branch_and_tag_from_invite(transport.sent[0])

    bad_407 = (
        "SIP/2.0 407 Proxy Authentication Required\r\n"
        f"Via: SIP/2.0/TCP {LOCAL_HOST}:{LOCAL_PORT};branch={branch};rport\r\n"
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"To: <{TARGET_URI}>;tag=server-tag\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 50 INVITE\r\n"
        "Proxy-Authenticate: NotADigestScheme this is junk\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    client.data_received(bad_407)

    assert terminated == [call_id]


def test_retransmitted_200_ok_re_acks_without_terminating(client_setup):
    """If the server retransmits a 200 OK (our ACK got lost), we re-ACK and
    do NOT tear down the call. Per RFC 3261 §13.2.2.4."""
    client, transport, established, terminated = client_setup
    call_id, from_tag = _confirm_call(client, transport)
    assert len(established) == 1
    sent_before = len(transport.sent)

    client.data_received(_build_200_ok(call_id, from_tag, 51))

    # An ACK went out; no new call_established fired; not terminated.
    assert len(transport.sent) == sent_before + 1
    start, _, _ = _parse(transport.sent[-1])
    # 2xx-ACK Request-URI = dialog remote target (Contact), not INVITE target.
    assert start == "ACK sip:server@178.32.84.99:5060;transport=tcp SIP/2.0"
    assert len(established) == 1
    assert terminated == []


def test_stray_response_for_unknown_call_ignored(client_setup):
    """A SIP message for a Call-ID we don't know about must not crash."""
    client, transport, _, _ = client_setup
    sent_before = len(transport.sent)
    junk = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TCP 1.2.3.4;branch=z9hG4bK-stray\r\n"
        "From: <sip:nobody@nowhere>;tag=x\r\n"
        "To: <sip:nobody@nowhere>;tag=y\r\n"
        "Call-ID: not-our-call@somewhere\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    client.data_received(junk)
    assert len(transport.sent) == sent_before


def test_malformed_data_does_not_crash(client_setup):
    """Garbage on the wire must not raise."""
    client, transport, _, _ = client_setup
    sent_before = len(transport.sent)
    # No CRLFCRLF — buffered indefinitely until valid data arrives.
    client.data_received(b"not-a-sip-message-at-all")
    assert len(transport.sent) == sent_before


def _build_200_ok_with_record_routes(
    call_id: str, from_tag: str, cseq: int, record_routes: list[str]
) -> bytes:
    """200 OK carrying one or more Record-Route headers, plus a TCP Contact."""
    sdp = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 178.32.84.99\r\n"
        "s=-\r\n"
        "c=IN IP4 178.32.84.99\r\n"
        "t=0 0\r\n"
        "m=audio 20002 RTP/AVP 0 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
    )
    head_lines = [
        "SIP/2.0 200 OK",
        f"Via: SIP/2.0/TCP {LOCAL_HOST}:{LOCAL_PORT};branch=ignored;rport",
        *(f"Record-Route: {rr}" for rr in record_routes),
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}",
        f"To: <{TARGET_URI}>;tag=srv-confirmed",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} INVITE",
        "Contact: <sip:server@178.32.84.99:5060;transport=tcp>",
        "Content-Type: application/sdp",
        f"Content-Length: {len(sdp)}",
        "",
        sdp,
    ]
    return "\r\n".join(head_lines).encode()


def test_record_route_reversed_into_route_set_for_in_dialog(client_setup):
    """RFC 3261 §12.1.2 / §16.6.4: Record-Route entries in the 200 OK must
    be reversed and emitted as Route: headers on every in-dialog request
    (ACK, MESSAGE, BYE). Without this, B2BUAs like blocip cannot route the
    in-dialog MESSAGE to the right downstream UAS and the door never opens."""
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    _, from_tag = _branch_and_tag_from_invite(transport.sent[0])
    branch_initial = _branch_and_tag_from_invite(transport.sent[0])[0]
    client.data_received(_build_407(call_id, branch_initial, from_tag))

    # Server adds two record-routes (typical B2BUA proxy chain).
    rrs = [
        "<sip:proxy-outer@178.32.84.135;lr;transport=tcp>",
        "<sip:proxy-inner@10.0.0.1;lr;transport=tcp>",
    ]
    client.data_received(_build_200_ok_with_record_routes(call_id, from_tag, 51, rrs))

    # ACK on 2xx must carry the route set in REVERSE order (§12.1.2).
    ack = transport.sent[3]
    ack_text = ack.decode()
    pos_outer = ack_text.find("proxy-outer")
    pos_inner = ack_text.find("proxy-inner")
    assert pos_outer != -1 and pos_inner != -1
    # Reversed: inner appears BEFORE outer in the ACK Route headers.
    assert pos_inner < pos_outer

    # Now in-dialog MESSAGE must also carry the route set.
    assert client.send_open_door(call_id) is True
    msg = transport.sent[-1]
    msg_text = msg.decode()
    assert "Route: <sip:proxy-inner@10.0.0.1;lr;transport=tcp>" in msg_text
    assert "Route: <sip:proxy-outer@178.32.84.135;lr;transport=tcp>" in msg_text
    assert msg_text.find("proxy-inner") < msg_text.find("proxy-outer")


def test_record_route_absent_means_no_route_set(client_setup):
    """No Record-Route in 200 OK → no Route headers on in-dialog requests."""
    client, transport, _, _ = client_setup
    call_id, _ = _confirm_call(client, transport)
    assert client.send_open_door(call_id) is True
    msg = transport.sent[-1]
    assert b"\r\nRoute:" not in msg


def test_invite_contact_includes_sip_instance(client_setup):
    """Belle-sip clients without a REGISTER binding still emit a stable
    `+sip.instance=\"<urn:uuid:...>\"` per RFC 5626 §4.1. The Cogelec server
    has been observed to inspect Contact params; mimicking belle-sip's
    default keeps us in the well-trodden code path."""
    client, transport, _, _ = client_setup
    client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    _, headers, _ = _parse(transport.sent[0])
    assert "+sip.instance=" in headers["contact"]
    assert "urn:uuid:" in headers["contact"]


def test_connection_lost_terminates_active_call(client_setup):
    """If the TCP connection drops mid-call, we fire on_call_terminated so
    CallManager can clean up — otherwise the next ring is silently dropped."""
    client, transport, _, terminated = client_setup
    call_id = client.call(TARGET_URI, LOCAL_RTP, USERNAME, PASSWORD)
    client.connection_lost(ConnectionResetError("peer closed"))
    assert terminated == [call_id]
