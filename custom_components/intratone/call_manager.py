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
import socket
from typing import Callable

from .audio_bridge import AudioBridge
from .sip_client import CallEstablished, IntratoneSipClient

_LOGGER = logging.getLogger(__name__)

_RTP_PORT_RANGE = range(16384, 16484, 2)  # even ports (RTP convention)

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


def _bind_rtp_pair() -> tuple[socket.socket, socket.socket]:
    """Bind an RTP+RTCP socket pair on consecutive (even, odd) ports.

    Used for the video media line where we want to send RFC 4585 PLI feedback
    on the RTCP port. Returns (rtp_sock, rtcp_sock); caller owns both.
    """
    last_error: OSError | None = None
    for port in _RTP_PORT_RANGE:
        rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp_sock.setblocking(False)
        try:
            rtp_sock.bind(("0.0.0.0", port))
        except OSError as err:
            last_error = err
            rtp_sock.close()
            continue
        rtcp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtcp_sock.setblocking(False)
        try:
            rtcp_sock.bind(("0.0.0.0", port + 1))
            return rtp_sock, rtcp_sock
        except OSError as err:
            last_error = err
            rtp_sock.close()
            rtcp_sock.close()
            continue
    raise RuntimeError(
        f"No free RTP+RTCP pair in {_RTP_PORT_RANGE!r}: {last_error}"
    )


