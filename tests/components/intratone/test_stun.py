"""Unit tests for the minimal STUN responder (stun.py).

STUN Binding Responses are the make-or-break condition for video: the
Intratone media gateway withholds VP8 RTP until it gets one back on the
negotiated video port.
"""

from __future__ import annotations

import struct

from custom_components.intratone.stun import (
    build_binding_response,
    is_stun_binding_request,
)

_MAGIC_COOKIE = 0x2112A442
_HEADER_FMT = ">HHI12s"
_TXID = bytes(range(12))


def _binding_request(txid: bytes = _TXID) -> bytes:
    return struct.pack(_HEADER_FMT, 0x0001, 0, _MAGIC_COOKIE, txid)


def test_is_stun_binding_request_accepts_real_request() -> None:
    assert is_stun_binding_request(_binding_request())


def test_is_stun_binding_request_rejects_rtp_packet() -> None:
    # RTP version bits are 10 → first byte 0x80 (version 2, no padding/ext).
    rtp = bytes([0x80, 0x60]) + bytes(30)
    assert not is_stun_binding_request(rtp)


def test_is_stun_binding_request_rejects_short_and_garbage() -> None:
    assert not is_stun_binding_request(b"")
    assert not is_stun_binding_request(b"\x00\x01")
    assert not is_stun_binding_request(bytes(19))  # one byte short of a header
    # Right length but wrong magic cookie.
    assert not is_stun_binding_request(
        struct.pack(_HEADER_FMT, 0x0001, 0, 0xDEADBEEF, _TXID)
    )
    # A Binding *Response* is not a request.
    assert not is_stun_binding_request(
        struct.pack(_HEADER_FMT, 0x0101, 0, _MAGIC_COOKIE, _TXID)
    )


def test_build_binding_response_header() -> None:
    response = build_binding_response(_binding_request(), ("192.0.2.10", 4321))

    msg_type, length, magic, txid = struct.unpack(
        _HEADER_FMT, response[: struct.calcsize(_HEADER_FMT)]
    )
    assert msg_type == 0x0101  # Binding Response
    assert magic == _MAGIC_COOKIE
    assert txid == _TXID  # transaction id echoed verbatim
    # message-length counts attribute bytes only, not the 20-byte header:
    # 4 (attr type+length) + 8 (XOR-MAPPED-ADDRESS value) = 12.
    assert length == 12
    assert len(response) == 20 + length


def test_build_binding_response_xor_mapped_address_math() -> None:
    response = build_binding_response(_binding_request(), ("192.0.2.10", 4321))

    attr_type, attr_len, reserved, family = struct.unpack(">HHBB", response[20:26])
    assert attr_type == 0x0020  # XOR-MAPPED-ADDRESS
    assert attr_len == 8
    assert reserved == 0
    assert family == 0x01  # IPv4

    # X-Port = port ^ (cookie >> 16), big-endian on the wire:
    # 4321 (0x10E1) ^ 0x2112 = 0x31F3.
    assert response[26:28] == bytes.fromhex("31f3")
    # X-Address = IPv4 ^ full cookie: C0.00.02.0A ^ 21.12.A4.42 = E1.12.A6.48.
    assert response[28:32] == bytes.fromhex("e112a648")


def test_build_binding_response_echoes_any_transaction_id() -> None:
    txid = b"\xaa" * 12
    response = build_binding_response(
        _binding_request(txid), ("10.0.0.1", 5004)
    )
    assert response[8:20] == txid
