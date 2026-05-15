"""Bluetooth stub config flow — never triggers, but HA imports it."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow


class BluetoothStubConfigFlow(ConfigFlow, domain="bluetooth"):
    """No-op config flow for the macOS-dev bluetooth stub."""

    VERSION = 1
