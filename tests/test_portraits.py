"""schlussgang portraits: JSON:API parsing, polite download, loading and linking.

Fixtures are saved API responses (``tests/fixtures/schlussgang/portraits_page1.json``:
first three portraits of the real page 1 with its paging links;
``event_portraits_12_2025.json``: one event of the real tid 12 / 2025 listing, cut to
four portrait links) and the
``--sample`` subset. Offline only (MockTransport)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from tenacity import wait_none

from src.db import Festival, connect, upsert_festivals
from src.scraper import portraits as P
from src.scraper.client import HttpClient

FIX = Path(__file__).parent / "fixtures" / "schlussgang"
SAMPLE = Path(__file__).parent / "fixtures" / "sample" / "schlussgang"
PAGE1 = json.loads((FIX / "portraits_page1.json").read_text(encoding="utf-8"))
EVENTS = json.loads((FIX / "event_portraits_12_2025.json").read_text(encoding="utf-8"))
EMPTY: dict[str, Any] = {"data": [], "links": {}}


def page2() -> dict[str, Any]:
    """A last page (no ``next`` link): portrait 57 of page 1 under a new node id."""
    doc = copy.deepcopy(PAGE1)
    doc["data"] = doc["data"][2:]
    doc["data"][0]["attributes"].update(drupal_internal__nid=9001, title="Jaquier Paul",
                                        field_portrait_first_name="Paul")
    doc["data"][0]["attributes"]["path"] = {"alias": "/portraet/paul-jaquier"}
    del doc["links"]["next"]
    return doc


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.url.host == "backend-api.schlussgang.ch"
        p = req.url.params
        if req.url.path.endswith("/node/portrait"):
            doc = page2() if p.get("page[offset]") == "50" else PAGE1
        elif (p.get("filter[field_category.tid]"),
              (p.get("filter[date][condition][value][0]") or "")[:4]) == ("12", "2025"):
            assert p.get("fields[node--event]") == "drupal_internal__nid,field_ref_portrait"
            doc = EVENTS
        else:
            doc = EMPTY
        return httpx.Response(200, content=json.dumps(doc).encode(),
                              headers={"content-type": "application/vnd.api+json"})


def make_client(tmp_path: Path, api: FakeApi, **kw: Any) -> HttpClient:
    return HttpClient(tmp_path / "raw", "test-agent", transport=httpx.MockTransport(api),
                      retry_wait=wait_none(), sleep=lambda s: None, **kw)


# ----------------------------------------------------------------- parsing
def test_parse_portrait_page_real_response() -> None:
    aerne, fischer, jaquier = P.parse_portrait_page(PAGE1)
    assert fischer == P.Portrait(
        portrait_id=56, slug="yvan-fischer", url="https://www.schlussgang.ch/portraet/yvan-fischer",
        title="Fischer Yvan, 1992-01-01 (900056)", last_name="Fischer", first_name="Yvan",
        name_key="fischer yvan", birthday="1992-01-01", city="Villaz-St-Pierre", hknr=900056,
        club_tid=194, club_name="Cottens", club_esv_id=303, association_name="Suedwestschweiz",
        canton_association="Fribourgeoise", activity=None, end_of_career=None)
    # old portraits lack licence number and club; the Teilverband is still there
    assert (aerne.slug, aerne.hknr, aerne.club_name, aerne.association_name, aerne.activity) == (
        "christian-aerne", None, None, "Nordostschweiz", "Aktive")
    assert aerne.name_key == "arne christian"  # same folding as the statistic sheets
    assert (jaquier.portrait_id, jaquier.club_esv_id) == (57, 303)


def test_parse_portrait_page_tolerates_missing_parts() -> None:
    doc = {"data": [{"attributes": {"title": "Muster Hans, 1990-01-01 (1)"}},  # no nid: skipped
                    {"attributes": {"drupal_internal__nid": 7, "title": "Muster Hans, 1990 (1)",
                                    "path": {"alias": "/node/7"}}}]}
    (p,) = P.parse_portrait_page(doc)
    assert (p.portrait_id, p.slug, p.name_key, p.club_name) == (7, None, "muster hans", None)
    assert P.parse_portrait_page({}) == []


def test_parse_event_portraits_real_response() -> None:
    pairs = P.parse_event_portraits(EVENTS)
    assert pairs == [(21085, 1076), (21085, 19796), (21085, 150), (21085, 634)]
    assert P.parse_event_portraits(EMPTY) == []


def test_portrait_max_requests_env() -> None:
    assert P.portrait_max_requests({}) == 600
    assert P.portrait_max_requests({"SCHWINGEN_PORTRAIT_MAX_REQUESTS": "5"}) == 5
    with pytest.raises(ValueError):
        P.portrait_max_requests({"SCHWINGEN_PORTRAIT_MAX_REQUESTS": "-1"})


# ----------------------------------------------------------------- download
def test_crawl_follows_next_links_and_is_cached(tmp_path: Path) -> None:
    api = FakeApi()
    with make_client(tmp_path, api) as c:
        rep = P.crawl_portraits(c, to_year=2025, max_requests=600)
        # 2 portrait pages + 5 categories x 3 seasons (2023-2025), one page each
        assert (rep.pages, rep.portraits, rep.event_queries, rep.appearances) == (2, 4, 15, 4)
        assert c.stats.network_requests == 17 and rep.cache_misses == []
    assert api.requests[0].headers["user-agent"] == "test-agent"
    with make_client(tmp_path, api) as c:
        rep = P.crawl_portraits(c, to_year=2025, max_requests=600)
        assert c.stats.network_requests == 0 and c.stats.cache_hits == 17
    assert len(api.requests) == 17


def test_crawl_stops_at_request_cap_and_keeps_the_cache(tmp_path: Path) -> None:
    api = FakeApi()
    with make_client(tmp_path, api) as c, pytest.raises(P.PortraitLimitExceeded):
        P.crawl_portraits(c, to_year=2025, max_requests=3)
    assert len(api.requests) == 3
    with make_client(tmp_path, api) as c:  # resumes: only the missing pages are fetched
        P.crawl_portraits(c, to_year=2025, max_requests=600)
        assert c.stats.network_requests == 14


def test_crawl_offline_reports_every_missing_query(tmp_path: Path) -> None:
    api = FakeApi()
    with make_client(tmp_path, api, offline=True) as c:
        rep = P.crawl_portraits(c, to_year=2024, max_requests=600)
    assert api.requests == []
    assert len(rep.cache_misses) == 1 + 10 and rep.cache_misses[0].startswith("portraits:")
    assert "event portraits tid=11 year=2023" in rep.cache_misses


# ----------------------------------------------------------------- loading + linking
def fest(fid: int, date: str = "2025-07-27", **kw: Any) -> Festival:
    return Festival(fest_id=fid, name=f"Fest {fid}", date=date, category="Bergkranz",
                    location=None, url=f"https://www.schlussgang.ch/event/f{fid}", **kw)


def add_athletes(conn: Any, fid: int, names: list[str]) -> None:
    conn.executemany(
        "INSERT INTO athletes_raw (athlete_raw_id, fest_id, idx, name_raw, name, name_key, "
        "name_base_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(f"{fid}-{i:03d}", fid, i, n, n, n.lower(), n.lower()) for i, n in enumerate(names)])
    conn.commit()


def portrait(pid: int, last: str, first: str, city: str | None = None,
             club: str | None = None) -> P.Portrait:
    return P.Portrait(pid, f"{first}-{last}".lower(), None, f"{last} {first}", last, first,
                      P.name_key(f"{last} {first}"), "2000-01-01", city, pid + 30000, None, club,
                      None, "Innerschweiz", None, None, None)


def test_load_portraits_from_cache_offline(tmp_path: Path) -> None:
    api = FakeApi()
    with make_client(tmp_path, api) as c:
        P.crawl_portraits(c, to_year=2025, max_requests=600)
    conn = connect(tmp_path / "s.db")
    upsert_festivals(conn, [fest(21085)])
    with make_client(tmp_path, api, offline=True) as c:
        rep = P.load_portraits(conn, c, to_year=2025, now="2026-10-03T00:00:00+00:00")
    assert (rep.portraits, rep.pages, rep.event_queries, rep.cache_misses) == (4, 2, 15, [])
    # the four linked portraits are not among the four cached ones: counted, not stored
    assert rep.appearances == 0 and rep.skipped == {"unknown_festival": 0, "unknown_portrait": 4}
    row = conn.execute("SELECT slug, hknr, club_name, club_esv_id, association_name, fetched_at "
                       "FROM portraits WHERE portrait_id = 56").fetchone()
    assert tuple(row) == ("yvan-fischer", 900056, "Cottens", 303, "Suedwestschweiz",
                          "2026-10-03T00:00:00+00:00")
    assert conn.execute("SELECT COUNT(*) FROM portraits").fetchone()[0] == 4


def test_load_portraits_without_cache_changes_nothing(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    P.store_portraits(conn, {1: portrait(1, "Muster", "Hans")}, set())
    with make_client(tmp_path, FakeApi(), offline=True) as c:
        rep = P.load_portraits(conn, c, to_year=2023)
    assert rep.portraits == 0 and len(rep.cache_misses) == 6
    assert conn.execute("SELECT COUNT(*) FROM portraits").fetchone()[0] == 1  # kept


def test_link_appearances_name_namesakes_fuzzy_and_unlinked(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    upsert_festivals(conn, [fest(1), fest(2)])
    add_athletes(conn, 1, ["Wicki Joel", "Gasser Dominik 1", "Gasser Dominik 2", "M?ller Remo",
                           "Schuler Alex", "Schuler Alex"])
    add_athletes(conn, 2, ["Wicki Joel"])
    # ranking entries tell the namesakes apart (residence / club)
    conn.executemany(
        "INSERT INTO ranking_entries (fest_id, idx, name_raw, name, name_key, residence, club_raw, "
        "athlete_raw_id) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
        [(0, "x", "Gasser Dominik", "gasser dominik", "Zäziwil", "Siehen", "1-001"),
         (1, "x", "Gasser Dominik", "gasser dominik", "Ibach", "Am Mythen", "1-002")])
    portraits = {10: portrait(10, "Wicki", "Joel"), 11: portrait(11, "Gasser", "Dominik", "Ibach"),
                 12: portrait(12, "Müller", "Remo"), 13: portrait(13, "Schuler", "Alex", "Rothenthurm"),
                 14: portrait(14, "Niemand", "Hans")}
    pairs = {(1, 10), (1, 11), (1, 12), (1, 13), (1, 14), (2, 10), (99, 10), (1, 555)}
    rep = P.store_portraits(conn, portraits, pairs)
    assert rep.appearances == 6
    assert rep.skipped == {"unknown_festival": 1, "unknown_portrait": 1}
    got = {(f, p): (raw, idx, m) for f, p, raw, idx, m in conn.execute(
        "SELECT fest_id, portrait_id, athlete_raw_id, ranking_idx, link_method "
        "FROM portrait_appearances")}
    assert got == {
        (1, 10): ("1-000", None, "name"),
        (2, 10): ("2-000", None, "name"),           # one portrait, two festivals
        (1, 11): ("1-002", 1, "name_city"),         # namesake picked by the ranking residence
        (1, 12): ("1-003", None, "name_fuzzy"),     # glyph-id sheet: 'M?ller'
        (1, 13): (None, None, None),                # two 'Schuler Alex', no ranking evidence
        (1, 14): (None, None, None),                # not in the statistic sheet
    }
    assert rep.linked == {"name": 2, "name_city": 1, "name_fuzzy": 1, "unlinked": 2}
    # two portraits of namesakes, one row: the ranking residence decides, else nobody
    twins = {20: portrait(20, "Gasser", "Dominik", "Zäziwil"), 21: portrait(21, "Gasser", "Dominik", "Ibach"),
             22: portrait(22, "Wicki", "Joel", "Sörenberg"), 23: portrait(23, "Wicki", "Joel", "Root")}
    conn.execute("DELETE FROM athletes_raw WHERE athlete_raw_id = '1-001'")
    P.store_portraits(conn, twins, {(1, 20), (1, 21), (1, 22), (1, 23)})
    assert {p: (raw, m) for p, raw, m in conn.execute(
        "SELECT portrait_id, athlete_raw_id, link_method FROM portrait_appearances")} == {
        20: (None, None), 21: ("1-002", "name_city"), 22: (None, None), 23: (None, None)}
    # reloading replaces everything (no stale appearances)
    rep = P.store_portraits(conn, {10: portraits[10]}, {(2, 10)})
    assert conn.execute("SELECT COUNT(*) FROM portrait_appearances").fetchone()[0] == 1


def test_load_portraits_from_sample_dir(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    upsert_festivals(conn, [fest(46055, "2025-08-02"), fest(37052, "2024-07-27")])
    add_athletes(conn, 46055, ["Schuler Christian", "Wicki Joel"])
    rep = P.load_portraits_from_dir(conn, SAMPLE)
    assert (rep.portraits, rep.appearances, rep.pages, rep.event_queries) == (46, 46, 1, 1)
    assert conn.execute("SELECT COUNT(*) FROM portraits WHERE slug IS NOT NULL").fetchone()[0] == 46
    empty = P.load_portraits_from_dir(conn, tmp_path)  # no files: tables emptied, no error
    assert (empty.portraits, empty.appearances) == (0, 0)
