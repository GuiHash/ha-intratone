"""Test bootstrap — make pytest-homeassistant-custom-component happy and
make our custom_component discoverable as `custom_components.intratone`.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

# Expose the repo's `custom_components/` to imports.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest_plugins = ["pytest_homeassistant_custom_component"]


# ---------------------------------------------------------------------------
# aiohttp 3.14 compatibility shim for aioresponses
#
# aiohttp 3.14 added a required `stream_writer` kwarg to
# ClientResponse.__init__ (alongside the existing `writer`) and now reads
# `stream_writer.output_size` when `writer is None`.  aioresponses 0.7.9
# only passes `writer=None`, so construction fails with TypeError (missing
# stream_writer) / AttributeError (None has no output_size).  When the
# installed aiohttp has `stream_writer`, wrap the response class aioresponses
# instantiates so it supplies a stub stream_writer.
# ---------------------------------------------------------------------------
import aioresponses.core as _arc
from aiohttp import ClientResponse as _ClientResponse

if "stream_writer" in inspect.signature(_ClientResponse.__init__).parameters:
    _OrigClientResponse = _arc.ClientResponse

    class _StubStreamWriter:
        output_size = 0

    class _CompatClientResponse(_OrigClientResponse):
        def __init__(self, *args, **kwargs):
            if kwargs.get("stream_writer") is None:
                kwargs["stream_writer"] = _StubStreamWriter()
            super().__init__(*args, **kwargs)

    _arc.ClientResponse = _CompatClientResponse


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Force HA to discover custom_components/ in every test."""
    yield
