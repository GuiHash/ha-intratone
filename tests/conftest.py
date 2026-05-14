"""Test bootstrap — make pytest-homeassistant-custom-component happy and
make our custom_component discoverable as `custom_components.intratone`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Expose the repo's `custom_components/` to imports.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Force HA to discover custom_components/ in every test."""
    yield
