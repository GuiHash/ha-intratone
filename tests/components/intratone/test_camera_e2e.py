"""Camera end-to-end through HA core, the way the frontend drives it.

Real config-entry setup (video on) → frontend websocket commands → HA core's
camera plumbing → HA's *real* go2rtc WebRTC provider (only its HTTP/WS
clients are faked) or the HLS path → the integration's lazy SIP dial.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioresponses import aioresponses
from homeassistant.components.camera import async_register_webrtc_provider
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.intratone.const import API_BASE, CONF_VIDEO_ENABLED, DOMAIN

_RELAY_URL = "rtsp://127.0.0.1:8554/intratone"
_SIP_CALL_ID = "fake-call-id"
_OFFER = "v=0\r\n"


def _push(call_id: str) -> dict:
    return {
        "call_id": call_id,
        "message": "PORTE RUE",
        "LOGIN_TO_CALL": "U",
        "LOGIN": "u",
        "PASS": "p",
        "ip_adress": "1.2.3.4",
    }


@pytest.fixture
async def camera(hass, mock_entry, mock_fcm_client, mock_call_manager):
    """Load the entry with video on; the fake call manager brings the audio
    bridge up shortly after each INVITE, like production does."""
    cm = mock_call_manager.instance
    cm.relay_rtsp_url = _RELAY_URL

    async def start_call(**_kwargs) -> str:
        coordinator = mock_entry.runtime_data.coordinator
        hass.loop.call_later(0.01, coordinator.set_stream_url, _SIP_CALL_ID, _RELAY_URL)
        return _SIP_CALL_ID

    cm.start_call = AsyncMock(side_effect=start_call)

    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry, options={CONF_VIDEO_ENABLED: True}
    )
    # passthrough: the test websocket client talks to HA over 127.0.0.1.
    with aioresponses(passthrough=["http://127.0.0.1"]) as api:
        api.post(
            f"{API_BASE}api/auth/device",
            payload={"state": "ok", "data": {"jwt": "fake.jwt.token", "id": "1"}},
            repeat=True,
        )
        for call_id in ("1", "2"):
            api.post(
                f"{API_BASE}api/calls/{call_id}/answer",
                payload={"error": 0, "state": "ok"},
                repeat=True,
            )
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        entity_id = er.async_get(hass).async_get_entity_id(
            "camera", DOMAIN, f"{mock_entry.entry_id}_camera"
        )
        assert entity_id
        yield entity_id, mock_entry.runtime_data.coordinator, cm


def _fake_rest_client() -> MagicMock:
    rest = MagicMock()
    rest.schemes.list = AsyncMock(return_value={"rtsp", "rtsps", "rtmp", "http"})
    rest.streams.list = AsyncMock(return_value={})
    rest.streams.add = AsyncMock()
    rest.preload.list = AsyncMock(return_value=set())
    return rest


async def _register_go2rtc(hass, rest: MagicMock) -> None:
    """Register HA core's real go2rtc WebRTC provider the way the go2rtc
    integration does, across the constructor changes CI's HA versions span."""
    from homeassistant.components import go2rtc as go2rtc_mod

    url = "http://127.0.0.1:1984/"
    with patch.object(go2rtc_mod, "Go2RtcRestClient", return_value=rest):
        if "rest_client" in inspect.signature(go2rtc_mod.WebRTCProvider).parameters:
            provider = go2rtc_mod.WebRTCProvider(
                hass, url, async_get_clientsession(hass), rest
            )
        else:  # HA 2024.12: the provider builds its own REST client
            provider = go2rtc_mod.WebRTCProvider(hass, url)
    if hasattr(provider, "initialize"):  # newer HA: schemes come from go2rtc
        await provider.initialize()
    async_register_webrtc_provider(hass, provider)
    await hass.async_block_till_done()


@pytest.fixture
async def go2rtc(hass):
    """HA's go2rtc provider loaded before any ring. Only the go2rtc HTTP/WS
    clients are faked."""
    rest = _fake_rest_client()
    ws = MagicMock(send=AsyncMock(), close=AsyncMock())
    with patch("homeassistant.components.go2rtc.Go2RtcWsClient", return_value=ws):
        await _register_go2rtc(hass, rest)
        yield rest, ws


def _registered_source(rest) -> str:
    """First producer HA's go2rtc registered for the camera stream."""
    sources = rest.streams.add.call_args.args[1]
    return sources[0] if isinstance(sources, list) else sources


