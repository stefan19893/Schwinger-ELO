"""Discovers all festivals 2011-present from schlussgang.ch (spec §4.1, Milestone 1).

Source: the Drupal JSON:API behind schlussgang.ch/resultate
(``backend-api.schlussgang.ch/jsonapi/node/event``). One query per
(schlussgang category, year), following ``links.next`` for paging. esv.ch is
never contacted; only the ESV Anlass id delivered by schlussgang is stored.

Only listing requests are made here. The per-festival "Statistische Tabelle"
PDFs (bout source) are only *located* (URL stored) and fetched in Phase 2.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from src.db import Festival, UpsertStats, upsert_festivals
from src.scraper.client import HttpClient

log = logging.getLogger("schwingen.crawl")

SITE_URL = "https://www.schlussgang.ch"
API_URL = "https://backend-api.schlussgang.ch/jsonapi/node/event"
PAGE_LIMIT = 50  # Drupal JSON:API maximum
MAX_PAGES_PER_QUERY = 40

# schlussgang event_tags tid -> tag name. Crawled categories only; 10
# (Jungschwingen) and 16 (Frauenschwingen) are deliberately not requested.
SOURCE_CATEGORIES: dict[int, str] = {
    11: "Eidgenössische Anlässe",
    12: "Bergkranzfest",
    13: "Teilverbandsfest",
    14: "Kantonal-/Gaufest",
    15: "Regionalfeste",
}
EXCLUDED_SOURCE_CATEGORIES: dict[int, str] = {10: "Jungschwingen", 16: "Frauenschwingen"}

_SIMPLE_MAP = {12: "Bergkranz", 13: "Teilverband", 15: "Regional"}

# tid 11: only the ESAF itself is "ESAF"; Kilchberg, Unspunnen and other
# eidgenössische competitions get Bergkranz-level K (spec §4.2.3).
ESAF_RE = re.compile(r"eidgen(ö|oe)ssisches\s+schwing", re.I)
# tid 14: the Bernese Gauverbands-Schwingfeste; everything else is cantonal.
GAU_RE = re.compile(
    r"mittelländisch|oberländisch|seeländisch|bern-jurassisch|oberaargauisch|emmentalisch",
    re.I)
KANTONAL_RE = re.compile(r"kantonal|cantonal", re.I)

YOUTH_RE = re.compile(
    r"jungschwing|nachwuchs|buebe|buben|knaben|\bjung\.?(?=\s|$)", re.I)
WOMEN_RE = re.compile(r"frauen|damen|schwingerinnen", re.I)
NON_COMPETITION_RE = re.compile(
    r"abgeordnetenversammlung|delegiertenversammlung|fussballturnier|nacht des schwingsports"
    r"|prämierung|brunch|veteranentagung|goldene[rn]?\s+kranz|jahresversammlung", re.I)
CANCELLED_RE = re.compile(r"abgebrochen|abgesagt|annulliert", re.I)
_CANCEL_MARKER_RE = re.compile(r"\s*!+\s*(abgebrochen|abgesagt)\s*!+\s*", re.I)

# Statistic PDF (per-Gang opponent + grade) inside the legacy field_event_pdf list.
_STAT_DESC_RE = re.compile(r"^\s*(schluss)?statisti(k|sche tabelle)\s*$", re.I)
_RANK_DESC_RE = re.compile(r"^\s*schlussrangliste\s*$", re.I)

EVENT_FIELDS = ",".join([
    "title", "path", "drupal_internal__nid", "field_title_custom", "field_event_esv_id",
    "field_event_date", "field_event_type", "field_event_state", "field_event_location",
    "field_event_participant_count", "field_category", "field_event_association",
    "field_event_pdf", "field_final_statistic_pdf", "field_final_ranking_pdf",
])
INCLUDES = ("field_category,field_event_association,field_event_pdf,"
            "field_final_statistic_pdf,field_final_ranking_pdf")


def listing_params(tid: int, year: int) -> list[tuple[str, str]]:
    """Query for all events of one schlussgang category in one calendar year."""
    return [
        ("filter[field_category.tid]", str(tid)),
        ("filter[date][condition][path]", "field_event_date"),
        ("filter[date][condition][operator]", "BETWEEN"),
        ("filter[date][condition][value][0]", f"{year}-01-01"),
        ("filter[date][condition][value][1]", f"{year}-12-31"),
        ("include", INCLUDES),
        ("fields[node--event]", EVENT_FIELDS),
        ("fields[file--file]", "filename,uri"),
        ("fields[taxonomy_term--event_tags]", "name,drupal_internal__tid"),
        ("fields[taxonomy_term--association]", "name,drupal_internal__tid"),
        ("sort", "field_event_date,drupal_internal__nid"),
        ("page[limit]", str(PAGE_LIMIT)),
    ]


# --------------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class Skipped:
    fest_id: int | None
    name: str
    reason: str


@dataclass
class ParseResult:
    festivals: list[Festival] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


def classify_kind(name: str, event_type: str | None) -> str:
    """active / youth / women / non_competition (type field wins, name as fallback)."""
    et = (event_type or "").strip().lower()
    if et == "aktivschwinger":
        return "women" if WOMEN_RE.search(name) else "active"
    if et.startswith("jungschwinger"):
        return "youth"
    if et.startswith(("allgmeiner", "allgemeiner")):
        return "non_competition"
    if WOMEN_RE.search(name):
        return "women"
    if YOUTH_RE.search(name):
        return "youth"
    if NON_COMPETITION_RE.search(name):
        return "non_competition"
    return "active"


def map_category(tid: int, name: str) -> str | None:
    """Map a schlussgang category tid (+ festival name) to a spec category."""
    if tid in _SIMPLE_MAP:
        return _SIMPLE_MAP[tid]
    if tid == 11:
        return "ESAF" if ESAF_RE.search(name) else "Bergkranz"
    if tid == 14:
        if GAU_RE.search(name) and not KANTONAL_RE.search(name):
            return "Gauverband"
        return "Kantonal"
    return None


def _included_index(doc: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(i["type"], i["id"]): i for i in doc.get("included") or []}


def _rel(node: dict[str, Any], name: str) -> Any:
    return ((node.get("relationships") or {}).get(name) or {}).get("data")


def _file_url(inc: dict[tuple[str, str], dict[str, Any]], ref: dict[str, Any] | None
              ) -> str | None:
    if not ref:
        return None
    f = inc.get((ref["type"], ref["id"]))
    url = (((f or {}).get("attributes") or {}).get("uri") or {}).get("url")
    return urljoin(SITE_URL, url) if url else None


def _pick_legacy_pdf(inc: dict[tuple[str, str], dict[str, Any]], refs: list[dict[str, Any]],
                     desc_re: re.Pattern[str]) -> str | None:
    for ref in refs or []:
        if desc_re.match((ref.get("meta") or {}).get("description") or ""):
            url = _file_url(inc, ref)
            if url:
                return url
    return None


def _to_int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_event(node: dict[str, Any], inc: dict[tuple[str, str], dict[str, Any]]
                ) -> Festival | Skipped:
    a = node.get("attributes") or {}
    nid = _to_int(a.get("drupal_internal__nid"))
    raw_name = (a.get("field_title_custom") or a.get("title") or "").strip()
    if nid is None:
        return Skipped(None, raw_name, "missing node id")
    if not raw_name:
        return Skipped(nid, "", "missing name")
    date = a.get("field_event_date")
    try:
        _dt.date.fromisoformat(date or "")
    except ValueError:
        return Skipped(nid, raw_name, f"invalid date {date!r}")

    cat_ref = _rel(node, "field_category")
    cat_term = inc.get((cat_ref["type"], cat_ref["id"])) if cat_ref else None
    tid = _to_int(((cat_term or {}).get("attributes") or {}).get("drupal_internal__tid"))
    if tid is None and cat_ref:
        tid = _to_int((cat_ref.get("meta") or {}).get("drupal_internal__target_id"))
    if tid is None:
        return Skipped(nid, raw_name, "missing category")
    source_category = ((cat_term or {}).get("attributes") or {}).get("name") \
        or SOURCE_CATEGORIES.get(tid) or EXCLUDED_SOURCE_CATEGORIES.get(tid)

    name = _CANCEL_MARKER_RE.sub(" ", raw_name).strip()
    event_type = a.get("field_event_type")
    kind = "women" if tid == 16 else "youth" if tid == 10 else classify_kind(name, event_type)
    category = map_category(tid, name) if kind == "active" else None
    if kind == "active" and category is None:
        return Skipped(nid, raw_name, f"unmapped category tid {tid}")
    state = (a.get("field_event_state") or "").lower()
    cancelled = bool(CANCELLED_RE.search(raw_name)) or state in {"cancelled", "canceled"}

    assoc_ref = _rel(node, "field_event_association")
    assoc = inc.get((assoc_ref["type"], assoc_ref["id"])) if assoc_ref else None
    path = (a.get("path") or {}).get("alias")
    legacy = _rel(node, "field_event_pdf") or []
    stat = (_file_url(inc, _rel(node, "field_final_statistic_pdf"))
            or _pick_legacy_pdf(inc, legacy, _STAT_DESC_RE))
    rank = (_file_url(inc, _rel(node, "field_final_ranking_pdf"))
            or _pick_legacy_pdf(inc, legacy, _RANK_DESC_RE))
    location = (a.get("field_event_location") or "").strip() or None

    return Festival(
        fest_id=nid, name=name, date=date, category=category, location=location,
        kind=kind, cancelled=cancelled,
        source_category=source_category, source_category_tid=tid,
        association=((assoc or {}).get("attributes") or {}).get("name"),
        esv_id=_to_int(a.get("field_event_esv_id")),
        event_type=event_type, participant_count=_to_int(a.get("field_event_participant_count")),
        url=urljoin(SITE_URL, path) if path else f"{SITE_URL}/node/{nid}",
        statistic_pdf_url=stat, ranking_pdf_url=rank,
    )


def parse_listing(doc: dict[str, Any]) -> ParseResult:
    """Parse one JSON:API listing page into festivals (+ skipped entries)."""
    inc = _included_index(doc)
    out = ParseResult()
    for node in doc.get("data") or []:
        if node.get("type") != "node--event":
            out.skipped.append(Skipped(None, str(node.get("type")), "not an event node"))
            continue
        res = parse_event(node, inc)
        (out.skipped if isinstance(res, Skipped) else out.festivals).append(res)
    return out


# --------------------------------------------------------------------------- crawling
class CrawlLimitExceeded(RuntimeError):
    pass


@dataclass
class CrawlReport:
    queries: int = 0
    pages: int = 0
    festivals: dict[int, Festival] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)
    future: int = 0
    upsert: UpsertStats = field(default_factory=UpsertStats)

    def counts(self) -> Counter[tuple[int, str]]:
        """(year, category or kind) -> number of stored festivals."""
        return Counter((f.year, f.category if f.kind == "active" else f"[{f.kind}]")
                       for f in self.festivals.values())


def iter_pages(client: HttpClient, tid: int, year: int, *, max_age: float | None,
               max_requests: int | None = None) -> Iterator[dict[str, Any]]:
    url: str | None = API_URL
    params: list[tuple[str, str]] | None = listing_params(tid, year)
    seen: set[str] = set()
    for _ in range(MAX_PAGES_PER_QUERY):
        if url is None:
            return
        if max_requests is not None and client.stats.network_requests >= max_requests:
            raise CrawlLimitExceeded(f"request cap {max_requests} reached")
        res = client.get(url, params, max_age=max_age)
        if res.url in seen:
            log.warning("paging loop at %s - stopping", res.url)
            return
        seen.add(res.url)
        doc = res.json()
        yield doc
        url = ((doc.get("links") or {}).get("next") or {}).get("href")
        params = None
    log.warning("more than %d pages for tid=%d year=%d - truncated", MAX_PAGES_PER_QUERY, tid, year)


def crawl_festivals(
    client: HttpClient,
    conn: sqlite3.Connection,
    from_year: int,
    to_year: int,
    *,
    today: _dt.date | None = None,
    current_max_age: float | None = 24 * 3600,
    categories: Iterable[int] = tuple(SOURCE_CATEGORIES),
    max_requests: int | None = None,
) -> CrawlReport:
    """Crawl listings for ``from_year..to_year`` and upsert into ``festivals``.

    Past seasons' listing pages are served from cache forever; pages for the
    current season (year >= today's year) are re-fetched once older than
    ``current_max_age`` so newly held festivals are discovered. Festivals dated
    after ``today`` are counted but not stored (no results yet).
    """
    today = today or _dt.date.today()
    report = CrawlReport()
    for year in range(from_year, to_year + 1):
        max_age = current_max_age if year >= today.year else None
        for tid in categories:
            report.queries += 1
            for doc in iter_pages(client, tid, year, max_age=max_age,
                                  max_requests=max_requests):
                report.pages += 1
                parsed = parse_listing(doc)
                for s in parsed.skipped:
                    log.warning("skipped event %s %r: %s", s.fest_id, s.name, s.reason)
                report.skipped.extend(parsed.skipped)
                for f in parsed.festivals:
                    if _dt.date.fromisoformat(f.date) > today:
                        report.future += 1
                        continue
                    if f.fest_id in report.festivals:
                        log.debug("duplicate event %d in listings", f.fest_id)
                    report.festivals[f.fest_id] = f
        log.info("crawl %d: %d festivals so far, %d network requests, %d cache hits",
                 year, len(report.festivals), client.stats.network_requests,
                 client.stats.cache_hits)
    report.upsert = upsert_festivals(conn, report.festivals.values())
    return report


def load_listing_files(docs: Iterable[dict[str, Any]]) -> ParseResult:
    """Parse already-loaded listing documents (used by ``--sample``)."""
    out = ParseResult()
    for doc in docs:
        r = parse_listing(doc)
        out.festivals.extend(r.festivals)
        out.skipped.extend(r.skipped)
    return out
