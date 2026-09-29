"""Tests for the rate-limited, cached HTTP client. Offline: httpx.MockTransport."""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from tenacity import wait_none

from src.scraper.client import CacheMiss, FetchError, HttpClient, build_url

UA = "Schwinger-ELO/test (+https://example.invalid)"
URL = "https://api.example.ch/jsonapi/node/event"


class FakeTime:
    """Deterministic clock + sleep: sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response],
                ft: FakeTime | None = None, **kw: object) -> HttpClient:
    ft = ft or FakeTime()
    return HttpClient(
        tmp_path / "raw", UA,
        transport=httpx.MockTransport(handler),
        retry_wait=wait_none(),
        sleep=ft.sleep, clock=ft.clock, rng=random.Random(42),
        **kw,  # type: ignore[arg-type]
    )


def ok_handler(calls: list[httpx.Request], body: bytes = b'{"data": []}'
               ) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})
    return handler


def test_caches_and_never_refetches(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    with make_client(tmp_path, ok_handler(calls)) as c:
        r1 = c.get(URL, params=[("page[limit]", "50")])
        r2 = c.get(URL, params=[("page[limit]", "50")])
    assert len(calls) == 1
    assert not r1.from_cache and r2.from_cache
    assert r1.json() == r2.json() == {"data": []}
    assert c.stats.network_requests == 1 and c.stats.cache_hits == 1
    body, meta = c.cache_paths(r1.url)
    assert body.is_file() and meta.is_file()
    assert body.is_relative_to(tmp_path / "raw" / "api.example.ch")
    info = json.loads(meta.read_text())
    assert info["url"] == r1.url and info["status"] == 200


def test_cache_survives_new_client_instance(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    with make_client(tmp_path, ok_handler(calls)) as c:
        c.get(URL)
    with make_client(tmp_path, ok_handler(calls)) as c2:
        assert c2.get(URL).from_cache
    assert len(calls) == 1


def test_sends_user_agent(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    with make_client(tmp_path, ok_handler(calls)) as c:
        c.get(URL)
    assert calls[0].headers["user-agent"] == UA


def test_refresh_refetches_and_overwrites(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    with make_client(tmp_path, ok_handler(calls, b"old")) as c:
        c.get(URL)
    with make_client(tmp_path, ok_handler(calls, b"new"), refresh=True) as c:
        assert c.get(URL).content == b"new"
    with make_client(tmp_path, ok_handler(calls, b"never")) as c:
        assert c.get(URL).content == b"new"  # refreshed copy is cached
    assert len(calls) == 2


def test_max_age_refetches_stale_entry(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    with make_client(tmp_path, ok_handler(calls)) as c:
        c.get(URL)
        assert c.get(URL, max_age=3600).from_cache
        _, meta = c.cache_paths(build_url(URL))
        info = json.loads(meta.read_text())
        info["fetched_at"] = "2020-01-01T00:00:00+00:00"
        meta.write_text(json.dumps(info))
        assert c.get(URL).from_cache  # no max_age -> cached forever
        assert not c.get(URL, max_age=3600).from_cache
    assert len(calls) == 2


def test_rate_limit_between_network_requests(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    ft = FakeTime()
    with make_client(tmp_path, ok_handler(calls), ft=ft) as c:
        c.get(URL + "?a=1")
        c.get(URL + "?a=1")  # cache hit: no delay
        c.get(URL + "?a=2")
        c.get(URL + "?a=3")
    assert len(calls) == 3
    assert len(ft.sleeps) == 2  # none before the first request
    assert all(0.5 <= s <= 1.0 for s in ft.sleeps)


def test_rate_limit_counts_elapsed_time(tmp_path: Path) -> None:
    ft = FakeTime()
    with make_client(tmp_path, ok_handler([]), ft=ft) as c:
        c.get(URL + "?a=1")
        ft.now += 5.0  # caller was busy for longer than the max delay
        c.get(URL + "?a=2")
    assert ft.sleeps == []


def test_retries_on_503_then_succeeds(tmp_path: Path) -> None:
    statuses = iter([503, 429, 200])

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), content=b"ok")

    ft = FakeTime()
    with make_client(tmp_path, handler, ft=ft) as c:
        res = c.get(URL)
    assert res.content == b"ok"
    assert c.stats.network_requests == 3 and c.stats.retries == 2
    throttle = [s for s in ft.sleeps if s > 0]  # wait_none() backoff sleeps 0 s
    assert len(throttle) == 2 and all(0.5 <= s <= 1.0 for s in throttle)  # retries throttled


def test_retries_transport_errors(tmp_path: Path) -> None:
    attempts = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, content=b"ok")

    with make_client(tmp_path, handler) as c:
        assert c.get(URL).content == b"ok"
    assert attempts["n"] == 2


def test_gives_up_after_max_retries_and_does_not_cache(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(500)

    with make_client(tmp_path, handler, max_retries=3) as c:
        with pytest.raises(FetchError) as ei:
            c.get(URL)
    assert ei.value.status == 500
    assert len(calls) == 3
    body, _ = c.cache_paths(build_url(URL))
    assert not body.exists()


def test_404_is_not_retried_or_cached(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(404)

    with make_client(tmp_path, handler) as c:
        with pytest.raises(FetchError) as ei:
            c.get(URL)
    assert ei.value.status == 404 and len(calls) == 1
    assert not (tmp_path / "raw").exists() or not any((tmp_path / "raw").rglob("*.body"))


def test_offline_mode_uses_cache_only(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    with make_client(tmp_path, ok_handler(calls)) as c:
        c.get(URL)
    with make_client(tmp_path, ok_handler(calls), offline=True) as c:
        assert c.get(URL).from_cache
        with pytest.raises(CacheMiss):
            c.get(URL + "?other=1")
    assert len(calls) == 1


def test_params_and_inline_query_share_cache_key(tmp_path: Path) -> None:
    assert build_url(URL, [("a", "1"), ("b", "x y")]) == build_url(URL + "?a=1&b=x+y")


def test_rejects_non_http_urls(tmp_path: Path) -> None:
    with make_client(tmp_path, ok_handler([])) as c:
        with pytest.raises(ValueError):
            c.get("file:///etc/passwd")


def test_invalid_delays_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        HttpClient(tmp_path, UA, delay_min=1.0, delay_max=0.5)


def test_real_network_is_blocked_in_tests(tmp_path: Path) -> None:
    """conftest.py safety net: a client without MockTransport cannot go online."""
    with HttpClient(tmp_path, UA, retry_wait=wait_none(), sleep=lambda s: None) as c:
        with pytest.raises(RuntimeError, match="forbidden"):
            c.get("https://backend-api.schlussgang.ch/jsonapi/node/event")
