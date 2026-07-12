"""Tests for digest_auth — vectors from RFC 2617 (the classic HTTP one) and
RFC 7616, plus an Intratone-shaped no-qop variant matching what the SIP
server actually sends.
"""

from __future__ import annotations

import pytest

from custom_components.intratone.digest_auth import (
    DigestChallenge,
    build_authorization,
    parse_challenge,
)


# --- parse_challenge ---------------------------------------------------------


def test_parse_challenge_minimal_intratone_shape():
    raw = 'Digest realm="asterisk", nonce="abc123"'
    c = parse_challenge(raw)
    assert c.realm == "asterisk"
    assert c.nonce == "abc123"
    assert c.qop is None
    assert c.algorithm == "MD5"


def test_parse_challenge_with_qop_and_opaque():
    raw = (
        'Digest realm="testrealm@host.com", '
        'qop="auth,auth-int", '
        'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
        'opaque="5ccc069c403ebaf9f0171e9517f40e41"'
    )
    c = parse_challenge(raw)
    assert c.realm == "testrealm@host.com"
    assert c.qop == "auth,auth-int"
    assert c.opaque == "5ccc069c403ebaf9f0171e9517f40e41"


def test_parse_challenge_case_insensitive_scheme():
    raw = 'digest realm="x", nonce="y"'
    assert parse_challenge(raw).realm == "x"


def test_parse_challenge_rejects_basic():
    with pytest.raises(ValueError, match="Not a Digest"):
        parse_challenge('Basic realm="x"')


def test_parse_challenge_rejects_missing_keys():
    with pytest.raises(ValueError, match="Missing required key"):
        parse_challenge('Digest realm="x"')


# --- build_authorization (Intratone-style, no qop) --------------------------


def test_build_authorization_intratone_no_qop():
    """Vector computed by hand for the actual Intratone creds.

    Matches what cogelecTest/CogeleC produces against an Asterisk that
    challenges INVITE without qop.
    """
    challenge = DigestChallenge(realm="asterisk", nonce="abc123")
    auth = build_authorization(
        challenge,
        username="cogelecTest",
        password="CogeleC",
        method="INVITE",
        uri="sip:2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36@178.32.84.135",
    )
    # HA1 = md5("cogelecTest:asterisk:CogeleC")
    # HA2 = md5("INVITE:sip:2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36@178.32.84.135")
    # response = md5(HA1 + ":abc123:" + HA2)
    import hashlib

    ha1 = hashlib.md5(b"cogelecTest:asterisk:CogeleC").hexdigest()
    ha2 = hashlib.md5(
        b"INVITE:sip:2DO77UAO49XTGJ5Y93TFIZ8YLPIMXN36@178.32.84.135"
    ).hexdigest()
    expected_response = hashlib.md5(f"{ha1}:abc123:{ha2}".encode()).hexdigest()

    assert f'response="{expected_response}"' in auth
    assert 'username="cogelecTest"' in auth
    assert 'realm="asterisk"' in auth
    assert 'nonce="abc123"' in auth
    assert "qop=" not in auth
    assert "cnonce=" not in auth


# --- build_authorization (RFC 2617 vector) ----------------------------------


def test_build_authorization_rfc2617_no_qop():
    """RFC 2617 §3.5 — historical Digest example without qop."""
    challenge = DigestChallenge(
        realm="testrealm@host.com",
        nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093",
        opaque="5ccc069c403ebaf9f0171e9517f40e41",
    )
    auth = build_authorization(
        challenge,
        username="Mufasa",
        password="Circle Of Life",
        method="GET",
        uri="/dir/index.html",
    )
    # RFC 2617 §3.5: response = "670fd8c2df070c60b045671b8b24ff02"
    assert 'response="670fd8c2df070c60b045671b8b24ff02"' in auth
    assert 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in auth


# --- build_authorization (qop=auth) -----------------------------------------


def test_build_authorization_qop_auth_uses_fixed_cnonce():
    """RFC 2617 §3.5 — Digest with qop=auth, deterministic via fixed cnonce."""
    challenge = DigestChallenge(
        realm="testrealm@host.com",
        nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093",
        qop="auth",
        opaque="5ccc069c403ebaf9f0171e9517f40e41",
    )
    auth = build_authorization(
        challenge,
        username="Mufasa",
        password="Circle Of Life",
        method="GET",
        uri="/dir/index.html",
        cnonce="0a4f113b",
        nc=1,
    )
    # RFC 2617 §3.5: response = "6629fae49393a05397450978507c4ef1"
    assert 'response="6629fae49393a05397450978507c4ef1"' in auth
    assert "qop=auth" in auth
    assert "nc=00000001" in auth
    assert 'cnonce="0a4f113b"' in auth


def test_build_authorization_qop_auth_generates_cnonce():
    """When no cnonce is provided, we generate a fresh one each call."""
    challenge = DigestChallenge(realm="r", nonce="n", qop="auth")
    a = build_authorization(challenge, "u", "p", "INVITE", "sip:x@h")
    b = build_authorization(challenge, "u", "p", "INVITE", "sip:x@h")
    # Same inputs but different cnonces → different responses.
    assert a != b


def test_build_authorization_rejects_unsupported_algorithm():
    challenge = DigestChallenge(realm="r", nonce="n", algorithm="SHA-256")
    with pytest.raises(ValueError, match="Unsupported algorithm"):
        build_authorization(challenge, "u", "p", "INVITE", "sip:x@h")


def test_build_authorization_rejects_auth_int_only_qop():
    """qop="auth-int" only: HA2 must hash the entity body (RFC 7616 §3.4.3)
    which we don't implement — answering anyway guarantees a 407 loop, so
    raise like the unsupported-algorithm case."""
    challenge = DigestChallenge(realm="r", nonce="n", qop="auth-int")
    with pytest.raises(ValueError, match="Unsupported qop"):
        build_authorization(challenge, "u", "p", "INVITE", "sip:x@h")


def test_build_authorization_qop_auth_and_auth_int_picks_auth():
    """When the server offers both, we pick plain `auth` — unchanged path."""
    challenge = DigestChallenge(realm="r", nonce="n", qop="auth,auth-int")
    auth = build_authorization(challenge, "u", "p", "INVITE", "sip:x@h")
    assert "qop=auth," in auth
    assert "qop=auth-int" not in auth
