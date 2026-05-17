"""Orchestrates the SIP UAC (TCP), audio bridge, and HA-facing callbacks.

Owns one AudioBridge whose ffmpeg process cycles per call. The SIP transport
is TCP per call — we open a fresh TCP connection to the Intratone server when
a ring arrives and close it on BYE / hang_up. This matches the Cogelec app's
behavior (liblinphone configured TCP-only).

Flow per ring:

    FCM push  → CallManager.start_call(target_uri, sip_server_ip, user, pass)
              → open TCP connection → IntratoneSipClient.call() → INVITE → ...
    sip_client.on_call_established(rtp_endpoint)
              → AudioBridge.start(local_rtp_port) → RTSP URL
              → on_call_active(rtsp_url) callback (coordinator sets camera state)
    [audio flows; HomeKit pulls RTSP]
    hang_up()  ← coordinator.async_open_door() (user accepted)
       or
    BYE        ← server (visitor or timeout)
              → AudioBridge.stop()
              → on_call_ended() callback
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Callable

from .audio_bridge import AudioBridge
from .sip_client import CallEstablished, IntratoneSipClient

_LOGGER = logging.getLogger(__name__)

_RTP_PORT_RANGE = range(16384, 16484, 2)  # even ports (RTP convention)

# go2rtc RTSP relay URL. The integration pushes its transcoded audio+video
# stream here and HA's HomeKit Bridge ffmpeg pulls from the same URL.
# Default targets the standalone go2rtc on its conventional port; HA OS users
# who rely on HA's embedded go2rtc (since 2024.x) should override to
# `rtsp://127.0.0.1:18554` — see README "go2rtc setup".
_GO2RTC_URL = (
    os.environ.get("INTRATONE_GO2RTC_URL", "rtsp://127.0.0.1:8554").rstrip("/")
)

# Roll-out gate: when unset the integration behaves like Phase 2 strict (audio
# only, no m=video in SDP, no video socket allocated). Flip to 1 to opt in to
# the VP8 video path. Lets us validate audio+HomeKit playback first before
# stacking the video pipeline on top.
_VIDEO_ENABLED = bool(int(os.environ.get("INTRATONE_VIDEO_ENABLED", "0") or "0"))
# Intratone doorbell calls don't last more than ~30 s in real life; if we never
# see a BYE the call is wedged. Hard-stop after this so the next ring isn't
# blocked by the "Call already active" guard.
_MAX_CALL_DURATION_S = 120
# Intratone tears the call down ~15-20s after the visitor stops pressing the
# button. We keep the audio bridge + camera stream URL alive this long AFTER
# the SIP BYE so the iPhone live-view RTSP pull (HomeKit hubs fire it ~8s
# after the doorbell event) still finds a valid source.
_POST_BYE_GRACE_S = 60
# TCP connect timeout — Intratone's blocip server is hosted in France with low
# latency from typical EU/HA installs; anything over a couple of seconds means
# something is wrong (DNS, firewall) and we'd rather drop the ring than hang
# the FCM listener thread.
_SIP_CONNECT_TIMEOUT_S = 5.0


def _bind_rtp_socket() -> socket.socket:
    """Bind a UDP socket on a free port in the RTP range and hand it back.

    Returns the bound socket; ownership transfers to the caller (must close).
    The port is held continuously from probe to use — no bind/probe race.
    """
    last_error: OSError | None = None
    for port in _RTP_PORT_RANGE:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            sock.bind(("0.0.0.0", port))
            return sock
        except OSError as err:
            last_error = err
            sock.close()
            continue
    raise RuntimeError(
        f"No free RTP port in range {_RTP_PORT_RANGE!r}: {last_error}"
    )


class CallManager:
    """One AudioBridge + one TCP SIP client per active Intratone call."""

    def __init__(
        self,
        local_host: str,
        on_call_active: Callable[[str, str], None],
        on_call_ended: Callable[[str], None],
        audio_bridge: AudioBridge | None = None,
    ) -> None:
        self._local_host = local_host
        self._on_call_active = on_call_active
        self._on_call_ended = on_call_ended
        self._bridge = audio_bridge or AudioBridge(rtsp_relay_url=_GO2RTC_URL)
        self._sip_client: IntratoneSipClient | None = None
        self._sip_transport: asyncio.Transport | None = None
        self._active_call_id: str | None = None
        self._pending_rtp_socket: socket.socket | None = None
        self._pending_video_rtp_socket: socket.socket | None = None
        self._max_duration_task: asyncio.Task | None = None
        self._grace_task: asyncio.Task | None = None
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started

    @property
    def active_call_id(self) -> str | None:
        return self._active_call_id

    async def async_start(self) -> None:
        """Mark the manager as started. SIP TCP connections are per-call now;
        nothing to bind here. Idempotent."""
        self._started = True

    async def async_stop(self) -> None:
        """Tear down audio bridge and any open SIP socket. Idempotent."""
        await self._bridge.stop()
        if self._sip_transport is not None and not self._sip_transport.is_closing():
            self._sip_transport.close()
        self._sip_transport = None
        self._sip_client = None
        self._active_call_id = None
        # Cancel any pending background tasks — without this they linger for
        # up to `_MAX_CALL_DURATION_S` (120 s) or `_POST_BYE_GRACE_S` (60 s)
        # after the integration stops, causing pytest-homeassistant to fail
        # tests with "Lingering task" errors and leaking memory in prod.
        for attr in ("_max_duration_task", "_grace_task"):
            task = getattr(self, attr)
            if task is not None and not task.done():
                task.cancel()
            setattr(self, attr, None)
        self._close_pending_sockets()
        self._started = False

    def _close_pending_sockets(self) -> None:
        """Close any RTP sockets we bound but haven't yet handed to AudioBridge."""
        for attr in ("_pending_rtp_socket", "_pending_video_rtp_socket"):
            sock = getattr(self, attr)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, attr, None)

    async def start_call(
        self,
        target_uri: str,
        target_host: str,
        sip_username: str,
        sip_password: str,
        target_port: int = 5060,
    ) -> str | None:
        """Open a TCP SIP connection and send INVITE. Returns Call-ID or None."""
        if not self._started:
            _LOGGER.warning("CallManager not started; cannot place call")
            return None
        if self._active_call_id is not None:
            _LOGGER.info(
                "Call %s already active; ignoring overlapping ring",
                self._active_call_id,
            )
            return None

        # Bind the audio RTP socket NOW so the port we advertise in SDP is
        # exactly the one ffmpeg will receive on — no probe/use race.
        rtp_socket = _bind_rtp_socket()
        rtp_port = rtp_socket.getsockname()[1]
        self._pending_rtp_socket = rtp_socket

        # Video: only allocated when the feature is enabled. The downstream
        # code (sip_client SDP builder, AudioBridge) treats None as "no video".
        video_rtp_port: int | None = None
        if _VIDEO_ENABLED:
            video_rtp_socket = _bind_rtp_socket()
            video_rtp_port = video_rtp_socket.getsockname()[1]
            self._pending_video_rtp_socket = video_rtp_socket

        loop = asyncio.get_running_loop()
        sip_client = IntratoneSipClient(
            local_host=self._local_host,
            on_call_established=self._handle_call_established,
            on_call_terminated=self._handle_call_terminated,
        )
        try:
            transport, _ = await asyncio.wait_for(
                loop.create_connection(
                    lambda: sip_client, host=target_host, port=target_port
                ),
                timeout=_SIP_CONNECT_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, OSError) as err:
            _LOGGER.error(
                "SIP TCP connect to %s:%d failed: %s",
                target_host,
                target_port,
                err,
            )
            self._close_pending_sockets()
            return None

        self._sip_client = sip_client
        self._sip_transport = transport
        try:
            call_id = sip_client.call(
                target_uri=target_uri,
                local_rtp_port=rtp_port,
                sip_username=sip_username,
                sip_password=sip_password,
                local_video_rtp_port=video_rtp_port,
            )
        except Exception:
            self._close_pending_sockets()
            transport.close()
            self._sip_transport = None
            self._sip_client = None
            raise

        self._active_call_id = call_id
        self._max_duration_task = asyncio.create_task(
            self._auto_terminate_after(call_id, _MAX_CALL_DURATION_S)
        )
        _LOGGER.info(
            "Outgoing SIP INVITE: call_id=%s target=%s local_rtp_port=%d",
            call_id,
            target_uri,
            rtp_port,
        )
        return call_id

    async def _auto_terminate_after(self, call_id: str, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        if self._active_call_id != call_id:
            return
        _LOGGER.warning(
            "Call %s exceeded %ds without BYE — forcing teardown", call_id, delay_s
        )
        if self._sip_client is not None:
            self._sip_client.hang_up(call_id)

    def send_open_door(self, code: str = "*") -> bool:
        """Send the SIP MESSAGE `opendoor:<code>` for the active call.

        This is what physically opens the door — the REST `/answer` endpoint
        only marks the call as picked up server-side. See APK
        `CallManager.sendMessage("opendoor:" + digit)` in
        com.cogelec.notificationpush.activities.CallActivity.
        """
        if self._sip_client is None or self._active_call_id is None:
            _LOGGER.warning("send_open_door: no active call")
            return False
        return self._sip_client.send_open_door(self._active_call_id, code)

    async def hang_up(self) -> None:
        """End the active call (user-initiated or explicit hangup)."""
        if self._sip_client is None or self._active_call_id is None:
            return
        self._sip_client.hang_up(self._active_call_id)
        await self._bridge.stop()

    # --- IntratoneSipClient callbacks (sync — must not await) -----------

    def _handle_call_established(self, info: CallEstablished) -> None:
        if info.call_id != self._active_call_id:
            _LOGGER.debug("Ignoring stale call_established for %s", info.call_id)
            return
        asyncio.create_task(self._spawn_bridge(info))

    def _handle_call_terminated(self, call_id: str) -> None:
        if call_id != self._active_call_id:
            return
        if self._max_duration_task is not None and not self._max_duration_task.done():
            self._max_duration_task.cancel()
        self._max_duration_task = None
        # If the call never reached CONFIRMED, both RTP sockets were bound but
        # never handed to AudioBridge — close them here to avoid leaks.
        self._close_pending_sockets()
        # The TCP transport closes itself in sip_client._terminate; clear our
        # reference. Keep `_active_call_id` set during the grace period so the
        # camera entity continues advertising the stream URL.
        self._sip_transport = None
        self._sip_client = None
        # Track the grace-period task so async_stop() can cancel it cleanly
        # instead of leaving it lingering for `_POST_BYE_GRACE_S` (60 s).
        if self._grace_task is not None and not self._grace_task.done():
            self._grace_task.cancel()
        self._grace_task = asyncio.create_task(
            self._teardown_bridge_after_grace(call_id)
        )

    async def _teardown_bridge_after_grace(self, call_id: str) -> None:
        try:
            await asyncio.sleep(_POST_BYE_GRACE_S)
        except asyncio.CancelledError:
            return
        if self._active_call_id != call_id:
            return  # superseded by hang_up / new call
        self._active_call_id = None
        await self._teardown_bridge(call_id)

    async def _spawn_bridge(self, info: CallEstablished) -> None:
        rtp_socket = self._pending_rtp_socket
        self._pending_rtp_socket = None
        video_rtp_socket = self._pending_video_rtp_socket
        self._pending_video_rtp_socket = None
        if rtp_socket is None:
            _LOGGER.warning(
                "No pending RTP socket for call %s — bridge cannot start",
                info.call_id,
            )
            if video_rtp_socket is not None:
                try:
                    video_rtp_socket.close()
                except OSError:
                    pass
            if self._sip_client is not None:
                self._sip_client.hang_up(info.call_id)
            return
        # If the server rejected our m=video offer, drop the unused socket.
        if info.remote_video_rtp_port is None and video_rtp_socket is not None:
            try:
                video_rtp_socket.close()
            except OSError:
                pass
            video_rtp_socket = None
        try:
            rtsp_url = await self._bridge.start(
                rtp_socket=rtp_socket,
                remote_rtp_ip=info.remote_rtp_ip,
                remote_rtp_port=info.remote_rtp_port,
                video_socket=video_rtp_socket,
                remote_video_rtp_ip=info.remote_video_rtp_ip,
                remote_video_rtp_port=info.remote_video_rtp_port,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Audio bridge failed to start: %s", err)
            if self._sip_client is not None:
                self._sip_client.hang_up(info.call_id)
            return
        _LOGGER.info(
            "Bridge up: %s (audio peer %s:%d%s)",
            rtsp_url,
            info.remote_rtp_ip,
            info.remote_rtp_port,
            (
                f", video peer {info.remote_video_rtp_ip}:{info.remote_video_rtp_port}"
                if info.remote_video_rtp_port is not None
                else ", video disabled"
            ),
        )
        self._on_call_active(info.call_id, rtsp_url)

    async def _teardown_bridge(self, call_id: str) -> None:
        await self._bridge.stop()
        self._on_call_ended(call_id)
