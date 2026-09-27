"""Doorbell notification blueprint — trigger semantics."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.trigger import (
    async_initialize_triggers,
    async_validate_trigger_config,
)
from homeassistant.util.yaml import load_yaml_dict

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
