"""Bluetooth stub for macOS dev (avoids TCC SIGKILL on Core Bluetooth access)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

CONFIG_SCHEMA = cv.config_entry_only_config_schema("bluetooth")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    return True
