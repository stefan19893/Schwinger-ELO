"""Test-wide safety net: tests must never hit the network (CLAUDE.md).

Any real httpx transport I/O raises; tests use ``httpx.MockTransport`` instead.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"network access in tests is forbidden: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
