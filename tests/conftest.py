"""Test-wide safety net: tests must never hit the network (CLAUDE.md).

Two layers, both autouse:

* any real httpx transport I/O raises (tests use ``httpx.MockTransport``);
* ``socket.socket.connect`` / ``connect_ex`` raise for every non-loopback
  address, so no other client library can go online either. Loopback and
  Unix sockets stay allowed (local ``serve`` / port-in-use tests).
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import httpx
import pytest

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_local(sock: socket.socket, address: Any) -> bool:
    if getattr(socket, "AF_UNIX", None) is not None and sock.family == socket.AF_UNIX:
        return True
    host = address[0] if isinstance(address, tuple) and address else address
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False  # hostnames other than localhost would need DNS -> not local


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if not _is_local(self, address):
        raise RuntimeError(f"network access in tests is forbidden: {address!r}")
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if not _is_local(self, address):
        raise RuntimeError(f"network access in tests is forbidden: {address!r}")
    return _real_connect_ex(self, address)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"network access in tests is forbidden: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