class CallManager:
    """One AudioBridge + one TCP SIP client per active Intratone call."""

    def __init__(
        self,
        local_host: str,
        on_call_active: Callable[[str, str], None],
        on_call_ended: Callable[[str], None],
        video_enabled: bool = False,
        go2rtc_url: str = "rtsp://127.0.0.1:8554",
        audio_bridge: AudioBridge | None = None,
    ) -> None:
        self._local_host = local_host
        self._on_call_active = on_call_active
        self._on_call_ended = on_call_ended
        self._video_enabled = video_enabled
        self._bridge = audio_bridge or AudioBridge(rtsp_relay_url=go2rtc_url.rstrip("/"))
        self._sip_client: IntratoneSipClient | None = None
        self._sip_transport: asyncio.Transport | None = None
        self._active_call_id: str | None = None
        self._pending_rtp_socket: socket.socket | None = None
        self._pending_video_rtp_socket: socket.socket | None = None
        self._pending_video_rtcp_socket: socket.socket | None = None
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
        for attr in (
            "_pending_rtp_socket",
            "_pending_video_rtp_socket",
            "_pending_video_rtcp_socket",
        ):
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
        max_duration_s: float | None = None,
    ) -> str | None:
        """Open a TCP SIP connection and send INVITE. Returns Call-ID or None.

        `max_duration_s` overrides the hardcoded wedge-protection timeout —
        the coordinator forwards the server-side `callEndDelay` push field
        when present so we honour intercom-specific call windows. Falls back
        to `_MAX_CALL_DURATION_S` when None.
        """
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
        # Bind RTP + RTCP as a pair so PLI feedback (RFC 4585) can be sent on
        # the gateway's RTCP port (rtp_port + 1 by convention).
        video_rtp_port: int | None = None
        if self._video_enabled:
            video_rtp_socket, video_rtcp_socket = _bind_rtp_pair()
            video_rtp_port = video_rtp_socket.getsockname()[1]
            self._pending_video_rtp_socket = video_rtp_socket
            self._pending_video_rtcp_socket = video_rtcp_socket

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
        effective_max_s = (
            max_duration_s if max_duration_s and max_duration_s > 0 else _MAX_CALL_DURATION_S
        )
        self._max_duration_task = asyncio.create_task(
            self._auto_terminate_after(call_id, effective_max_s)
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

    def send_mute_off(self) -> bool:
        """Send the SIP MESSAGE `MUTE_OFF` for the active call.

        Mirrors the Cogelec app behaviour on manual pickup
        (`DECROCHER_AUTO=false`, the default state — see APK
        `CallManager.java:752-755`). The signal appears to keep the
        doorbell hardware engaged in full-duplex and may extend the
        server-side call window before BYE; harmless if the server ignores
        it. Fire-and-forget: we don't gate the bridge on its success.
        """
        if self._sip_client is None or self._active_call_id is None:
            _LOGGER.debug("send_mute_off: no active call")
            return False
        return self._sip_client.send_mute_off(self._active_call_id)

    def send_backlight(self) -> bool:
        """Send the SIP MESSAGE `contrast` (backlight mode) for the active
        call. Per the Cogelec app UI string, this asks the doorbell
        hardware to enable a front illuminator / high-gain camera mode for
        the rest of the call; reset by the server when BYE arrives.
        """
        if self._sip_client is None or self._active_call_id is None:
            _LOGGER.warning("send_backlight: no active call")
            return False
        return self._sip_client.send_backlight(self._active_call_id)

    async def hang_up(self) -> None:
        """End the active call (user-initiated or explicit hangup)."""
        if self._sip_client is None or self._active_call_id is None:
            return
        self._sip_client.hang_up(self._active_call_id)
        await self._bridge.stop()

    async def abort_active_call(self) -> None:
        """Tear down the current call immediately so a new ring can start
        fresh. Handles two states:

        - SIP dialog still up → send BYE + stop bridge + close transport.
        - Post-BYE grace (sip_client cleared, bridge waiting on the 60 s
          teardown task) → cancel the grace task + stop bridge now.

        In both cases, fires `on_call_ended` synchronously so the coordinator
        clears its pending/active state before a new ring overrides it.
        Idempotent: no-op if no call is currently tracked.

        Used by the coordinator when a fresh FCM push arrives — the old
        call's credentials are stale (the Intratone server issues a new
        SIP dialog per ring and the doorbell hardware only handles one
        audio channel at a time) so there's no point keeping it alive.
        """
        call_id = self._active_call_id
        if call_id is None:
            return
        for attr in ("_max_duration_task", "_grace_task"):
            task = getattr(self, attr)
            if task is not None and not task.done():
                task.cancel()
            setattr(self, attr, None)
        if self._sip_client is not None:
            self._sip_client.hang_up(call_id)
        if self._sip_transport is not None and not self._sip_transport.is_closing():
            self._sip_transport.close()
        self._sip_transport = None
        self._sip_client = None
        self._close_pending_sockets()
        await self._bridge.stop()
        self._active_call_id = None
        self._on_call_ended(call_id)

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
        video_rtcp_socket = self._pending_video_rtcp_socket
        self._pending_video_rtcp_socket = None
        if rtp_socket is None:
            _LOGGER.warning(
                "No pending RTP socket for call %s — bridge cannot start",
                info.call_id,
            )
            for sock in (video_rtp_socket, video_rtcp_socket):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if self._sip_client is not None:
                self._sip_client.hang_up(info.call_id)
            return
        # If the server rejected our m=video offer, drop the unused sockets.
        if info.remote_video_rtp_port is None and video_rtp_socket is not None:
            for sock in (video_rtp_socket, video_rtcp_socket):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            video_rtp_socket = None
            video_rtcp_socket = None
        try:
            rtsp_url = await self._bridge.start(
                rtp_socket=rtp_socket,
                remote_rtp_ip=info.remote_rtp_ip,
                remote_rtp_port=info.remote_rtp_port,
                video_socket=video_rtp_socket,
                remote_video_rtp_ip=info.remote_video_rtp_ip,
                remote_video_rtp_port=info.remote_video_rtp_port,
                video_rtcp_socket=video_rtcp_socket,
                on_video_failure=self._handle_video_failure,
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
        # Signal manual pickup engagement to the server. Mirrors the Cogelec
        # app on default-pref DECROCHER_AUTO=false: a one-shot SIP MESSAGE
        # body `MUTE_OFF` sent right after the dialog is up. Empirically
        # appears to keep the doorbell hardware engaged longer before BYE.
        self.send_mute_off()
        self._on_call_active(info.call_id, rtsp_url)

    async def _teardown_bridge(self, call_id: str) -> None:
        await self._bridge.stop()
        self._on_call_ended(call_id)

    def _handle_video_failure(self) -> None:
        """AudioBridge PLI loop gave up — no VP8 keyframe arrived.

        Mirrors the iOS app's `Failed to update call to audio-only:` flow:
        send an in-dialog re-INVITE without `m=video` so the gateway stops
        wasting bandwidth on a stream nothing can decode. Audio path is
        unaffected; the visitor remains audible / openable. Sync callback
        from `_pli_loop` — runs on the event loop thread, must not await.
        """
        if self._sip_client is None or self._active_call_id is None:
            return
        try:
            self._sip_client.send_reinvite_audio_only(self._active_call_id)
        except Exception:  # noqa: BLE001 — never let bridge crashes propagate
            _LOGGER.exception("re-INVITE audio-only failed")
