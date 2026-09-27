"""Doorbell notification blueprint — trigger semantics."""

from __future__ import annotations

import asyncio
import shutil
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import voluptuous as vol
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import template
from homeassistant.helpers.trigger import (
    async_initialize_triggers,
    async_validate_trigger_config,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    async_fire_time_changed,
    async_mock_service,
    mock_integration,
    mock_platform,
)

_BLUEPRINT = (
    Path(__file__).parents[3]
    / "blueprints/automation/intratone/doorbell_notification.yaml"
)
_EVENT = "event.intratone_test_doorbell"


async def test_trigger_fires_on_ring_but_not_on_reload(hass) -> None:
    """A config-entry reload drives the event entity timestamp → unavailable
    → restored timestamp. Neither transition is a ring; only a new timestamp
    is. A false positive here is a "Someone is ringing" push — critical
    ones even ring through Do Not Disturb."""
    blueprint = Blueprint(
        load_yaml_dict(_BLUEPRINT),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    config = BlueprintInputs(
        blueprint,
        {
            "use_blueprint": {
                "path": "intratone/doorbell_notification.yaml",
                "input": {"doorbell_event": _EVENT, "notify_device": "abc"},
            }
        },
    ).async_substitute()
    triggers = await async_validate_trigger_config(
        hass, cv.TRIGGER_SCHEMA(config["triggers"])
    )

    fired: list[str] = []

    @callback
    def action(run_variables, context=None):
        fired.append(run_variables["trigger"]["to_state"].state)

    hass.states.async_set(_EVENT, "2026-09-27T10:00:00+00:00")
    await hass.async_block_till_done()
    unsub = await async_initialize_triggers(
        hass, triggers, action, "test", "test", lambda *a, **k: None
    )

    hass.states.async_set(_EVENT, "unavailable", {"restored": True})  # unload
    hass.states.async_set(_EVENT, "2026-09-27T10:00:00+00:00")  # restored
    hass.states.async_set(_EVENT, "2026-09-27T10:05:00+00:00")  # real ring
    await hass.async_block_till_done()
    unsub()

    assert fired == ["2026-09-27T10:05:00+00:00"]


# --- The whole blueprint, run by HA's real automation engine -----------------

_LOCK = "lock.intratone_door"
_CAMERA = "camera.intratone_intercom"


@pytest.fixture
async def notifications(hass, tmp_path):
    """Serve the repo blueprint from a temp config dir and stand in for the
    mobile_app device-action platform (the real one drags in HA's whole
    cloud/assist stack). The stand-in keeps mobile_app's schema and renders
    message/title/data with the run variables exactly like the real one."""
    hass.config.config_dir = str(tmp_path)
    dest = tmp_path / "blueprints/automation/intratone"
    dest.mkdir(parents=True)
    shutil.copy(_BLUEPRINT, dest / _BLUEPRINT.name)

    sent: list[dict] = []

    async def async_call_action_from_config(hass, config, variables, context):
        sent.append(
            {
                key: template.render_complex(config[key], variables)
                for key in ("message", "title", "data")
                if key in config
            }
        )

    mock_integration(hass, MockModule("mobile_app"))
    mock_platform(
        hass,
        "mobile_app.device_action",
        Mock(
            ACTION_SCHEMA=cv.DEVICE_ACTION_BASE_SCHEMA.extend(
                {
                    vol.Required("type"): "notify",
                    vol.Required("message"): cv.template,
                    vol.Optional("title"): cv.template,
                    vol.Optional("data"): cv.template_complex,
                }
            ),
            async_call_action_from_config=async_call_action_from_config,
            spec=["ACTION_SCHEMA", "async_call_action_from_config"],
        ),
    )
    phone_entry = MockConfigEntry(domain="mobile_app")
    phone_entry.add_to_hass(hass)
    phone = dr.async_get(hass).async_get_or_create(
        config_entry_id=phone_entry.entry_id, identifiers={("mobile_app", "phone")}
    )
    hass.states.async_set(_EVENT, "2026-09-27T10:00:00+00:00")
    return phone.id, sent


async def _setup_automation(hass, phone_id: str, **inputs) -> None:
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": "intratone/doorbell_notification.yaml",
                    "input": {
                        "doorbell_event": _EVENT,
                        "notify_device": phone_id,
                        **inputs,
                    },
                }
            }
        },
    )
    await hass.async_block_till_done()
    # An invalid generated automation only logs an error — fail loudly here
    # so negative assertions below can't pass against a dead automation.
    automation = hass.states.get("automation.automation_0")
    assert automation is not None and automation.state == "on", automation


