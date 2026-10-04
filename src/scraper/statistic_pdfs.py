"""Download and text-extract the "Statistische Tabelle" PDFs (bout source).

Downloads go through :class:`HttpClient` (politeness, retries, cache in
``data/raw/``). Cache policy (spec §4.1 + Phase 2 decision):

* festivals of past seasons: cached forever;
* current-season festivals: a cached PDF is re-checked when older than
  ``max_age`` until one copy was fetched ``grace_days`` after the festival
  date (late corrections), then it is final as well.
"""

from __future__ import annotations

import datetime as _dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

import pypdfium2 as pdfium

from src.db import Festival
from src.scraper.client import Blocked, CacheMiss, FetchError, FetchResult, HttpClient

log = logging.getLogger("schwingen.pdfs")

PAGE_BREAK = "\f"


def pdf_cache_policy(fest_date: str, today: _dt.date, max_age_hours: float,
                     grace_days: int) -> tuple[float | None, _dt.datetime | None]:
    """(max_age seconds, final_after) for :meth:`HttpClient.get`."""
    d = _dt.date.fromisoformat(fest_date)
    if d.year < today.year:
        return None, None
    final_after = _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc) \
        + _dt.timedelta(days=grace_days)
    return max_age_hours * 3600, final_after


def pdf_to_text(content: bytes) -> str:
    """Plain text of all pages (PDFium), pages separated by form feeds, ``\n`` newlines."""
    pdf = pdfium.PdfDocument(content)
    try:
        pages = []
        for page in pdf:
            textpage = page.get_textpage()
            pages.append(textpage.get_text_range().replace("\r\n", "\n").replace("\r", "\n"))
            textpage.close()
            page.close()
        return PAGE_BREAK.join(pages)
    finally:
        pdf.close()


def fetch_statistic_pdf(client: HttpClient, fest: Festival, today: _dt.date,
                        max_age_hours: float = 24.0, grace_days: int = 14) -> FetchResult:
    if not fest.statistic_pdf_url:
        raise ValueError(f"festival {fest.fest_id} has no statistic PDF")
    max_age, final_after = pdf_cache_policy(fest.date, today, max_age_hours, grace_days)
    return client.get(fest.statistic_pdf_url, max_age=max_age, final_after=final_after)


class PdfLimitExceeded(RuntimeError):
    pass


@dataclass
class DownloadReport:
    fetched: int = 0          # served from network
    cached: int = 0           # served from cache
    missing: list[int] = field(default_factory=list)            # offline cache misses
    failed: list[tuple[int, str]] = field(default_factory=list)  # (fest_id, error)
    status_counts: dict[int, int] = field(default_factory=dict)  # HTTP error statuses


def download_statistic_pdfs(client: HttpClient, festivals: Iterable[Festival], *,
                            today: _dt.date, max_requests: int | None,
                            max_age_hours: float = 24.0, grace_days: int = 14,
                            max_consecutive_errors: int = 10) -> DownloadReport:
    """Fetch (or confirm cached) statistic PDFs; errors are recorded, not raised.

    Aborts with :class:`PdfLimitExceeded` when the network request cap is hit
    and with ``RuntimeError`` after ``max_consecutive_errors`` failures in a row
    (looks like being blocked -> stop and let a human look).
    """
    rep = DownloadReport()
    start = client.stats.network_requests
    streak = 0
    for f in festivals:
        if not f.statistic_pdf_url:
            continue
        if max_requests is not None and client.stats.network_requests - start >= max_requests:
            raise PdfLimitExceeded(f"PDF request cap {max_requests} reached")
        try:
            res = fetch_statistic_pdf(client, f, today, max_age_hours, grace_days)
        except CacheMiss:
            rep.missing.append(f.fest_id)
            continue
        except Blocked:
            raise  # refused (backfill: 403 / 429): the caller ends the run
        except FetchError as exc:
            rep.failed.append((f.fest_id, str(exc)))
            if exc.status is not None:
                rep.status_counts[exc.status] = rep.status_counts.get(exc.status, 0) + 1
            streak += 1
            log.warning("pdf %d: %s", f.fest_id, exc)
            if streak >= max_consecutive_errors:
                raise RuntimeError(f"{streak} consecutive PDF download errors - stopping "
                                   f"(blocked?): {exc}") from exc
            continue
        streak = 0
        if res.from_cache:
            rep.cached += 1
        else:
            rep.fetched += 1
    return rep
