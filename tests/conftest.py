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
# aiohttp 3.14+ compatibility shim for aioresponses
#
# aiohttp 3.14 renamed ClientResponse.__init__'s `writer` kwarg to
# `stream_writer` (now required).  aioresponses 0.7.9 still passes
# `writer=None`, which raises TypeError.  Detect the kwarg name at import
# time and patch _build_response only when the rename is present.
# ---------------------------------------------------------------------------
import aioresponses.core as _arc
from aiohttp import ClientResponse as _ClientResponse

_cr_params = set(inspect.signature(_ClientResponse.__init__).parameters)
if "stream_writer" in _cr_params and "writer" not in _cr_params:
    _orig_build = _arc.RequestMatch._build_response

    def _build_response_stream_writer(  # type: ignore[override]
        self,
        url,
        method="GET",
        request_headers=None,
        status=200,
        body="",
        content_type="application/json",
        payload=None,
        headers=None,
        response_class=None,
        reason=None,
    ):
        _rc = response_class if response_class is not None else _ClientResponse

        def _compat(m, u, *, writer=None, **kw):
            return _rc(m, u, stream_writer=writer, **kw)

        return _orig_build(
            self, url, method, request_headers, status, body,
            content_type, payload, headers, _compat, reason,
        )

    _arc.RequestMatch._build_response = _build_response_stream_writer


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Force HA to discover custom_components/ in every test."""
    yield
