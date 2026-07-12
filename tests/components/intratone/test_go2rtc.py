"""go2rtc probe + self-test — RTSP exchanges mocked at the socket level."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.intratone.go2rtc import (
    ERR_INVALID_URL,
    ERR_PUBLISH_REFUSED,
    ERR_UNREACHABLE,
    async_probe_go2rtc,
    async_selftest_go2rtc,
)

URL = "rtsp://127.0.0.1:8554/intratone"


def _fake_connection(response: bytes):
    """(reader, writer) pair answering one readline with `response`."""
    reader = MagicMock()
    reader.readline = AsyncMock(return_value=response)
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


@pytest.fixture
def rtsp_ok():
    with patch(
        "custom_components.intratone.go2rtc.asyncio.open_connection",
        new=AsyncMock(return_value=_fake_connection(b"RTSP/1.0 200 OK\r\n")),
    ) as conn:
        yield conn


async def test_probe_rejects_non_rtsp_scheme() -> None:
    assert await async_probe_go2rtc("http://127.0.0.1:8554") == ERR_INVALID_URL
    assert await async_probe_go2rtc("not a url") == ERR_INVALID_URL
    assert await async_probe_go2rtc("rtsp://") == ERR_INVALID_URL


async def test_probe_ok_on_rtsp_200(rtsp_ok) -> None:
    assert await async_probe_go2rtc(URL) is None
    rtsp_ok.assert_awaited_once_with("127.0.0.1", 8554)


async def test_probe_unreachable_on_connection_refused() -> None:
    with patch(
        "custom_components.intratone.go2rtc.asyncio.open_connection",
        new=AsyncMock(side_effect=ConnectionRefusedError),
    ):
        assert await async_probe_go2rtc(URL) == ERR_UNREACHABLE


async def test_probe_unreachable_on_non_rtsp_response() -> None:
    """A web server (e.g. the go2rtc API port instead of the RTSP port)
    answers with HTTP — must not pass as healthy."""
    with patch(
        "custom_components.intratone.go2rtc.asyncio.open_connection",
        new=AsyncMock(
            return_value=_fake_connection(b"HTTP/1.1 400 Bad Request\r\n")
        ),
    ):
        assert await async_probe_go2rtc(URL) == ERR_UNREACHABLE


async def test_probe_unreachable_on_rtsp_error_status() -> None:
    with patch(
        "custom_components.intratone.go2rtc.asyncio.open_connection",
        new=AsyncMock(
            return_value=_fake_connection(b"RTSP/1.0 404 Not Found\r\n")
        ),
    ):
        assert await async_probe_go2rtc(URL) == ERR_UNREACHABLE


def _fake_ffmpeg(returncode=None):
    process = MagicMock()
    process.returncode = returncode
    process.kill = MagicMock()
    process.wait = AsyncMock()
    process.stderr = MagicMock()
    process.stderr.read = AsyncMock(return_value=b"Connection refused")
    return process


async def test_selftest_ok_when_publish_accepted_and_describe_200(
    rtsp_ok,
) -> None:
    """ffmpeg stays alive and DESCRIBE answers 200 → full path validated,
    the publisher is killed on the way out."""
    process = _fake_ffmpeg()
    with patch(
        "custom_components.intratone.go2rtc.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        assert await async_selftest_go2rtc(URL) is None
    process.kill.assert_called_once()


async def test_selftest_publish_refused_when_ffmpeg_dies() -> None:
    """OPTIONS is fine but go2rtc refuses the ANNOUNCE (stream path not
    declared, auth, …) → ffmpeg exits and the self-test reports it."""

    responses = iter([b"RTSP/1.0 200 OK\r\n"])  # only the OPTIONS probe

    async def connect(host, port):
        return _fake_connection(next(responses))

    process = _fake_ffmpeg(returncode=1)
    with (
        patch(
            "custom_components.intratone.go2rtc.asyncio.open_connection",
            new=AsyncMock(side_effect=connect),
        ),
        patch(
            "custom_components.intratone.go2rtc.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    ):
        assert await async_selftest_go2rtc(URL) == ERR_PUBLISH_REFUSED
    process.kill.assert_not_called()


async def test_selftest_fails_fast_when_probe_fails() -> None:
    """No ffmpeg is spawned at all when the cheap probe already fails."""
    with (
        patch(
            "custom_components.intratone.go2rtc.asyncio.open_connection",
            new=AsyncMock(side_effect=ConnectionRefusedError),
        ),
        patch(
            "custom_components.intratone.go2rtc.asyncio.create_subprocess_exec",
            new=AsyncMock(),
        ) as spawn,
    ):
        assert await async_selftest_go2rtc(URL) == ERR_UNREACHABLE
    spawn.assert_not_called()


async def test_selftest_publish_refused_on_describe_timeout() -> None:
    """ffmpeg alive but the stream never becomes consumable → refused
    after the (shortened) deadline."""
    process = _fake_ffmpeg()
    responses = iter(
        [b"RTSP/1.0 200 OK\r\n"]  # OPTIONS
    )

    async def connect(host, port):
        try:
            return _fake_connection(next(responses))
        except StopIteration:
            return _fake_connection(b"RTSP/1.0 500 Internal Server Error\r\n")

    with (
        patch(
            "custom_components.intratone.go2rtc.asyncio.open_connection",
            new=AsyncMock(side_effect=connect),
        ),
        patch(
            "custom_components.intratone.go2rtc.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
        patch(
            "custom_components.intratone.go2rtc._SELFTEST_DESCRIBE_INTERVAL_S",
            0.01,
        ),
    ):
        assert await async_selftest_go2rtc(URL, timeout=0.05) == (
            ERR_PUBLISH_REFUSED
        )
    process.kill.assert_called_once()