async def _webrtc_offer(client, entity_id: str) -> str:
    """Send a frontend WebRTC offer; return the type of the first
    non-session event (the provider handed the offer on, or an error)."""
    await client.send_json_auto_id(
        {"type": "camera/webrtc/offer", "entity_id": entity_id, "offer": _OFFER}
    )
    assert (await client.receive_json())["success"]
    while True:
        event = (await client.receive_json())["event"]
        if event["type"] != "session":
            return event["type"]


async def test_frontend_gets_webrtc_capability_at_idle(
    hass, hass_ws_client, camera, go2rtc
) -> None:
    """With HA's go2rtc loaded and no call, the frontend is told the camera
    speaks WebRTC — and nothing was dialed to find that out."""
    entity_id, _coordinator, cm = camera
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": "camera/capabilities", "entity_id": entity_id}
    )
    msg = await client.receive_json()

    assert set(msg["result"]["frontend_stream_types"]) == {"hls", "web_rtc"}
    cm.start_call.assert_not_called()


async def test_webrtc_tap_during_ring_answers_and_registers_bare_url(
    hass, hass_ws_client, camera, go2rtc
) -> None:
    entity_id, coordinator, cm = camera
    rest, ws = go2rtc
    client = await hass_ws_client(hass)
    await coordinator.async_handle_push(_push("1"))

    # No event yet: the offer was forwarded to go2rtc, which answers later.
    await client.send_json_auto_id(
        {"type": "camera/webrtc/offer", "entity_id": entity_id, "offer": _OFFER}
    )
    assert (await client.receive_json())["success"]
    assert (await client.receive_json())["event"]["type"] == "session"
    # The ws handler waits for the audio bridge before reaching go2rtc.
    async with asyncio.timeout(5):
        while not ws.send.await_count:
            await asyncio.sleep(0.01)

    cm.start_call.assert_awaited_once()
    assert _registered_source(rest) == _RELAY_URL
    ws.send.assert_awaited_once()
    assert ws.send.call_args.args[0].sdp == _OFFER


async def test_webrtc_tap_at_idle_errors_without_dialing(
    hass, hass_ws_client, camera, go2rtc
) -> None:
    entity_id, _coordinator, cm = camera
    rest, ws = go2rtc
    client = await hass_ws_client(hass)

    assert await _webrtc_offer(client, entity_id) == "error"

    cm.start_call.assert_not_called()
    rest.streams.add.assert_not_called()
    ws.send.assert_not_called()


async def test_go2rtc_loading_mid_ring_does_not_answer(hass, camera) -> None:
    """HA's go2rtc entry (re)loading while the intercom rings triggers a
    provider refresh — it must not pick up the call."""
    _entity_id, coordinator, cm = camera
    await coordinator.async_handle_push(_push("1"))

    # Registering the provider only now = go2rtc (re)loaded mid-ring.
    await _register_go2rtc(hass, _fake_rest_client())

    cm.start_call.assert_not_called()
    assert not coordinator._pending.started


async def test_hls_tap_answers_every_ring(hass, hass_ws_client, camera) -> None:
    """Without a WebRTC provider the frontend falls back to HLS
    (`camera/stream`). Each ring's tap must dial, not just the first."""
    entity_id, coordinator, cm = camera
    client = await hass_ws_client(hass)
    streams = []

    def fake_create_stream(hass_, source, **_kwargs):
        stream = MagicMock(start=AsyncMock(), stop=AsyncMock())
        stream.endpoint_url.return_value = f"/api/hls/{len(streams)}/master.m3u8"
        stream.source = source
        streams.append(stream)
        return stream

    with patch(
        "homeassistant.components.camera.create_stream",
        side_effect=fake_create_stream,
    ):
        for n, call_id in enumerate(("1", "2"), start=1):
            await coordinator.async_handle_push(_push(call_id))
            await client.send_json_auto_id(
                {"type": "camera/stream", "entity_id": entity_id, "format": "hls"}
            )
            msg = await client.receive_json()
            assert msg["success"], msg
            assert cm.start_call.await_count == n
            assert streams[-1].source == _RELAY_URL
            coordinator.set_stream_url(_SIP_CALL_ID, None)  # call ends
            await hass.async_block_till_done()

    assert len(streams) == 2
    streams[0].stop.assert_awaited_once()
