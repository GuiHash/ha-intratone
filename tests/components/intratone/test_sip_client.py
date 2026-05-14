"""Tests for IntratoneSipClient — full call flow with mocked transport.

Verifies:
- INVITE bytes (PCMU SDP, correct CSeq, Call-ID, Via branch)
- 407 retry: ACK sent on same CSeq, then INVITE on CSeq+1 with Proxy-Authorization
- 200 OK: ACK sent, on_call_established called with parsed RTP info
- Server BYE: 200 OK sent, on_call_terminated called
- hang_up: BYE sent on CONFIRMED call
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

SERVER = ("178.32.84.135", 5060)
LOCAL_HOST = "192.168.1.50"
LOCAL_PORT = 5070
LOCAL_RTP = 10000
TARGET_URI = "sip:2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36@178.32.84.135"
USERNAME = "cogelecTest"
PASSWORD = "CogeleC"


class FakeTransport:
    """Records every sendto() so tests can assert on outgoing bytes."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))

    # Unused in tests but required by the DatagramTransport surface.
    def close(self) -> None: ...
    def is_closing(self) -> bool: return False
    def get_extra_info(self, name: str, default: Any = None) -> Any: return default


@pytest.fixture
def client_setup() -> tuple[IntratoneSipClient, FakeTransport, list, list]:
    transport = FakeTransport()
    established: list[CallEstablished] = []
    terminated: list[str] = []
    client = IntratoneSipClient(
        local_host=LOCAL_HOST,
        local_port=LOCAL_PORT,
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
    client.call(TARGET_URI, SERVER[0], SERVER[1], LOCAL_RTP, USERNAME, PASSWORD)

    assert len(transport.sent) == 1
    data, addr = transport.sent[0]
    assert addr == SERVER

    start, headers, body = _parse(data)
    assert start == f"INVITE {TARGET_URI} SIP/2.0"
    assert headers["cseq"] == "50 INVITE"
    assert headers["content-type"] == "application/sdp"
    assert "proxy-authorization" not in headers
    assert "authorization" not in headers

    sdp = body.decode()
    assert "m=audio 10000 RTP/AVP 0 8 101" in sdp
    assert "a=rtpmap:0 PCMU/8000" in sdp
    assert "a=rtpmap:8 PCMA/8000" in sdp
    assert "opus" not in sdp.lower()
    assert f"c=IN IP4 {LOCAL_HOST}" in sdp


def test_call_returns_call_id_matching_invite(client_setup):
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, *SERVER, LOCAL_RTP, USERNAME, PASSWORD)

    _, headers, _ = _parse(transport.sent[0][0])
    assert headers["call-id"] == call_id


# --- 407 retry --------------------------------------------------------------


def _build_407(call_id: str, branch: str, from_tag: str) -> bytes:
    return (
        "SIP/2.0 407 Proxy Authentication Required\r\n"
        f"Via: SIP/2.0/UDP {LOCAL_HOST}:{LOCAL_PORT};branch={branch};rport\r\n"
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"To: <{TARGET_URI}>;tag=server-tag-xyz\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 50 INVITE\r\n"
        'Proxy-Authenticate: Digest realm="asterisk", nonce="N0NCE-001"\r\n'
        "Content-Length: 0\r\n"
        "\r\n"
    ).encode()


def _branch_and_tag_from_invite(invite: bytes) -> tuple[str, str]:
    _, headers, _ = _parse(invite)
    branch = re.search(r"branch=([^;]+)", headers["via"]).group(1)
    from_tag = re.search(r"tag=(\S+)", headers["from"]).group(1)
    return branch, from_tag


