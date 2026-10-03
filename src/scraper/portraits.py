"""schlussgang.ch athlete portraits: identity anchors for Phase 3.

Two JSON:API sources (both fetched through :class:`HttpClient`, cached in ``data/raw``):

* ``/jsonapi/node/portrait`` — every portrait (≈ 10,500 incl. youth and retired
  athletes): slug (``/portraet/<slug>``), first/last name, birthday, city, ESV
  licence number (``hknr``) plus club / Teilverband / cantonal association terms
  (included in the same response). ~215 pages of 50.
* ``/jsonapi/node/event`` with ``fields[node--event]=field_ref_portrait`` — the
  festival → portrait participation links. schlussgang only fills
  ``field_ref_portrait`` from the 2023 season on (empty on every older event
  checked), so earlier festivals can be matched to portraits only by name /
  club evidence, not by an authoritative link.

``crawl_portraits`` only downloads (network step of ``crawl``);
``load_portraits`` rebuilds the ``portraits`` / ``portrait_appearances`` tables
from the cache (offline step of ``parse``); ``link_appearances`` maps each
appearance to the ``athletes_raw`` row of the festival by name.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.pipeline.names import clean_raw_name, name_key, name_similarity
from src.scraper.client import CacheMiss, FetchError, HttpClient
from src.scraper.fests_crawler import (API_URL, MAX_PAGES_PER_QUERY, PAGE_LIMIT, SITE_URL,
                                       SOURCE_CATEGORIES, listing_final_after)

log = logging.getLogger("schwingen.portraits")

PORTRAIT_API_URL = "https://backend-api.schlussgang.ch/jsonapi/node/portrait"
# field_ref_portrait is empty on all events before this season (checked 2011-2022).
PORTRAIT_LINK_FIRST_YEAR = 2023
# Portrait list pages are re-fetched when older than this (new athletes, club changes).
PORTRAIT_MAX_AGE_DAYS = 30
# Own request cap per crawl run (env SCHWINGEN_PORTRAIT_MAX_REQUESTS).
PORTRAIT_MAX_REQUESTS = 600
MAX_PORTRAIT_PAGES = 400

PORTRAIT_FIELDS = ",".join([
    "drupal_internal__nid", "title", "path", "field_portrait_first_name",
    "field_portrait_last_name", "field_portrait_birthday", "field_portrait_city",
    "field_portrait_hknr", "field_portait_end_of_career", "field_portrait_club",
    "field_portrait_association", "field_portrait_cant_association",
    "field_portrait_activity",
])
PORTRAIT_INCLUDES = ("field_portrait_club,field_portrait_association,"
                     "field_portrait_cant_association,field_portrait_activity")


class PortraitLimitExceeded(RuntimeError):
    pass


def portrait_max_requests(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = env.get("SCHWINGEN_PORTRAIT_MAX_REQUESTS")
    value = PORTRAIT_MAX_REQUESTS if raw is None else int(raw)
    if value < 0:
        raise ValueError("SCHWINGEN_PORTRAIT_MAX_REQUESTS must be >= 0")
    return value


def portrait_list_params() -> list[tuple[str, str]]:
    return [
        ("include", PORTRAIT_INCLUDES),
        ("fields[node--portrait]", PORTRAIT_FIELDS),
        ("fields[taxonomy_term--club]", "name,drupal_internal__tid,field_tax_esv_id"),
        ("fields[taxonomy_term--association]", "name,drupal_internal__tid"),
        ("fields[taxonomy_term--canton_association]", "name,drupal_internal__tid"),
        ("fields[taxonomy_term--portrait_activity]", "name"),
        ("sort", "drupal_internal__nid"),
        ("page[limit]", str(PAGE_LIMIT)),
    ]


def event_portrait_params(tid: int, year: int) -> list[tuple[str, str]]:
    return [
        ("filter[field_category.tid]", str(tid)),
        ("filter[date][condition][path]", "field_event_date"),
        ("filter[date][condition][operator]", "BETWEEN"),
        ("filter[date][condition][value][0]", f"{year}-01-01"),
        ("filter[date][condition][value][1]", f"{year}-12-31"),
        ("fields[node--event]", "drupal_internal__nid,field_ref_portrait"),
        ("sort", "field_event_date,drupal_internal__nid"),
        ("page[limit]", str(PAGE_LIMIT)),
    ]


# --------------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class Portrait:
    portrait_id: int
    slug: str | None
    url: str | None
    title: str | None
    last_name: str | None
    first_name: str | None
    name_key: str
    birthday: str | None
    city: str | None
    hknr: int | None
    club_tid: int | None
    club_name: str | None
    club_esv_id: int | None
    association_name: str | None
    canton_association: str | None
    activity: str | None
    end_of_career: str | None


def _to_int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _term(inc: dict[tuple[str, str], dict[str, Any]], node: dict[str, Any],
          rel: str) -> tuple[dict[str, Any], int | None]:
    ref = ((node.get("relationships") or {}).get(rel) or {}).get("data")
    if not ref:
        return {}, None
    tid = _to_int((ref.get("meta") or {}).get("drupal_internal__target_id"))
    term = inc.get((ref["type"], ref["id"])) or {}
    return term.get("attributes") or {}, tid


def parse_portrait_page(doc: dict[str, Any]) -> list[Portrait]:
    inc = {(i["type"], i["id"]): i for i in doc.get("included") or []}
    out: list[Portrait] = []
    for node in doc.get("data") or []:
        a = node.get("attributes") or {}
        nid = _to_int(a.get("drupal_internal__nid"))
        if nid is None:
            continue
        alias = (a.get("path") or {}).get("alias") or ""
        slug = alias.rsplit("/", 1)[-1] if alias.startswith("/portraet/") else None
        last = (a.get("field_portrait_last_name") or "").strip() or None
        first = (a.get("field_portrait_first_name") or "").strip() or None
        display = " ".join(x for x in (last, first) if x) or (a.get("title") or "").split(",")[0]
        club, club_tid = _term(inc, node, "field_portrait_club")
        assoc, _ = _term(inc, node, "field_portrait_association")
        cant, _ = _term(inc, node, "field_portrait_cant_association")
        act, _ = _term(inc, node, "field_portrait_activity")
        out.append(Portrait(
            portrait_id=nid, slug=slug, url=f"{SITE_URL}{alias}" if alias else None,
            title=a.get("title"), last_name=last, first_name=first,
            name_key=name_key(display), birthday=a.get("field_portrait_birthday"),
            city=(a.get("field_portrait_city") or "").strip() or None,
            hknr=_to_int(a.get("field_portrait_hknr")), club_tid=club_tid,
            club_name=club.get("name"), club_esv_id=_to_int(club.get("field_tax_esv_id")),
            association_name=assoc.get("name"), canton_association=cant.get("name"),
            activity=act.get("name"),
            end_of_career=(a.get("field_portait_end_of_career") or "").strip() or None))
    return out


def parse_event_portraits(doc: dict[str, Any]) -> list[tuple[int, int]]:
    """(fest_id, portrait_id) pairs from an event listing page."""
    out: list[tuple[int, int]] = []
    for node in doc.get("data") or []:
        nid = _to_int((node.get("attributes") or {}).get("drupal_internal__nid"))
        refs = ((node.get("relationships") or {}).get("field_ref_portrait") or {}).get("data")
        if nid is None or not refs:
            continue
        for ref in refs:
            pid = _to_int((ref.get("meta") or {}).get("drupal_internal__target_id"))
            if pid is not None:
                out.append((nid, pid))
    return out


# --------------------------------------------------------------------------- fetching
def _pages(client: HttpClient, url: str, params: list[tuple[str, str]], *, max_pages: int,
           max_age: float | None, final_after: _dt.datetime | None,
           budget: _Budget | None) -> Iterator[dict[str, Any]]:
    next_url: str | None = url
    next_params: list[tuple[str, str]] | None = params
    seen: set[str] = set()
    for _ in range(max_pages):
        if next_url is None:
            return
        if budget is not None:
            budget.check(client)
        res = client.get(next_url, next_params, max_age=max_age, final_after=final_after)
        if res.url in seen:
            log.warning("paging loop at %s - stopping", res.url)
            return
        seen.add(res.url)
        doc = res.json()
        yield doc
        next_url = ((doc.get("links") or {}).get("next") or {}).get("href")
        next_params = None
    log.warning("more than %d pages for %s - truncated", max_pages, url)


@dataclass
class _Budget:
    start: int
    cap: int | None

    def check(self, client: HttpClient) -> None:
        if self.cap is not None and client.stats.network_requests - self.start >= self.cap:
            raise PortraitLimitExceeded(f"portrait request cap {self.cap} reached")


def iter_portrait_pages(client: HttpClient, *, max_age_days: float = PORTRAIT_MAX_AGE_DAYS,
                        budget: _Budget | None = None) -> Iterator[dict[str, Any]]:
    return _pages(client, PORTRAIT_API_URL, portrait_list_params(),
                  max_pages=MAX_PORTRAIT_PAGES, max_age=max_age_days * 86400,
                  final_after=None, budget=budget)


def iter_event_portrait_pages(client: HttpClient, tid: int, year: int, *,
                              max_age: float | None = 24 * 3600, grace_days: int = 60,
                              budget: _Budget | None = None) -> Iterator[dict[str, Any]]:
    return _pages(client, API_URL, event_portrait_params(tid, year),
                  max_pages=MAX_PAGES_PER_QUERY, max_age=max_age,
                  final_after=listing_final_after(year, grace_days), budget=budget)


@dataclass
class PortraitReport:
    portraits: int = 0
    pages: int = 0
    appearances: int = 0
    event_queries: int = 0
    cache_misses: list[str] = field(default_factory=list)
    linked: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)


def link_years(to_year: int) -> range:
    return range(PORTRAIT_LINK_FIRST_YEAR, to_year + 1)


def crawl_portraits(client: HttpClient, *, to_year: int, max_requests: int | None,
                    listing_max_age: float | None = 24 * 3600, grace_days: int = 60,
                    max_age_days: float = PORTRAIT_MAX_AGE_DAYS) -> PortraitReport:
    """Download (or confirm cached) all portrait pages and the 2023+ event→portrait
    listings. Raises :class:`PortraitLimitExceeded` at the request cap."""
    rep = PortraitReport()
    budget = _Budget(client.stats.network_requests, max_requests)
    try:
        for doc in iter_portrait_pages(client, max_age_days=max_age_days, budget=budget):
            rep.pages += 1
            rep.portraits += len(doc.get("data") or [])
    except CacheMiss as exc:
        rep.cache_misses.append(f"portraits: {exc}")
    for year in link_years(to_year):
        for tid in SOURCE_CATEGORIES:
            rep.event_queries += 1
            try:
                for doc in iter_event_portrait_pages(client, tid, year, max_age=listing_max_age,
                                                     grace_days=grace_days, budget=budget):
                    rep.appearances += len(parse_event_portraits(doc))
            except CacheMiss:
                rep.cache_misses.append(f"event portraits tid={tid} year={year}")
    return rep


# --------------------------------------------------------------------------- loading
_PORTRAIT_COLS = ["portrait_id", "slug", "url", "title", "last_name", "first_name", "name_key",
                  "birthday", "city", "hknr", "club_tid", "club_name", "club_esv_id",
                  "association_name", "canton_association", "activity", "end_of_career"]


def load_portraits(conn: sqlite3.Connection, client: HttpClient, *, to_year: int,
                   now: str | None = None) -> PortraitReport:
    """Rebuild ``portraits`` and ``portrait_appearances`` from the cache (offline
    client recommended) and link the appearances to ``athletes_raw``."""
    rep = PortraitReport()
    portraits: dict[int, Portrait] = {}
    try:
        for doc in iter_portrait_pages(client, max_age_days=10**6):
            rep.pages += 1
            for p in parse_portrait_page(doc):
                portraits[p.portrait_id] = p
    except (CacheMiss, FetchError) as exc:
        rep.cache_misses.append(f"portraits: {exc}")
    pairs: set[tuple[int, int]] = set()
    for year in link_years(to_year):
        for tid in SOURCE_CATEGORIES:
            rep.event_queries += 1
            try:
                for doc in iter_event_portrait_pages(client, tid, year, max_age=None):
                    pairs.update(parse_event_portraits(doc))
            except (CacheMiss, FetchError):
                rep.cache_misses.append(f"event portraits tid={tid} year={year}")
    if rep.cache_misses and not portraits:
        log.warning("portraits: nothing cached - run `crawl` first")
        return rep
    return store_portraits(conn, portraits, pairs, rep, now=now)


def load_portraits_from_dir(conn: sqlite3.Connection, directory: Path, *,
                            now: str | None = None) -> PortraitReport:
    """Offline variant for ``--sample``: JSON:API documents saved as
    ``portraits_*.json`` (portrait list pages) and ``event_portraits_*.json``."""
    rep = PortraitReport()
    portraits: dict[int, Portrait] = {}
    for path in sorted(directory.glob("portraits_*.json")):
        rep.pages += 1
        for p in parse_portrait_page(json.loads(path.read_text(encoding="utf-8"))):
            portraits[p.portrait_id] = p
    pairs: set[tuple[int, int]] = set()
    for path in sorted(directory.glob("event_portraits_*.json")):
        rep.event_queries += 1
        pairs.update(parse_event_portraits(json.loads(path.read_text(encoding="utf-8"))))
    return store_portraits(conn, portraits, pairs, rep, now=now)


def store_portraits(conn: sqlite3.Connection, portraits: Mapping[int, Portrait],
                    pairs: set[tuple[int, int]], rep: PortraitReport | None = None, *,
                    now: str | None = None) -> PortraitReport:
    """Replace ``portraits`` / ``portrait_appearances`` and link the appearances.
    Appearances of unknown festivals (youth / women categories are not crawled) or
    unknown portraits are counted in ``rep.skipped``, not stored."""
    rep = rep or PortraitReport()
    now = now or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    rep.portraits = len(portraits)
    known_fests = {r[0] for r in conn.execute("SELECT fest_id FROM festivals")}
    keep = sorted(p for p in pairs if p[0] in known_fests and p[1] in portraits)
    rep.skipped["unknown_festival"] = sum(1 for p in pairs if p[0] not in known_fests)
    rep.skipped["unknown_portrait"] = sum(1 for p in pairs
                                          if p[0] in known_fests and p[1] not in portraits)
    with conn:
        conn.execute("DELETE FROM portraits")
        conn.executemany(
            f"INSERT INTO portraits ({', '.join(_PORTRAIT_COLS)}, fetched_at) "
            f"VALUES ({', '.join('?' for _ in _PORTRAIT_COLS)}, ?)",
            [[getattr(p, c) for c in _PORTRAIT_COLS] + [now] for p in portraits.values()])
        conn.execute("DELETE FROM portrait_appearances")
        conn.executemany("INSERT INTO portrait_appearances (fest_id, portrait_id) VALUES (?, ?)",
                         keep)
    rep.appearances = len(keep)
    rep.linked = link_appearances(conn)
    return rep


def link_appearances(conn: sqlite3.Connection) -> Counter[str]:
    """Map every (festival, portrait) appearance to the festival's athletes_raw row:
    identical name key (unique) → ``name``; among namesakes (two rows, or two
    portraits for one row), the one whose ranking entry shows the portrait's city /
    club → ``name_city``; else best unique name similarity ≥ 0.9 → ``name_fuzzy``.
    A row is linked to at most one portrait. Unmatched appearances stay NULL
    (``unlinked``)."""
    stats: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for raw_id, fid, name in conn.execute("SELECT athlete_raw_id, fest_id, name FROM athletes_raw"):
        stats[fid].append((raw_id, name_key(clean_raw_name(name)[0] or name)))
    rank_by_raw: dict[str, tuple[int, str | None, str | None]] = {}
    for raw_id, idx, residence, club in conn.execute(
            "SELECT athlete_raw_id, idx, residence, club_raw FROM ranking_entries "
            "WHERE athlete_raw_id IS NOT NULL"):
        rank_by_raw[raw_id] = (idx, residence, club)
    portraits = {pid: (key, city, club) for pid, key, city, club in conn.execute(
        "SELECT portrait_id, name_key, city, club_name FROM portraits")}
    apps: dict[int, list[int]] = defaultdict(list)
    for fid, pid in conn.execute("SELECT fest_id, portrait_id FROM portrait_appearances"):
        apps[fid].append(pid)
    counts: Counter[str] = Counter()
    updates: list[tuple[str | None, int | None, str | None, int, int]] = []
    for fid, pids in apps.items():
        free = dict(stats.get(fid, []))
        by_key: dict[str, list[str]] = defaultdict(list)
        for raw_id, key in free.items():
            by_key[key].append(raw_id)
        result: dict[int, tuple[str, str]] = {}
        pending: list[int] = []
        for pid in pids:
            key = portraits.get(pid, ("", None, None))[0]
            cands = by_key.get(key, [])
            if len(cands) == 1:
                result[pid] = (cands[0], "name")
            elif len(cands) > 1:
                _, city, club = portraits[pid]
                hits = [c for c in cands if c in rank_by_raw and (
                    _same_text(rank_by_raw[c][1], city) or _same_text(rank_by_raw[c][2], club))]
                if len(hits) == 1:
                    result[pid] = (hits[0], "name_city")
            else:
                pending.append(pid)
        # portraits of namesakes claiming the same row: the one whose city / club the
        # ranking entry shows keeps it, otherwise nobody does
        claims: dict[str, list[int]] = defaultdict(list)
        for pid, (raw, _) in result.items():
            claims[raw].append(pid)
        for raw, claimants in claims.items():
            if len(claimants) < 2:
                continue
            _, residence, club = rank_by_raw.get(raw, (None, None, None))
            hits = [p for p in claimants if _same_text(residence, portraits[p][1])
                    or _same_text(club, portraits[p][2])]
            for pid in claimants:
                del result[pid]
            if len(hits) == 1:
                result[hits[0]] = (raw, "name_city")
        taken = {raw for raw, _ in result.values()}
        for pid in pending:
            key = portraits.get(pid, ("", None, None))[0]
            scored = sorted(((name_similarity(key, k), r) for r, k in free.items()
                             if r not in taken), reverse=True)
            if scored and scored[0][0] >= 0.9 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
                result[pid] = (scored[0][1], "name_fuzzy")
                taken.add(scored[0][1])
        for pid in pids:
            raw, method = result.get(pid, (None, None))
            idx = rank_by_raw[raw][0] if raw in rank_by_raw else None
            updates.append((raw, idx, method, fid, pid))
            counts[method or "unlinked"] += 1
    with conn:
        conn.executemany("UPDATE portrait_appearances SET athlete_raw_id = ?, ranking_idx = ?, "
                         "link_method = ? WHERE fest_id = ? AND portrait_id = ?", updates)
    return counts


def _same_text(a: str | None, b: str | None) -> bool:
    return bool(a and b) and name_key(a) == name_key(b)  # type: ignore[arg-type]
