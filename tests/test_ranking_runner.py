"""Ranking list download, storage and linking to athletes_raw (offline, MockTransport)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest

from src.db import Festival, connect, upsert_festivals
from src.scraper.client import HttpClient
from src.scraper.ranking_pdfs import (download_ranking_pdfs, festivals_with_ranking,
                                      ranking_max_requests)
from src.scraper.ranking_runner import (_Rank, _Stat, link_all, link_festival, norm_rank,
                                        parse_rankings, rank_number, result_agreement,
                                        split_residence)
from src.scraper.statistic_pdfs import PdfLimitExceeded
from tests.fixture_paths import SAMPLE_RANKING, ranking_file

BASE = "https://www.schlussgang.ch/sites/default/files/event-ranking-list"


def fest(fid: int, category: str = "Regional", date: str = "2024-06-01", url: bool = True,
         **kw: object) -> Festival:
    return Festival(fest_id=fid, name=f"Fest {fid}", date=date, category=category,
                    location=None, url=f"https://www.schlussgang.ch/event/f{fid}",
                    ranking_pdf_url=f"{BASE}/{fid}-final.pdf" if url else None,
                    eidg_type="ESAF" if category == "ESAF" else None, **kw)  # type: ignore[arg-type]


def client(tmp_path: Path, handler: object, **kw: object) -> HttpClient:
    return HttpClient(tmp_path / "raw", "test-agent", transport=httpx.MockTransport(handler),
                      sleep=lambda s: None, **kw)  # type: ignore[arg-type]


# ----------------------------------------------------------------- downloads
def test_download_order_tier_first_and_shared_urls_once() -> None:
    fs = [fest(1, "Regional", "2012-05-01"), fest(2, "ESAF", "2019-08-24"),
          fest(3, "Kantonal", "2011-06-01"), fest(4, "Bergkranz", "2020-07-01", url=False),
          fest(5, None, "2013-05-01", kind="youth")]  # type: ignore[arg-type]
    shared = Festival(**{**fest(6, "Teilverband").__dict__,
                         "ranking_pdf_url": fs[0].ranking_pdf_url})
    order = [f.fest_id for f in festivals_with_ranking([*fs, shared])]
    assert order == [2, 6, 3]  # ESAF, Teilverband, Kantonal; 1 shares 6's URL; no URL / youth


def test_download_counts_cache_and_cap(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, content=b"%PDF-fake")

    fs = [fest(i, date="2015-06-01") for i in range(1, 4)]
    with client(tmp_path, handler) as c:
        rep = download_ranking_pdfs(c, fs, today=dt.date(2026, 10, 1), max_requests=None)
        assert (rep.fetched, rep.cached) == (3, 0)
        rep = download_ranking_pdfs(c, fs, today=dt.date(2026, 10, 1), max_requests=None)
        assert (rep.fetched, rep.cached) == (0, 3)  # past seasons: cached forever
    assert len(calls) == 3
    with client(tmp_path / "other", handler) as c, pytest.raises(PdfLimitExceeded):
        download_ranking_pdfs(c, fs, today=dt.date(2026, 10, 1), max_requests=2)


def test_download_stops_after_consecutive_errors(tmp_path: Path) -> None:
    with client(tmp_path, lambda req: httpx.Response(404)) as c:
        rep = download_ranking_pdfs(c, [fest(1)], today=dt.date(2026, 10, 1), max_requests=None)
        assert rep.failed and rep.status_counts == {404: 1}
        with pytest.raises(RuntimeError, match="consecutive"):
            download_ranking_pdfs(c, [fest(i) for i in range(10)], today=dt.date(2026, 10, 1),
                                  max_requests=None, max_consecutive_errors=3)


def test_ranking_max_requests_env() -> None:
    assert ranking_max_requests({}) == 2500
    assert ranking_max_requests({"SCHWINGEN_RANKING_PDF_MAX_REQUESTS": "7"}) == 7
    with pytest.raises(ValueError):
        ranking_max_requests({"SCHWINGEN_RANKING_PDF_MAX_REQUESTS": "-1"})


# ----------------------------------------------------------------- linking units
def R(idx: int, rank: str | None, points: float | None, key: str, result: str | None = None) -> _Rank:
    return _Rank(idx, rank, points, key, result)


def S(raw: str, rank: str | None, points: float | None, key: str,
      results: dict[int, str] | None = None) -> _Stat:
    return _Stat(raw, rank, points, key, results or {})


def test_norm_rank_and_number() -> None:
    assert norm_rank("1 b") == "1b" and norm_rank("6.") == "6" and norm_rank(None) is None
    assert rank_number("12c") == 12 and rank_number(None) is None


def test_link_by_rank_points_and_name() -> None:
    links, reasons = link_festival(
        [R(0, "1a", 58.5, "wicki joel"), R(1, "1b", 58.5, "stucki christian"),
         R(2, "2", 57.0, "muller hans")],
        [S("f-0", "1a", 58.5, "stucki christian"), S("f-1", "1b", 58.5, "wicki joel"),
         S("f-2", "3", 56.0, "muller hans")])
    got = {ln.idx: (ln.raw_id, ln.method) for ln in links}
    # tied athletes whose letters differ between the lists are linked by name
    assert got[0] == ("f-1", "rank_points") and got[1] == ("f-0", "rank_points")
    assert got[2] == ("f-2", "name")  # rank/points differ (e.g. corrected list)
    assert reasons == {}


def test_letterless_stat_ranks_and_residence_in_name() -> None:
    """Glarner-Bündner style: stat ranks '4' for 4a/4b; plain lists that print
    the residence inside the name cell ('wehrli christian suhr')."""
    links, _ = link_festival(
        [R(0, "4a", 57.0, "burkhalter stefan"), R(1, "4b", 57.0, "schlapfer markus"),
         R(2, "13", 55.0, "wehrli christian suhr")],
        [S("f-0", "4", 57.0, "schlapfer markus"), S("f-1", "4", 57.0, "burkhalter stefan"),
         S("f-2", "13", 55.0, "wehrli christian")])
    assert {(ln.idx, ln.raw_id) for ln in links} == {(0, "f-1"), (1, "f-0"), (2, "f-2")}


def test_same_rank_different_person_is_not_linked() -> None:
    """Ties share points and similar result strings: a different name at the same
    rank is not linked (no result-string guessing)."""
    links, reasons = link_festival(
        [R(0, "16c", 54.0, "dandliker shane", "+-o+-+")],
        [S("f-0", "16c", 54.0, "nussli philipp", {1: "+", 2: "-", 3: "o", 4: "+"})])
    assert links == [] and reasons == {0: "not_in_statistic_sheet"}


def test_different_rank_letter_needs_close_name() -> None:
    links, _ = link_festival([R(0, "10a", 55.25, "erb vincent")],
                             [S("f-0", "10c", 55.25, "roch vincent")])
    assert links == []


def test_namesakes_disambiguated_by_points() -> None:
    links, reasons = link_festival(
        [R(0, None, 57.0, "gasser dominik"), R(1, None, 55.0, "gasser dominik")],
        [S("f-0", None, 55.0, "gasser dominik"), S("f-1", None, 57.0, "gasser dominik")])
    assert {(ln.idx, ln.raw_id, ln.method) for ln in links} == {
        (0, "f-1", "name_points"), (1, "f-0", "name_points")}
    assert reasons == {}


def test_fuzzy_name_and_glyph_wildcards() -> None:
    links, _ = link_festival([R(0, "30", 50.0, "muller remo"), R(1, "31", 49.0, "marridor loic")],
                             [S("f-0", None, 50.0, "m?ller remo"), S("f-1", None, 49.25, "maridor loic")])
    assert {(ln.idx, ln.raw_id, ln.method) for ln in links} == {
        (0, "f-0", "name_fuzzy"), (1, "f-1", "name_fuzzy")}


def test_unlinked_reasons() -> None:
    _, reasons = link_festival([R(0, "1", 58.0, "a b")], [])
    assert reasons == {0: "no_statistic_athletes"}


def test_split_residence_and_result_agreement() -> None:
    assert split_residence("Wehrli Christian Suhr", "wehrli christian") == ("Wehrli Christian", "Suhr")
    assert split_residence("Wehrli Christian", "wehrli christian") is None
    assert result_agreement("+-o+", {1: "+", 2: "-", 3: "+", 9: "o"}) == (2, 3)


# ----------------------------------------------------------------- end to end
def test_parse_store_and_link(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    f = fest(46147, "ESAF", "2026-08-23")
    f = Festival(**{**f.__dict__, "eidg_type": "Kilchberg"})
    upsert_festivals(conn, [f, fest(2, url=False), fest(3)])
    # two stat-sheet athletes of the festival (rank letters omitted for the second)
    conn.executemany(
        "INSERT INTO athletes_raw (athlete_raw_id, fest_id, idx, rank, name_raw, name, name_key, "
        "name_base_key, points) VALUES (?, 46147, ?, ?, ?, ?, ?, ?, ?)",
        [("46147-000", 0, "1", "Giger Samuel ***", "Giger Samuel", "giger samuel", "giger samuel", 58.5),
         ("46147-001", 1, "4", "Kramer Lario ***", "Kramer Lario", "kramer lario", "kramer lario", 56.5)])
    conn.commit()
    pdf = ranking_file("46147.pdf").read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("46147-final.pdf"):
            return httpx.Response(200, content=pdf)
        return httpx.Response(404)

    with client(tmp_path, handler) as c:
        download_ranking_pdfs(c, festivals_with_ranking([f]), today=dt.date(2026, 10, 1),
                              max_requests=None)
    with client(tmp_path, handler, offline=True) as c:
        rep = parse_rankings(conn, c, today=dt.date(2026, 10, 1))
    assert rep.status == {"ok": 1, "no_pdf": 1, "pdf_not_cached": 1}
    assert rep.entries == 60 and rep.linked == 2
    row = conn.execute("SELECT n_entries, n_linked, n_stat_athletes, layout FROM ranking_parse "
                       "WHERE fest_id = 46147").fetchone()
    assert tuple(row) == (60, 2, 2, "esv")
    linked = dict(conn.execute("SELECT name, athlete_raw_id FROM ranking_entries "
                               "WHERE athlete_raw_id IS NOT NULL").fetchall())
    assert linked == {"Giger Samuel": "46147-000", "Kramer Lario": "46147-001"}
    n_rej = conn.execute("SELECT COUNT(*) FROM ranking_rejects WHERE stage = 'link' "
                         "AND reason = 'not_in_statistic_sheet'").fetchone()[0]
    assert n_rej == 58  # every unlinked entry is a reject with its reason
    # second run: unchanged PDF -> not re-parsed, links recomputed identically
    with client(tmp_path, handler, offline=True) as c:
        rep2 = parse_rankings(conn, c, today=dt.date(2026, 10, 1))
    assert rep2.parsed == 0 and rep2.linked == 2
    # stat sheet re-parsed with new ids -> link_all follows
    conn.execute("UPDATE athletes_raw SET athlete_raw_id = 'new-0' WHERE athlete_raw_id = '46147-000'")
    link_all(conn)
    assert conn.execute("SELECT athlete_raw_id FROM ranking_entries WHERE name = 'Giger Samuel'"
                        ).fetchone()[0] == "new-0"
