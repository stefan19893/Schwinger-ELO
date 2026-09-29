"""Rate-limited, retrying HTTP client with an on-disk cache (spec §4.1).

Politeness rules enforced here, so callers cannot forget them:

* a random 0.5-1.0 s delay (configurable) between *network* requests,
* the project User-Agent on every request,
* retries with exponential backoff (``tenacity``) on transport errors,
  HTTP 429 and 5xx,
* every successful response is cached under ``cache_dir`` and served from
  there on later calls; the network is only hit again with ``refresh=True``
  or when a caller passes ``max_age`` and the cached copy is older.

Cache layout: ``<cache_dir>/<host>/<sha[:2]>/<sha>.body`` + ``<sha>.meta.json``
where ``sha`` is the SHA-256 of the full request URL (query included).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)
from tenacity.wait import wait_base

log = logging.getLogger("schwingen.client")

QueryParams = Mapping[str, str] | Sequence[tuple[str, str]] | None

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class FetchError(RuntimeError):
    """Non-retryable HTTP error (e.g. 404) or retries exhausted."""

    def __init__(self, url: str, status: int | None, message: str) -> None:
        super().__init__(f"{message} ({url})")
        self.url = url
        self.status = status


class CacheMiss(RuntimeError):
    """Raised in offline mode when a URL is not in the cache."""


class _RetryableStatus(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content: bytes
    fetched_at: str  # ISO-8601 UTC
    from_cache: bool

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.content)


@dataclass
class ClientStats:
    network_requests: int = 0  # every attempt that went over the wire
    cache_hits: int = 0
    retries: int = 0


def build_url(url: str, params: QueryParams = None) -> str:
    """Canonical URL string (what is requested and what the cache is keyed on)."""
    return str(httpx.URL(url, params=params)) if params else str(httpx.URL(url))


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (_RetryableStatus, httpx.TransportError))


class HttpClient:
    def __init__(
        self,
        cache_dir: Path,
        user_agent: str,
        *,
        delay_min: float = 0.5,
        delay_max: float = 1.0,
        timeout: float = 30.0,
        max_retries: int = 4,
        refresh: bool = False,
        offline: bool = False,
        transport: httpx.BaseTransport | None = None,
        retry_wait: wait_base | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        if not 0 < delay_min <= delay_max:
            raise ValueError("require 0 < delay_min <= delay_max")
        self.cache_dir = Path(cache_dir)
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_retries = max(1, max_retries)
        self.refresh = refresh
        self.offline = offline
        self.stats = ClientStats()
        self._retry_wait = retry_wait or wait_exponential_jitter(initial=2, max=60)
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._last_request: float | None = None
        self._http = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ cache
    def cache_paths(self, url: str) -> tuple[Path, Path]:
        u = httpx.URL(url)
        sha = hashlib.sha256(url.encode("utf-8")).hexdigest()
        base = self.cache_dir / (u.host or "_nohost") / sha[:2]
        return base / f"{sha}.body", base / f"{sha}.meta.json"

    def _read_cache(self, url: str) -> FetchResult | None:
        body, meta = self.cache_paths(url)
        if not (body.is_file() and meta.is_file()):
            return None
        info = json.loads(meta.read_text(encoding="utf-8"))
        return FetchResult(url=url, status=int(info["status"]), content=body.read_bytes(),
                           fetched_at=info["fetched_at"], from_cache=True)

    def _write_cache(self, res: FetchResult, content_type: str | None) -> None:
        body, meta = self.cache_paths(res.url)
        body.parent.mkdir(parents=True, exist_ok=True)
        info = {"url": res.url, "status": res.status, "fetched_at": res.fetched_at,
                "content_type": content_type}
        # Atomic writes: an interrupted crawl never leaves a half-written entry.
        for path, data in ((body, res.content),
                           (meta, json.dumps(info, ensure_ascii=False, indent=1).encode())):
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)

    @staticmethod
    def _age_seconds(fetched_at: str) -> float:
        then = _dt.datetime.fromisoformat(fetched_at)
        return (_dt.datetime.now(_dt.timezone.utc) - then).total_seconds()

    # ------------------------------------------------------------ network
    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self._rng.uniform(self.delay_min, self.delay_max)
            elapsed = self._clock() - self._last_request
            if elapsed < wait:
                self._sleep(wait - elapsed)

    def _attempt(self, url: str) -> httpx.Response:
        self._throttle()
        self.stats.network_requests += 1
        try:
            resp = self._http.get(url)
        finally:
            self._last_request = self._clock()
        if resp.status_code in RETRY_STATUS:
            log.warning("HTTP %d for %s - will retry", resp.status_code, url)
            raise _RetryableStatus(resp.status_code)
        return resp

    def _fetch_network(self, url: str) -> httpx.Response:
        retrying = Retrying(
            stop=stop_after_attempt(self.max_retries),
            wait=self._retry_wait,
            retry=retry_if_exception(_is_retryable),
            sleep=self._sleep,
            before_sleep=lambda rs: setattr(self.stats, "retries", self.stats.retries + 1),
            reraise=True,
        )
        try:
            return retrying(self._attempt, url)
        except _RetryableStatus as exc:
            raise FetchError(url, exc.status,
                             f"HTTP {exc.status} after {self.max_retries} attempts") from exc
        except httpx.TransportError as exc:
            raise FetchError(url, None,
                             f"{type(exc).__name__} after {self.max_retries} attempts") from exc

    # ------------------------------------------------------------ public
    def get(self, url: str, params: QueryParams = None, *,
            max_age: float | None = None) -> FetchResult:
        """GET ``url`` (+ ``params``), from cache unless refresh/stale.

        ``max_age`` (seconds): treat a cached copy older than this as stale.
        Only non-2xx responses raise :class:`FetchError`; they are not cached.
        """
        full = build_url(url, params)
        if not full.startswith(("http://", "https://")):
            raise ValueError(f"not an http(s) URL: {full}")
        cached = None if self.refresh else self._read_cache(full)
        if cached is not None and (max_age is None
                                   or self._age_seconds(cached.fetched_at) <= max_age):
            self.stats.cache_hits += 1
            return cached
        if self.offline:
            raise CacheMiss(f"offline mode and not cached: {full}")
        resp = self._fetch_network(full)
        if not resp.is_success:
            raise FetchError(full, resp.status_code, f"HTTP {resp.status_code}")
        res = FetchResult(
            url=full, status=resp.status_code, content=resp.content,
            fetched_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            from_cache=False,
        )
        self._write_cache(res, resp.headers.get("content-type"))
        log.debug("fetched %s (%d bytes)", full, len(res.content))
        return res


def client_from_config(cfg: Any, *, transport: httpx.BaseTransport | None = None,
                       offline: bool | None = None) -> HttpClient:
    """Build an :class:`HttpClient` from a :class:`src.config.Config`."""
    return HttpClient(
        cfg.raw_dir,
        cfg.user_agent,
        delay_min=cfg.request_delay_min,
        delay_max=cfg.request_delay_max,
        timeout=cfg.request_timeout,
        max_retries=cfg.max_retries,
        refresh=cfg.refresh,
        offline=cfg.sample if offline is None else offline,
        transport=transport,
    )
