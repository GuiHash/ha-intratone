"""Supervisor behaviour of the FCM listener: backoff, healthcheck, shutdown."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from firebase_messaging.fcmpushclient import FcmPushClientRunState

from custom_components.intratone import fcm_listener
from custom_components.intratone.fcm_listener import FcmListener


@pytest.fixture
def listener(hass) -> FcmListener:
    """A bare FcmListener wired to mocks (no config entry setup needed)."""
    entry = MagicMock()
    entry.entry_id = "test-entry-id"
    coordinator = MagicMock()
    coordinator.async_handle_push = AsyncMock()
    store = MagicMock()
    store.fcm_creds = {"gcm": {"android_id": 1, "security_token": 2}}
    store.fcm_token = "fake-fcm-token"
    store.async_update = AsyncMock()
    return FcmListener(hass, entry, coordinator, store)


@pytest.fixture
def fake_client():
    """Patch FcmPushClient with a mock exposing a real-ish run_state.

    Unlike conftest's mock_fcm_client, `is_started()` here follows
    `run_state` so tests can walk the client through its lifecycle.
    """
    client = MagicMock()
    client.checkin_or_register = AsyncMock(return_value="fake-fcm-token")
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.run_state = FcmPushClientRunState.STARTING_TASKS
    client.is_started = lambda: client.run_state is FcmPushClientRunState.STARTED
    with patch("firebase_messaging.FcmPushClient", return_value=client) as cls:
        cls.instance = client
        yield cls


def _logged_backoffs(caplog) -> list[float]:
    """Extract the backoff delays from the supervisor's crash warnings."""
    return [
        record.args[1]
        for record in caplog.records
        if "FCM listener crashed" in record.getMessage()
    ]


async def test_supervisor_restarts_with_exponential_backoff(
    listener, monkeypatch, caplog
) -> None:
    """Each consecutive crash doubles the reconnect delay, capped at max."""
    monkeypatch.setattr(fcm_listener, "BACKOFF_INITIAL_S", 0.01)
    monkeypatch.setattr(fcm_listener, "BACKOFF_MAX_S", 0.04)
    attempts = 0

    async def crash() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 4:
            listener._stopping = True
        raise RuntimeError("boom")

    monkeypatch.setattr(listener, "_run_once", crash)
    await listener._supervisor()

    assert attempts == 4
    assert _logged_backoffs(caplog) == [0.01, 0.02, 0.04, 0.04]


async def test_backoff_resets_after_healthy_run(
    listener, monkeypatch, caplog
) -> None:
    """A crash after a long healthy run restarts backoff from the initial delay."""
    monkeypatch.setattr(fcm_listener, "BACKOFF_INITIAL_S", 0.01)
    monkeypatch.setattr(fcm_listener, "BACKOFF_MAX_S", 0.04)
    monkeypatch.setattr(fcm_listener, "HEALTHY_RUN_S", 0.05, raising=False)
    attempts = 0

    async def run() -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise RuntimeError("crash loop")
        if attempts == 4:
            # Healthy for longer than HEALTHY_RUN_S, then dies (the daily
            # silent self-termination scenario).
            await asyncio.sleep(0.1)
            raise RuntimeError("late crash")
        listener._stopping = True
        raise RuntimeError("final crash")

    monkeypatch.setattr(listener, "_run_once", run)
    await listener._supervisor()

    # Grows over the crash loop, resets to initial after the healthy run,
    # then grows again.
    assert _logged_backoffs(caplog) == [0.01, 0.02, 0.04, 0.01, 0.02]


async def test_healthcheck_detects_silent_client_death(
    hass, listener, fake_client, monkeypatch
) -> None:
    """The client stopping itself (no exception raised) must surface as a
    crash so the supervisor reconnects instead of sleeping forever."""
    monkeypatch.setattr(fcm_listener, "HEALTHCHECK_INTERVAL_S", 0.01)
    monkeypatch.setattr(
        fcm_listener, "STARTUP_POLL_INTERVAL_S", 0.01, raising=False
    )
    monkeypatch.setattr(
        fcm_listener, "STARTUP_POLL_TIMEOUT_S", 0.02, raising=False
    )
    fake_client.instance.run_state = FcmPushClientRunState.STOPPING

    with pytest.raises(RuntimeError, match="stopped itself"):
        await listener._run_once()

    assert listener.connected is False