def test_407_triggers_ack_then_authenticated_reinvite(client_setup):
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, *SERVER, LOCAL_RTP, USERNAME, PASSWORD)
    initial_invite = transport.sent[0][0]
    branch, from_tag = _branch_and_tag_from_invite(initial_invite)

    client.datagram_received(_build_407(call_id, branch, from_tag), SERVER)

    # 3 messages total: INVITE 50, ACK 50, INVITE 51
    assert len(transport.sent) == 3
    ack_start, ack_headers, _ = _parse(transport.sent[1][0])
    reinvite_start, reinvite_headers, reinvite_body = _parse(transport.sent[2][0])

    assert ack_start == f"ACK {TARGET_URI} SIP/2.0"
    assert ack_headers["cseq"] == "50 ACK"
    assert ack_headers["call-id"] == call_id
    # ACK echoes the To with server's tag (RFC 3261 §17.1.1.3).
    assert "tag=server-tag-xyz" in ack_headers["to"]

    assert reinvite_start == f"INVITE {TARGET_URI} SIP/2.0"
    assert reinvite_headers["cseq"] == "51 INVITE"
    assert reinvite_headers["call-id"] == call_id
    # New transaction → new Via branch.
    new_branch = re.search(r"branch=([^;]+)", reinvite_headers["via"]).group(1)
    assert new_branch != branch

    # Proxy-Authorization computed correctly.
    auth = reinvite_headers["proxy-authorization"]
    assert auth.startswith("Digest ")
    assert f'response="{_expected_digest_response("N0NCE-001")}"' in auth
    assert f'username="{USERNAME}"' in auth
    assert 'realm="asterisk"' in auth

    # State updated.
    assert client._calls[call_id].state == CallState.AUTHENTICATING


# --- 200 OK -----------------------------------------------------------------


def _build_200_ok(call_id: str, from_tag: str, cseq: int) -> bytes:
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
    return (
        "SIP/2.0 200 OK\r\n"
        f"Via: SIP/2.0/UDP {LOCAL_HOST}:{LOCAL_PORT};branch=ignored;rport\r\n"
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"To: <{TARGET_URI}>;tag=srv-confirmed\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} INVITE\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n"
        f"\r\n{sdp}"
    ).encode()


def test_200_ok_acks_and_notifies_with_rtp_endpoint(client_setup):
    client, transport, established, _ = client_setup
    call_id = client.call(TARGET_URI, *SERVER, LOCAL_RTP, USERNAME, PASSWORD)
    _, from_tag = _branch_and_tag_from_invite(transport.sent[0][0])

    # Go through 407 retry first.
    branch_initial = _branch_and_tag_from_invite(transport.sent[0][0])[0]
    client.datagram_received(_build_407(call_id, branch_initial, from_tag), SERVER)
    # Now the server confirms (CSeq 51 on the authenticated INVITE).
    client.datagram_received(_build_200_ok(call_id, from_tag, 51), SERVER)

    # 4 messages: INVITE 50, ACK 50, INVITE 51, ACK 51.
    assert len(transport.sent) == 4
    ack_start, ack_headers, _ = _parse(transport.sent[3][0])
    assert ack_start == f"ACK {TARGET_URI} SIP/2.0"
    assert ack_headers["cseq"] == "51 ACK"
    assert "tag=srv-confirmed" in ack_headers["to"]

    # Callback fired with parsed RTP info.
    assert len(established) == 1
    assert established[0] == CallEstablished(
        call_id=call_id,
        remote_rtp_ip="178.32.84.99",
        remote_rtp_port=20002,
        local_rtp_port=LOCAL_RTP,
    )
    assert client._calls[call_id].state == CallState.CONFIRMED


# --- BYE & hang_up ----------------------------------------------------------


def _confirm_call(client: IntratoneSipClient, transport: FakeTransport) -> tuple[str, str]:
    call_id = client.call(TARGET_URI, *SERVER, LOCAL_RTP, USERNAME, PASSWORD)
    branch, from_tag = _branch_and_tag_from_invite(transport.sent[0][0])
    client.datagram_received(_build_407(call_id, branch, from_tag), SERVER)
    client.datagram_received(_build_200_ok(call_id, from_tag, 51), SERVER)
    return call_id, from_tag


