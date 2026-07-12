"""go2rtc relay health checks.

Two levels, both returning None on success or a translation error key:

- `async_probe_go2rtc` — cheap RTSP OPTIONS round-trip, used by the config
  and options flows to reject an unreachable URL at save time.
- `async_selftest_go2rtc` — full-path check for the diagnostic button:
  publishes a short synthetic stream with ffmpeg (ANNOUNCE+RECORD, exactly
  what AudioBridge does during a call) then reads it back with DESCRIBE.
  It targets the same stream path as the bridge because a properly
  configured go2rtc pre-declares that path and may reject any other.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)

_RTSP_DEFAULT_PORT = 554
_PROBE_TIMEOUT_S = 3.0
_SELFTEST_TIMEOUT_S = 10.0
_SELFTEST_DESCRIBE_INTERVAL_S = 0.5

ERR_INVALID_URL = "invalid_url"
ERR_UNREACHABLE = "go2rtc_unreachable"
ERR_PUBLISH_REFUSED = "go2rtc_publish_refused"


def _parse_rtsp_url(url: str) -> tuple[str, int] | None:
    """Return (host, port) for an rtsp:// URL, or None if unusable."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "rtsp" or not parsed.hostname:
        return None
    try:
        port = parsed.port or _RTSP_DEFAULT_PORT
    except ValueError:
        return None
    return parsed.hostname, port


async def _rtsp_request(host: str, port: int, request: bytes) -> str:
    """Send one RTSP request over a fresh TCP connection, return the
    response status line (e.g. `RTSP/1.0 200 OK`)."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(request)
        await writer.drain()
        line = await reader.readline()
        return line.decode(errors="replace").strip()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def _is_rtsp_ok(status_line: str) -> bool:
    parts = status_line.split(maxsplit=2)
    return (
        len(parts) >= 2 and parts[0].startswith("RTSP/") and parts[1] == "200"
    )


async def async_probe_go2rtc(
    url: str, timeout: float = _PROBE_TIMEOUT_S
) -> str | None:
    """Check that an RTSP server answers OPTIONS at `url`.

    Proves something speaks RTSP at that address (go2rtc not running, wrong
    host/port and typos all fail here) — it does NOT prove the server
    accepts publishing; that's `async_selftest_go2rtc`.
    """
    endpoint = _parse_rtsp_url(url)
    if endpoint is None:
        return ERR_INVALID_URL
    host, port = endpoint
    request = (
        f"OPTIONS {url} RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "User-Agent: ha-intratone\r\n"
        "\r\n"
    ).encode()
    try:
        status = await asyncio.wait_for(
            _rtsp_request(host, port, request), timeout
        )
    except (OSError, asyncio.TimeoutError) as err:
        _LOGGER.debug("go2rtc probe of %s failed: %r", url, err)
        return ERR_UNREACHABLE
    if not _is_rtsp_ok(status):
        _LOGGER.debug("go2rtc probe of %s: unexpected response %r", url, status)
        return ERR_UNREACHABLE
    return None


async def async_selftest_go2rtc(
    url: str,
    *,
    ffmpeg_binary: str = "ffmpeg",
    timeout: float = _SELFTEST_TIMEOUT_S,
) -> str | None:
    """Publish a short synthetic stream to `url` and read it back.

    `url` must be the full stream URL the bridge publishes to (relay +
    path). Must not run while a call is up — it would fight the call's
    ffmpeg for the same go2rtc slot; callers are responsible for that check.
    """
    err = await async_probe_go2rtc(url)
    if err is not None:
        return err
    host, port = _parse_rtsp_url(url)  # probe validated it already

    args = [
        "-hide_banner",
        "-loglevel", "error",
        # -re is load-bearing: without it ffmpeg encodes the whole synthetic
        # clip in ~50ms, disconnects (rc=0), and go2rtc drops the producer
        # before our first DESCRIBE — a false "publish refused" on a healthy
        # relay. Real-time pacing keeps the publisher connected while we poll.
        "-re",
        "-f", "lavfi",
        "-i", "color=color=0x202020:size=320x180:rate=5",
        "-t", str(int(timeout) + 2),
        "-c:v", "libx264",
        "-tune", "zerolatency",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-rtsp_transport", "tcp",
        "-f", "rtsp",
        url,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg_binary,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as err:
        _LOGGER.error("go2rtc self-test: could not spawn ffmpeg: %s", err)
        return ERR_PUBLISH_REFUSED

    describe = (
        f"DESCRIBE {url} RTSP/1.0\r\n"
        "CSeq: 2\r\n"
        "Accept: application/sdp\r\n"
        "User-Agent: ha-intratone\r\n"
        "\r\n"
    ).encode()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while loop.time() < deadline:
            if process.returncode is not None:
                # ANNOUNCE refused (or ffmpeg broken) — the publish leg is
                # what a real call needs, so this is the actionable failure.
                stderr = b""
                if process.stderr is not None:
                    stderr = await process.stderr.read()
                _LOGGER.error(
                    "go2rtc self-test: ffmpeg exited rc=%s before the stream "
                    "was consumable: %s",
                    process.returncode,
                    stderr.decode(errors="replace").strip(),
                )
                return ERR_PUBLISH_REFUSED
            try:
                status = await asyncio.wait_for(
                    _rtsp_request(host, port, describe), _PROBE_TIMEOUT_S
                )
            except (OSError, asyncio.TimeoutError):
                status = ""
            if _is_rtsp_ok(status):
                _LOGGER.info("go2rtc self-test OK: %s publishes and serves", url)
                return None
            await asyncio.sleep(_SELFTEST_DESCRIBE_INTERVAL_S)
        _LOGGER.error(
            "go2rtc self-test: %s never became consumable within %ss "
            "(ffmpeg still alive — DESCRIBE kept failing)",
            url,
            timeout,
        )
        return ERR_PUBLISH_REFUSED
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