async def test_connected_only_reported_once_client_started(
    hass, listener, fake_client, monkeypatch
) -> None:
    """start() only spawns tasks (STARTING_TASKS); the connectivity flag must
    not go up until the client actually reaches STARTED."""
    monkeypatch.setattr(fcm_listener, "HEALTHCHECK_INTERVAL_S", 0.01)
    monkeypatch.setattr(
        fcm_listener, "STARTUP_POLL_INTERVAL_S", 0.01, raising=False
    )
    task = hass.async_create_background_task(
        listener._run_once(), name="test_fcm_run_once"
    )

    await asyncio.sleep(0.03)
    # Login has not completed — bad credentials would look exactly like this.
    assert listener.connected is False

    fake_client.instance.run_state = FcmPushClientRunState.STARTED
    await asyncio.sleep(0.03)
    assert listener.connected is True

    # Silent self-termination flips it back off.
    fake_client.instance.run_state = FcmPushClientRunState.STOPPING
    with pytest.raises(RuntimeError, match="stopped itself"):
        await task
    assert listener.connected is False


async def test_connected_goes_true_promptly_after_login(
    hass, listener, fake_client, monkeypatch
) -> None:
    """The flag must flip on as soon as the client reaches STARTED — not one
    30s healthcheck later, which would show a false 'disconnected' window on
    every daily reconnect and trip user automations watching the sensor."""
    monkeypatch.setattr(
        fcm_listener, "STARTUP_POLL_INTERVAL_S", 0.01, raising=False
    )
    # HEALTHCHECK_INTERVAL_S deliberately left at its real 30s value: the
    # prompt flip must not depend on the healthcheck loop.
    task = hass.async_create_background_task(
        listener._run_once(), name="test_fcm_run_once"
    )
    await asyncio.sleep(0.02)
    assert listener.connected is False

    fake_client.instance.run_state = FcmPushClientRunState.STARTED
    await asyncio.sleep(0.05)
    assert listener.connected is True

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_connected_stays_false_when_client_never_starts(
    hass, listener, fake_client, monkeypatch
) -> None:
    """Bad credentials: the client never reaches STARTED, so the startup poll
    times out and the flag never shows a false positive."""
    monkeypatch.setattr(
        fcm_listener, "STARTUP_POLL_INTERVAL_S", 0.01, raising=False
    )
    monkeypatch.setattr(
        fcm_listener, "STARTUP_POLL_TIMEOUT_S", 0.03, raising=False
    )
    monkeypatch.setattr(fcm_listener, "HEALTHCHECK_INTERVAL_S", 0.01)
    task = hass.async_create_background_task(
        listener._run_once(), name="test_fcm_run_once"
    )
    # Well past the startup poll timeout and several healthchecks.
    await asyncio.sleep(0.1)
    assert listener.connected is False

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_async_stop_is_clean(
    hass, listener, fake_client, monkeypatch
) -> None:
    """async_stop halts the client and supervisor: no restarts, no callbacks."""
    monkeypatch.setattr(fcm_listener, "HEALTHCHECK_INTERVAL_S", 0.01)
    fake_client.instance.run_state = FcmPushClientRunState.STARTED
    states: list[bool] = []
    listener.add_state_listener(states.append)

    await listener.async_start()
    await asyncio.sleep(0.05)
    assert listener.connected is True

    await listener.async_stop()
    assert listener.connected is False
    assert listener._task is None
    fake_client.instance.stop.assert_awaited()
    assert states == [True, False]

    # Nothing restarts and no state callbacks fire after shutdown.
    client_constructions = fake_client.call_count
    await asyncio.sleep(0.05)
    assert fake_client.call_count == client_constructions == 1
    assert states == [True, False]
