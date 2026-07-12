"""HTTP/SIP Digest authentication (RFC 7616, MD5 only).

Intratone's SIP server (Asterisk) sends `407 Proxy Authentication Required`
with a `Proxy-Authenticate: Digest realm="...", nonce="...", ...` header on
every INVITE. We compute the matching Authorization header here.

Only the MD5 algorithm is implemented — the APK reverse engineering and the
captured traffic confirm Intratone never sends `algorithm=SHA-256` nor `qop`.
The qop=auth path is included anyway because some Asterisk versions enable
it via config.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class DigestChallenge:
    """Parsed `WWW-Authenticate` or `Proxy-Authenticate` Digest challenge."""

    realm: str
    nonce: str
    qop: str | None = None
    opaque: str | None = None
    algorithm: str = "MD5"


_KV_PATTERN = re.compile(
    r'(?P<key>[a-zA-Z0-9_-]+)\s*=\s*(?:"(?P<qval>[^"]*)"|(?P<val>[^,\s]+))'
)


def parse_challenge(header_value: str) -> DigestChallenge:
    """Parse a `Digest realm="x", nonce="y", ...` challenge value.

    Raises ValueError if the scheme is not Digest or required keys are missing.
    """
    stripped = header_value.strip()
    if not stripped.lower().startswith("digest"):
        raise ValueError(f"Not a Digest challenge: {header_value!r}")
    params = {
        m.group("key").lower(): m.group("qval") if m.group("qval") is not None else m.group("val")
        for m in _KV_PATTERN.finditer(stripped[len("digest") :])
    }
    try:
        return DigestChallenge(
            realm=params["realm"],
            nonce=params["nonce"],
            qop=params.get("qop"),
            opaque=params.get("opaque"),
            algorithm=params.get("algorithm", "MD5").upper(),
        )
    except KeyError as exc:
        raise ValueError(f"Missing required key {exc} in challenge") from exc


def build_authorization(
    challenge: DigestChallenge,
    username: str,
    password: str,
    method: str,
    uri: str,
    *,
    cnonce: str | None = None,
    nc: int = 1,
) -> str:
    """Build the `Digest username="...", ..., response="..."` Authorization value.

    For Intratone the request typically has no qop, so `cnonce` and `nc` are
    ignored. They're still computed for the qop=auth path.
    """
    if challenge.algorithm != "MD5":
        raise ValueError(f"Unsupported algorithm {challenge.algorithm!r}")

    ha1 = _md5(f"{username}:{challenge.realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")

    parts = [
        f'username="{username}"',
        f'realm="{challenge.realm}"',
        f'nonce="{challenge.nonce}"',
        f'uri="{uri}"',
        f'algorithm={challenge.algorithm}',
    ]

    if challenge.qop:
        if "auth" not in [q.strip() for q in challenge.qop.split(",")]:
            # qop="auth-int" only: HA2 must include the entity-body hash
            # (RFC 7616 §3.4.3) which we don't implement — answering anyway
            # would just loop on 407s with a wrong response.
            raise ValueError(f"Unsupported qop {challenge.qop!r}")
        qop_value = "auth"
        cnonce = cnonce or secrets.token_hex(8)
        nc_str = f"{nc:08x}"
        response = _md5(f"{ha1}:{challenge.nonce}:{nc_str}:{cnonce}:{qop_value}:{ha2}")
        parts.extend(
            [
                f"qop={qop_value}",
                f"nc={nc_str}",
                f'cnonce="{cnonce}"',
                f'response="{response}"',
            ]
        )
    else:
        response = _md5(f"{ha1}:{challenge.nonce}:{ha2}")
        parts.append(f'response="{response}"')

    if challenge.opaque:
        parts.append(f'opaque="{challenge.opaque}"')

    return "Digest " + ", ".join(parts)


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()
