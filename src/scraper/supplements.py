"""Interim statistic sheets merged into a festival's final sheet.

Some final "Statistik" sheets omit athletes eliminated at an early cut (ESAF
2013: the Gang-8 sheet lists only athletes still in the competition after
Gang 4). The festival node also links interim sheets ("Statistik nach 4
Gängen") that list them. They are registered here explicitly (user decision
2026-10-01: one festival, exactly the needed download), downloaded by
``crawl`` like the final sheets and merged by ``parse``
(:func:`src.scraper.bouts_parser.merge_interim_sheet`).
"""

from __future__ import annotations

import datetime as _dt

from src.db import Festival
from src.scraper.client import FetchResult, HttpClient
from src.scraper.statistic_pdfs import pdf_cache_policy

_FILES = "https://www.schlussgang.ch/sites/default/files/public%3A//schlussrangliste-pdfs/"

# fest_id -> URL of the interim statistic sheet to merge
INTERIM_SHEETS: dict[int, str] = {
    # ESAF Burgdorf 2013: "Statistik nach 4 Gängen" (final sheet lacks 77 athletes)
    25862: _FILES + "stat_burgdorf13-gang4_0.pdf.pdf",
}


def fetch_interim_sheet(client: HttpClient, fest: Festival, today: _dt.date,
                        max_age_hours: float = 24.0, grace_days: int = 14) -> FetchResult | None:
    """The registered interim sheet of ``fest`` (cache first), or None if there is none."""
    url = INTERIM_SHEETS.get(fest.fest_id)
    if url is None:
        return None
    max_age, final_after = pdf_cache_policy(fest.date, today, max_age_hours, grace_days)
    return client.get(url, max_age=max_age, final_after=final_after)
