"""Orchestrates the SIP UAC, audio bridge, and HA-facing callbacks for one entry.

Owns the long-lived SIP UDP socket (so Asterisk can reach us across multiple
calls without per-call port churn) and one AudioBridge whose ffmpeg process
cycles per call.

Flow per ring:

    FCM push  → CallManager.start_call(target_uri, sip_server_ip, user, pass)
              → IntratoneSipClient.call() → INVITE → 407 → INVITE+auth → 200 OK
    sip_client.on_call_established(rtp_endpoint)
              → AudioBridge.start(local_rtp_port) → RTSP URL
              → on_call_active(rtsp_url) callback (coordinator sets camera state)
    [audio flows; HomeKit pulls RTSP]
    hang_up()  ← coordinator.async_open_door() (user accepted)
       or
    BYE        ← server (visitor or timeout)
              → AudioBridge.stop()
              → on_call_ended() callback

Local RTP port: picked once per call from a small range; race with another
binder is rare and handled by the SIP call simply failing — the doorbell will
ring again on retry.
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
# see a BYE the call is wedged (e.g. ghost from a stale `simulate_ring` or a
# missed Asterisk teardown). Hard-stop after this so the next ring isn't
# blocked by the "Call already active" guard in `start_call`.
_MAX_CALL_DURATION_S = 120


def _bind_rtp_socket() -> socket.socket:
    """Bind a UDP socket on a free port in the RTP range and hand it back.

    Returns the bound socket; ownership transfers to the caller (must close).
    Eliminates the bind/probe race that a separate `_pick_rtp_port` helper
    would introduce — the port is held continuously from probe to use.
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
    """One SIP UDP endpoint + one AudioBridge per Intratone config entry."""

    def __init__(
        self,
        local_host: str,
        on_call_active: Callable[[str, str], None],
        on_call_ended: Callable[[str], None],
        sip_port: int = 0,
        audio_bridge: AudioBridge | None = None,
    ) -> None:
        self._local_host = local_host
        self._sip_port_request = sip_port
        self._on_call_active = on_call_active
        self._on_call_ended = on_call_ended
        self._bridge = audio_bridge or AudioBridge()
        self._transport: asyncio.DatagramTransport | None = None
        self._sip_client: IntratoneSipClient | None = None
        self._active_call_id: str | None = None
        # Sockets held from start_call until handed to AudioBridge in
        # _spawn_bridge. Closed on hang_up if the call never reaches CONFIRMED.
        self._pending_rtp_sockets: dict[str, socket.socket] = {}
        self._max_duration_task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._transport is not None

    @property
    def active_call_id(self) -> str | None:
        return self._active_call_id

    async def async_start(self) -> None:
        """Bind the SIP UDP socket. Idempotent."""
        if self._transport is not None:
            return

        loop = asyncio.get_running_loop()
        self._sip_client = IntratoneSipClient(
            local_host=self._local_host,
            local_port=self._sip_port_request,
            on_call_established=self._handle_call_established,
            on_call_terminated=self._handle_call_terminated,
        )
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self._sip_client,  # type: ignore[return-value]
            local_addr=("0.0.0.0", self._sip_port_request),
            family=socket.AF_INET,
        )
        self._transport = transport
        bound_host, bound_port = transport.get_extra_info("sockname")
        # SIP client uses the actual bound port in its Via/Contact headers.
        self._sip_client._local_port = bound_port  # noqa: SLF001
        _LOGGER.debug("SIP UDP endpoint bound on %s:%d", bound_host, bound_port)

    async def async_stop(self) -> None:
        """Tear down audio bridge and SIP socket. Idempotent."""
        await self._bridge.stop()
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._sip_client = None
        self._active_call_id = None

    async def start_call(
        self,
        target_uri: str,
        target_host: str,
        sip_username: str,
        sip_password: str,
        target_port: int = 5060,
    ) -> str | None:
        """Send INVITE for an incoming doorbell ring. Returns Call-ID or None."""
        if self._sip_client is None or self._transport is None:
            _LOGGER.warning("CallManager not started; cannot place call")
            return None
        if self._active_call_id is not None:
            _LOGGER.info(
                "Call %s already active; ignoring overlapping ring", self._active_call_id
            )
            return None

        # Bind the RTP socket NOW so the port we advertise in SDP is exactly
        # the one ffmpeg will receive on — no probe/use race.
        rtp_socket = _bind_rtp_socket()
        rtp_port = rtp_socket.getsockname()[1]
        try:
            call_id = self._sip_client.call(
                target_uri=target_uri,
                target_host=target_host,
                target_port=target_port,
                local_rtp_port=rtp_port,
                sip_username=sip_username,
                sip_password=sip_password,
            )
        except Exception:
            rtp_socket.close()
            raise
        self._active_call_id = call_id
        self._pending_rtp_sockets[call_id] = rtp_socket
        # Defensive max-duration timer: if no BYE arrives within N seconds we
        # tear the call down ourselves. Without this a missed teardown leaves
        # `_active_call_id` set and the next ring is silently dropped.
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

    async def hang_up(self) -> None:
        """End the active call (user accepted via door open, or explicit hangup)."""
        if self._sip_client is None or self._active_call_id is None:
            return
        self._sip_client.hang_up(self._active_call_id)
        # _handle_call_terminated will fire from the protocol path on confirmation,
        # but hang_up is fire-and-forget from the caller's POV; clean up eagerly.
        await self._bridge.stop()

    # --- IntratoneSipClient callbacks (sync — must not await) -----------

    def _handle_call_established(self, info: CallEstablished) -> None:
        if info.call_id != self._active_call_id:
            _LOGGER.debug("Ignoring stale call_established for %s", info.call_id)
            return
        # ffmpeg start is async; schedule on the loop.
        asyncio.create_task(self._spawn_bridge(info))

    def _handle_call_terminated(self, call_id: str) -> None:
        if call_id != self._active_call_id:
            return
        self._active_call_id = None
        if self._max_duration_task is not None and not self._max_duration_task.done():
            self._max_duration_task.cancel()
        self._max_duration_task = None
        # If the call never reached CONFIRMED, the RTP socket was bound but
        # never handed to AudioBridge — close it here to avoid a leak.
        leftover_socket = self._pending_rtp_sockets.pop(call_id, None)
        if leftover_socket is not None:
            try:
                leftover_socket.close()
            except OSError:
                pass
        asyncio.create_task(self._teardown_bridge(call_id))

    async def _spawn_bridge(self, info: CallEstablished) -> None:
        rtp_socket = self._pending_rtp_sockets.pop(info.call_id, None)
        if rtp_socket is None:
            _LOGGER.warning(
                "No pending RTP socket for call %s — bridge cannot start",
                info.call_id,
            )
            if self._sip_client is not None:
                self._sip_client.hang_up(info.call_id)
            return
        try:
            rtsp_url = await self._bridge.start(
                rtp_socket=rtp_socket,
                remote_rtp_ip=info.remote_rtp_ip,
                remote_rtp_port=info.remote_rtp_port,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Audio bridge failed to start: %s", err)
            if self._sip_client is not None:
                self._sip_client.hang_up(info.call_id)
            return
        _LOGGER.info(
            "Audio bridge up: %s (RTP peer %s:%d, keepalive on)",
            rtsp_url,
            info.remote_rtp_ip,
            info.remote_rtp_port,
        )
        self._on_call_active(info.call_id, rtsp_url)

    async def _teardown_bridge(self, call_id: str) -> None:
        await self._bridge.stop()
        self._on_call_ended(call_id)


def _pick_rtp_port() -> int:
    """Return a free port from the RTP range by transient-binding to test it."""
    for port in _RTP_PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free RTP port in range")