def test_incoming_bye_replies_200_and_terminates(client_setup):
    client, transport, _, terminated = client_setup
    call_id, from_tag = _confirm_call(client, transport)
    sent_before = len(transport.sent)

    bye = (
        f"BYE <sip:{USERNAME}@{LOCAL_HOST}> SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {SERVER[0]}:5060;branch=z9hG4bK-srvbye\r\n"
        f"From: <{TARGET_URI}>;tag=srv-confirmed\r\n"
        f"To: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 BYE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    client.datagram_received(bye, SERVER)

    assert len(transport.sent) == sent_before + 1
    start, headers, _ = _parse(transport.sent[-1][0])
    assert start == "SIP/2.0 200 OK"
    assert headers["cseq"] == "1 BYE"
    assert headers["call-id"] == call_id
    assert terminated == [call_id]
    assert call_id not in client._calls


def test_hang_up_sends_bye_with_bumped_cseq(client_setup):
    client, transport, _, terminated = client_setup
    call_id, _ = _confirm_call(client, transport)
    sent_before = len(transport.sent)

    client.hang_up(call_id)

    assert len(transport.sent) == sent_before + 1
    start, headers, _ = _parse(transport.sent[-1][0])
    assert start == f"BYE {TARGET_URI} SIP/2.0"
    # After 200 OK on CSeq 51, BYE bumps to 52.
    assert headers["cseq"] == "52 BYE"
    assert headers["call-id"] == call_id
    assert terminated == [call_id]


def test_hang_up_on_pending_invite_sends_cancel_and_terminates(client_setup):
    """If user accepts (via /answer REST) before the SIP 200 OK arrives, we
    must still wind the call down — otherwise the manager wedges and the next
    ring is dropped."""
    client, transport, _, terminated = client_setup
    call_id = client.call(TARGET_URI, *SERVER, LOCAL_RTP, USERNAME, PASSWORD)
    sent_before = len(transport.sent)

    client.hang_up(call_id)  # state == INVITING

    assert len(transport.sent) == sent_before + 1
    start, headers, _ = _parse(transport.sent[-1][0])
    assert start == f"CANCEL {TARGET_URI} SIP/2.0"
    assert headers["cseq"] == "50 CANCEL"
    assert headers["call-id"] == call_id
    assert terminated == [call_id]
    assert call_id not in client._calls


def test_hang_up_on_authenticating_call_sends_cancel(client_setup):
    """Same as above, after a 407 has been processed but before the auth'd 200 OK."""
    client, transport, _, terminated = client_setup
    call_id = client.call(TARGET_URI, *SERVER, LOCAL_RTP, USERNAME, PASSWORD)
    branch, from_tag = _branch_and_tag_from_invite(transport.sent[0][0])
    client.datagram_received(_build_407(call_id, branch, from_tag), SERVER)
    sent_before = len(transport.sent)

    client.hang_up(call_id)  # state == AUTHENTICATING

    start, headers, _ = _parse(transport.sent[-1][0])
    assert start.startswith("CANCEL")
    # CSeq was bumped to 51 for the re-INVITE; CANCEL matches it.
    assert headers["cseq"] == "51 CANCEL"
    assert terminated == [call_id]


# --- error paths ------------------------------------------------------------


def test_4xx_other_than_auth_terminates_call(client_setup):
    client, transport, _, terminated = client_setup
    call_id = client.call(TARGET_URI, *SERVER, LOCAL_RTP, USERNAME, PASSWORD)
    branch, from_tag = _branch_and_tag_from_invite(transport.sent[0][0])

    busy = (
        "SIP/2.0 486 Busy Here\r\n"
        f"Via: SIP/2.0/UDP {LOCAL_HOST}:{LOCAL_PORT};branch={branch};rport\r\n"
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag={from_tag}\r\n"
        f"To: <{TARGET_URI}>;tag=srv\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 50 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    client.datagram_received(busy, SERVER)

    # ACK sent, then call torn down.
    assert len(transport.sent) == 2
    ack_start, ack_headers, _ = _parse(transport.sent[1][0])
    assert ack_start == f"ACK {TARGET_URI} SIP/2.0"
    assert ack_headers["cseq"] == "50 ACK"
    assert terminated == [call_id]
    assert call_id not in client._calls


def test_invalid_digest_challenge_terminates(client_setup):
    client, transport, _, terminated = client_setup
    call_id = client.call(TARGET_URI, *SERVER, LOCAL_RTP, USERNAME, PASSWORD)

    bad_407 = (
        "SIP/2.0 407 Proxy Authentication Required\r\n"
        f"Via: SIP/2.0/UDP {LOCAL_HOST}:{LOCAL_PORT};branch=x;rport\r\n"
        f"From: <sip:{USERNAME}@{LOCAL_HOST}>;tag=t\r\n"
        f"To: <{TARGET_URI}>;tag=srv\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 50 INVITE\r\n"
        "Proxy-Authenticate: Basic realm=\"x\"\r\n"  # wrong scheme
        "Content-Length: 0\r\n\r\n"
    ).encode()
    client.datagram_received(bad_407, SERVER)

    assert terminated == [call_id]


def test_retransmitted_200_ok_re_acks_without_terminating(client_setup):
    """Asterisk retransmits 200 OK if it doesn't see our ACK; we must re-ACK
    silently, not tear the call down."""
    client, transport, established, terminated = client_setup
    call_id = client.call(TARGET_URI, *SERVER, LOCAL_RTP, USERNAME, PASSWORD)
    _, from_tag = _branch_and_tag_from_invite(transport.sent[0][0])

    # Go through 407 → 200 OK normally.
    branch_initial = _branch_and_tag_from_invite(transport.sent[0][0])[0]
    client.datagram_received(_build_407(call_id, branch_initial, from_tag), SERVER)
    client.datagram_received(_build_200_ok(call_id, from_tag, 51), SERVER)
    sent_after_ok = len(transport.sent)
    assert established  # the call established once

    # Same 200 OK arrives again (retransmission).
    client.datagram_received(_build_200_ok(call_id, from_tag, 51), SERVER)

    # One extra ACK sent, call still CONFIRMED, no termination callback.
    assert len(transport.sent) == sent_after_ok + 1
    ack_start, ack_headers, _ = _parse(transport.sent[-1][0])
    assert ack_start == f"ACK {TARGET_URI} SIP/2.0"
    assert ack_headers["cseq"] == "51 ACK"
    assert client._calls[call_id].state == CallState.CONFIRMED
    assert terminated == []
    assert len(established) == 1  # established once only


def test_ack_sent_to_source_address_not_original_target(client_setup):
    """For NAT'd setups Asterisk may respond from a different port — the ACK
    must go to the response's source address, not the INVITE destination."""
    client, transport, _, _ = client_setup
    call_id = client.call(TARGET_URI, *SERVER, LOCAL_RTP, USERNAME, PASSWORD)
    _, from_tag = _branch_and_tag_from_invite(transport.sent[0][0])
    branch_initial = _branch_and_tag_from_invite(transport.sent[0][0])[0]
    client.datagram_received(_build_407(call_id, branch_initial, from_tag), SERVER)

    # 200 OK comes back from a different port than the INVITE went to.
    different_port_addr = (SERVER[0], 6789)
    client.datagram_received(
        _build_200_ok(call_id, from_tag, 51), different_port_addr
    )

    # ACK after 200 OK should be sent to that different address.
    _, ack_addr = transport.sent[-1]
    assert ack_addr == different_port_addr


def test_stray_response_for_unknown_call_ignored(client_setup):
    client, transport, _, terminated = client_setup
    stray = (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/UDP somewhere\r\n"
        "From: <sip:x@h>\r\n"
        "To: <sip:y@h>\r\n"
        "Call-ID: nobody-knows-me\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    # Should not raise, should not send anything.
    client.datagram_received(stray, SERVER)
    assert transport.sent == []
    assert terminated == []


def test_malformed_datagram_does_not_crash(client_setup):
    client, transport, _, _ = client_setup
    client.datagram_received(b"not a SIP message at all", SERVER)
    assert transport.sent == []