async def _wait_for(predicate) -> None:
    # Not hass.async_block_till_done(): it would wait out the blueprint's
    # 120 s "Open door" wait_for_trigger.
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def _waiting_for_button(hass) -> bool:
    return hass.bus.async_listeners().get("mobile_app_notification_action", 0) > 0


async def _ring(hass, sent: list[dict]) -> None:
    hass.states.async_set(_EVENT, "2026-09-27T10:05:00+00:00", {"door_name": "PORTE RUE"})
    await _wait_for(lambda: sent)


async def _press(hass, action: str) -> None:
    hass.bus.async_fire("mobile_app_notification_action", {"action": action})
    await asyncio.sleep(0.05)


async def test_minimal_notification(hass, notifications) -> None:
    """Every optional input left empty — the blueprint defaults."""
    phone_id, sent = notifications
    await _setup_automation(hass, phone_id)

    await _ring(hass, sent)

    assert sent == [
        {"message": "Someone is ringing at PORTE RUE", "title": "Intratone", "data": {}}
    ]
    assert not _waiting_for_button(hass)


async def test_camera_and_critical_notification(hass, notifications) -> None:
    phone_id, sent = notifications
    await _setup_automation(hass, phone_id, camera_entity=_CAMERA, critical=True)

    await _ring(hass, sent)

    assert sent[0]["data"] == {
        "clickAction": f"entityId:{_CAMERA}",
        "url": f"entityId:{_CAMERA}",
        "push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}},
        "priority": "high",
        "ttl": 0,
        "channel": "alarm_stream",
    }


async def test_open_door_button_unlocks_the_chosen_lock(hass, notifications) -> None:
    phone_id, sent = notifications
    unlocks = async_mock_service(hass, "lock", "unlock")
    await _setup_automation(hass, phone_id, door_lock=_LOCK)

    await _ring(hass, sent)
    action = "INTRATONE_UNLOCK_LOCK_INTRATONE_DOOR"
    assert sent[0]["data"] == {"actions": [{"action": action, "title": "Open door"}]}
    await _wait_for(lambda: _waiting_for_button(hass))

    # Another blueprint instance's button must not open this door.
    await _press(hass, "INTRATONE_UNLOCK_LOCK_OTHER")
    assert unlocks == []

    await _press(hass, action)
    await _wait_for(lambda: unlocks)
    assert unlocks[0].data["entity_id"] in (_LOCK, [_LOCK])


async def test_open_door_button_expires(hass, notifications) -> None:
    phone_id, sent = notifications
    unlocks = async_mock_service(hass, "lock", "unlock")
    await _setup_automation(hass, phone_id, door_lock=_LOCK)

    await _ring(hass, sent)
    await _wait_for(lambda: _waiting_for_button(hass))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=121))
    await _wait_for(lambda: not _waiting_for_button(hass))
    await _press(hass, "INTRATONE_UNLOCK_LOCK_INTRATONE_DOOR")

    assert unlocks == []


async def test_reload_sends_nothing(hass, notifications) -> None:
    phone_id, sent = notifications
    await _setup_automation(hass, phone_id, critical=True)

    hass.states.async_set(_EVENT, "unavailable", {"restored": True})
    hass.states.async_set(_EVENT, "2026-09-27T10:00:00+00:00")
    await hass.async_block_till_done()

    assert sent == []
