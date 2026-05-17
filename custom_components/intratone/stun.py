"""Minimal STUN responder for Intratone's video media port.

Intratone's media gateway sends STUN Binding Requests on the negotiated video
RTP port (and only the video port — audio rides plain RTP) as a NAT-traversal
keepalive. It withholds the actual VP8 RTP stream until it receives a Binding
Response back. Our spike (2026-05-16) confirmed: 167 STUN Binding Requests
received, zero VP8 frames, because we never replied.

This module implements only what's strictly needed: parse a Binding Request,
emit a Binding Response carrying XOR-MAPPED-ADDRESS for IPv4. No
MESSAGE-INTEGRITY, no FINGERPRINT, no LONG-TERM CREDENTIALS — Intratone's
checks don't include them.

RFC 5389 §6 (Binding) + §15.2 (XOR-MAPPED-ADDRESS).
"""

from __future__ import annotations

import socket
import struct

_MAGIC_COOKIE = 0x2112A442
_BINDING_REQUEST = 0x0001
_BINDING_RESPONSE = 0x0101
_ATTR_XOR_MAPPED_ADDRESS = 0x0020
_FAMILY_IPV4 = 0x01

_STUN_HEADER_FMT = ">HHI12s"  # type, length, magic cookie, txid
_STUN_HEADER_SIZE = 20


def is_stun_binding_request(data: bytes) -> bool:
    """True iff `data` is a well-formed STUN Binding Request."""
    if len(data) < _STUN_HEADER_SIZE:
        return False
    msg_type, _, magic, _ = struct.unpack(_STUN_HEADER_FMT, data[:_STUN_HEADER_SIZE])
    return msg_type == _BINDING_REQUEST and magic == _MAGIC_COOKIE


def build_binding_response(request: bytes, reflexive_addr: tuple[str, int]) -> bytes:
    """Build a Binding Response echoing the request's transaction ID.

    `reflexive_addr` is the (ip, port) the requester appeared to come from —
    we echo that back as XOR-MAPPED-ADDRESS so the gateway considers the path
    validated.
    """
    _, _, _, txid = struct.unpack(_STUN_HEADER_FMT, request[:_STUN_HEADER_SIZE])
    ip, port = reflexive_addr
    addr_bytes = socket.inet_aton(ip)
    # XOR rules (RFC 5389 §15.2): port ^ high 16 bits of magic cookie;
    # IPv4 address ^ full magic cookie.
    x_port = port ^ (_MAGIC_COOKIE >> 16)
    x_addr = bytes(b ^ c for b, c in zip(addr_bytes, _MAGIC_COOKIE.to_bytes(4, "big")))
    # Attribute: type(2) + length(2) + reserved(1)+family(1) + x-port(2) + x-addr(4)
    attr = struct.pack(
        ">HHBBH4s",
        _ATTR_XOR_MAPPED_ADDRESS,
        8,  # value length: 1B reserved + 1B family + 2B x-port + 4B x-addr
        0,
        _FAMILY_IPV4,
        x_port,
        x_addr,
    )
    header = struct.pack(
        _STUN_HEADER_FMT,
        _BINDING_RESPONSE,
        len(attr),
        _MAGIC_COOKIE,
        txid,
    )
    return header + attr
