"""Download the "Schlussrangliste" PDFs (athlete identity evidence, Phase 3).

The final ranking list prints, per athlete, rank, points, the result string
(``-+++++``), name, residence, Schwingklub and Kranz status — the only
schlussgang source with a club per festival. Downloads go through
:class:`HttpClient` (politeness, retries, cache in ``data/raw/``) with the same
cache policy as the statistic PDFs (:func:`pdf_cache_policy`): past seasons are
cached forever, current-season lists are re-checked after ``max_age_hours``
until a copy was fetched ``grace_days`` after the festival.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from collections.abc import Iterable, Mapping

from src.db import Festival
from src.scraper.client import CacheMiss, FetchError, FetchResult, HttpClient
from src.scraper.statistic_pdfs import DownloadReport, PdfLimitExceeded, pdf_cache_policy

log = logging.getLogger("schwingen.ranking")

# Own request cap per crawl run (env SCHWINGEN_RANKING_PDF_MAX_REQUESTS).
RANKING_PDF_MAX_REQUESTS = 2500
# Download order: high-K tiers first so a capped / interrupted run covers them.
TIER_ORDER = {"ESAF": 0, "Bergkranz": 1, "Teilverband": 2, "Kantonal": 3, "Gauverband": 3,
              "Regional": 4}


def ranking_max_requests(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = env.get("SCHWINGEN_RANKING_PDF_MAX_REQUESTS")
    if raw is None:
        return RANKING_PDF_MAX_REQUESTS
    value = int(raw)
    if value < 0:
        raise ValueError("SCHWINGEN_RANKING_PDF_MAX_REQUESTS must be >= 0")
    return value


def festivals_with_ranking(festivals: Iterable[Festival]) -> list[Festival]:
    """Active, non-cancelled festivals with a ranking PDF, by tier then date.

    A URL shared by several festivals is downloaded once (first festival wins)."""
    seen: set[str] = set()
    out: list[Festival] = []
    for f in sorted(festivals, key=lambda f: (TIER_ORDER.get(f.category or "", 9),
                                              f.date, f.fest_id)):
        if f.kind == "active" and not f.cancelled and f.ranking_pdf_url \
                and f.ranking_pdf_url not in seen:
            seen.add(f.ranking_pdf_url)
            out.append(f)
    return out


def fetch_ranking_pdf(client: HttpClient, fest: Festival, today: _dt.date,
                      max_age_hours: float = 24.0, grace_days: int = 14) -> FetchResult:
    if not fest.ranking_pdf_url:
        raise ValueError(f"festival {fest.fest_id} has no ranking PDF")
    max_age, final_after = pdf_cache_policy(fest.date, today, max_age_hours, grace_days)
    return client.get(fest.ranking_pdf_url, max_age=max_age, final_after=final_after)


def download_ranking_pdfs(client: HttpClient, festivals: Iterable[Festival], *,
                          today: _dt.date, max_requests: int | None,
                          max_age_hours: float = 24.0, grace_days: int = 14,
                          max_consecutive_errors: int = 10) -> DownloadReport:
    """Fetch (or confirm cached) ranking PDFs; errors are recorded, not raised.

    Raises :class:`PdfLimitExceeded` at the request cap and ``RuntimeError``
    after ``max_consecutive_errors`` failures in a row (blocked?)."""
    rep = DownloadReport()
    start = client.stats.network_requests
    streak = 0
    for f in festivals:
        if not f.ranking_pdf_url:
            continue
        if max_requests is not None and client.stats.network_requests - start >= max_requests:
            raise PdfLimitExceeded(f"ranking PDF request cap {max_requests} reached")
        try:
            res = fetch_ranking_pdf(client, f, today, max_age_hours, grace_days)
        except CacheMiss:
            rep.missing.append(f.fest_id)
            continue
        except FetchError as exc:
            rep.failed.append((f.fest_id, str(exc)))
            if exc.status is not None:
                rep.status_counts[exc.status] = rep.status_counts.get(exc.status, 0) + 1
            streak += 1
            log.warning("ranking pdf %d: %s", f.fest_id, exc)
            if streak >= max_consecutive_errors:
                raise RuntimeError(f"{streak} consecutive ranking PDF errors - stopping "
                                   f"(blocked?): {exc}") from exc
            continue
        streak = 0
        if res.from_cache:
            rep.cached += 1
        else:
            rep.fetched += 1
    return rep
