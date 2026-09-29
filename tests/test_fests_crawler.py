"""Festival crawler tests against saved schlussgang JSON:API fixtures (offline)."""

from __future__ import annotations

import copy
import datetime as dt
import json
import random
from pathlib import Path
from typing import Any

import httpx
import pytest
from tenacity import wait_none

from src.db import connect, load_festivals
from src.scraper import fests_crawler as fc
from src.scraper.client import HttpClient

FIX = Path(__file__).parent / "fixtures" / "schlussgang"
TODAY = dt.date(2026, 9, 29)


def load(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def by_id(name: str) -> dict[int, Any]:
    return {f.fest_id: f for f in fc.parse_listing(load(name)).festivals}


# ------------------------------------------------------------------ parsing
def test_parse_bergkranz_2011_legacy_pdfs_and_cancelled() -> None:
    fests = by_id("events_12_2011.json")
    assert len(fests) == 6
    br = fests[26400]
    assert (br.name, br.date, br.category, br.location) == (
        "Brünig-Schwinget 2011", "2011-07-31", "Bergkranz", "Brünig-Passhöhe")
    assert br.kind == "active" and br.esv_id == 1255 and not br.cancelled
    assert br.url == "https://www.schlussgang.ch/event/bruenig-schwinget-2011"
    assert br.statistic_pdf_url is not None and br.statistic_pdf_url.endswith("stat_bruenig.pdf.pdf")
    assert br.ranking_pdf_url is not None and br.ranking_pdf_url.endswith("rl_bruenig.pdf.pdf")
    ws = fests[26402]
    assert ws.cancelled and ws.name == "Weissenstein-Schwinget ob Solothurn 2011"
    assert ws.statistic_pdf_url is None and ws.esv_id is None


def test_parse_new_style_statistic_pdf() -> None:
    f = by_id("events_13_2025.json")[21071]
    assert f.category == "Teilverband" and f.association == "Nordostschweiz"
    assert f.statistic_pdf_url == (
        "https://www.schlussgang.ch/sites/default/files/event-ranking-list/21071-statistic-final.pdf")
    assert f.ranking_pdf_url is not None and f.ranking_pdf_url.endswith("21071-final.pdf")


def test_berner_kantonal_is_teilverband_by_source_category() -> None:
    assert by_id("events_13_2025.json")[21066].category == "Teilverband"


def test_eidgenoessische_anlaesse_mapping() -> None:
    f19 = by_id("events_11_2019.json")
    assert f19[24110].category == "ESAF" and f19[24110].kind == "active"
    assert f19[24110].statistic_pdf_url is not None  # legacy "Statistik" item
    assert f19[24360].kind == "non_competition" and f19[24360].category is None  # AV, type null
    assert f19[24077].kind == "non_competition"  # Fussballturnier
    f23 = by_id("events_11_2023.json")
    assert f23[22371].category == "Bergkranz"  # Unspunnen -> Bergkranz-level K
    assert f23[22599].kind == "non_competition"  # type "Allgmeiner Anlass"


def test_kantonal_vs_gauverband_rule() -> None:
    f = by_id("events_14_2024.json")
    kantonal = {22303, 36814, 22294, 22291}  # Thurgau, Basel-Stadt, OW/NW, Glarner-Bündner
    gau = {22298, 22257, 22236, 34412}  # Mittelländisch, Bern-Jura, Oberaargau, Emmental
    assert {i for i, x in f.items() if x.category == "Kantonal"} == kantonal
    assert {i for i, x in f.items() if x.category == "Gauverband"} == gau


@pytest.mark.parametrize(("tid", "name", "expected"), [
    (11, "Eidgenössisches Schwing- und Älplerfest Glarnerland 2025", "ESAF"),
    (11, "Eidgenössisches Schwingfest Estavayer 2016", "ESAF"),
    (11, "Kilchberger Schwinget Kilchberg 2021", "Bergkranz"),
    (11, "Unspunnen-Schwinget Interlaken 2017", "Bergkranz"),
    (12, "Rigi-Schwinget 2011", "Bergkranz"),
    (13, "Innerschweizer Schwingfest Seedorf 2025", "Teilverband"),
    (14, "Seeländisches Schwingfest Täuffelen 2024", "Gauverband"),
    (14, "Oberländisches Schwingfest Brienz 2024", "Gauverband"),
    (14, "Zürcher Kantonalschwingfest Horgen 2024", "Kantonal"),
    (14, "Bündner-Glarner Schwingertag Davos 2024", "Kantonal"),
    (15, "Rheintal-Oberländisches Kriessern 2025", "Regional"),
    (99, "Irgendwas", None),
])
def test_map_category(tid: int, name: str, expected: str | None) -> None:
    assert fc.map_category(tid, name) == expected


@pytest.mark.parametrize(("name", "etype", "expected"), [
    ("Schwägalp-Schwinget 2011", None, "active"),
    ("Schwägalp-Schwinget 2026", "Aktivschwinger", "active"),
    ("Eidgenössischer Nachwuchsschwingertag Sion 2024", "Jungschwinger", "youth"),
    ("Eidgenössischer Nachwuchsschwingertag Aarburg 2015", None, "youth"),
    ("Wartenbergschwinget Jung. Muttenz", None, "youth"),
    ("Buebeschwinget Attiswil", None, "youth"),
    ("Frauenschwingfest Hasle", None, "women"),
    ("Abgeordnetenversammlung ESV Suhr 2025", "Allgmeiner Anlass", "non_competition"),
    ("Nacht des Schwingsports „Der goldene Kranz“ 2015", None, "non_competition"),
    ("Goldener Kranz Schwingerbrunch 2016", None, "non_competition"),
    ("Jungfrau-Schwinget 2020", None, "active"),  # 'Jung' inside a place name
])
def test_classify_kind(name: str, etype: str | None, expected: str) -> None:
    assert fc.classify_kind(name, etype) == expected


def test_invalid_entries_are_reported_not_dropped_silently() -> None:
    doc = load("events_12_2011.json")
    bad_date = copy.deepcopy(doc["data"][0])
    bad_date["attributes"]["field_event_date"] = None
    no_cat = copy.deepcopy(doc["data"][1])
    no_cat["relationships"]["field_category"]["data"] = None
    doc["data"] = [bad_date, no_cat, doc["data"][2], {"type": "node--article"}]
    res = fc.parse_listing(doc)
    assert len(res.festivals) == 1
    reasons = sorted(s.reason for s in res.skipped)
    assert reasons == ["invalid date None", "missing category", "not an event node"]


# ------------------------------------------------------------------ crawling
ROUTES = {
    ("11", "2019", "0"): "events_11_2019.json",
    ("12", "2011", "0"): "events_12_2011.json",
    ("14", "2024", "0"): "events_14_2024.json",
    ("15", "2025", "0"): "events_15_2025_page1.json",
    ("15", "2025", "50"): "events_15_2025_page2.json",
}
EMPTY = {"data": [], "included": [], "links": {}}


class FakeApi:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.url.host == "backend-api.schlussgang.ch"  # never esv.ch
        p = req.url.params
        key = (p.get("filter[field_category.tid]"),
               (p.get("filter[date][condition][value][0]") or "")[:4],
               p.get("page[offset]", "0"))
        name = ROUTES.get(key)
        body = (FIX / name).read_bytes() if name else json.dumps(EMPTY).encode()
        return httpx.Response(200, content=body,
                              headers={"content-type": "application/vnd.api+json"})


def make_client(tmp_path: Path, api: FakeApi, **kw: Any) -> HttpClient:
    return HttpClient(tmp_path / "raw", "test-agent", transport=httpx.MockTransport(api),
                      retry_wait=wait_none(), sleep=lambda s: None, rng=random.Random(1), **kw)


def test_crawl_follows_paging_and_persists(tmp_path: Path) -> None:
    api = FakeApi()
    conn = connect(tmp_path / "s.db")
    with make_client(tmp_path, api) as c:
        rep = fc.crawl_festivals(c, conn, 2025, 2025, today=TODAY, categories=[15])
    assert len(api.requests) == 2 and rep.pages == 2
    assert set(load_festivals(conn)) == {43884, 48706, 21094, 21084, 38144}
    assert rep.upsert.inserted == 5
    assert rep.counts()[(2025, "Regional")] == 5


def test_crawl_is_incremental_via_cache(tmp_path: Path) -> None:
    api = FakeApi()
    conn = connect(tmp_path / "s.db")
    with make_client(tmp_path, api) as c:
        fc.crawl_festivals(c, conn, 2011, 2011, today=TODAY)
    n = len(api.requests)
    assert n == 5  # one query per crawled category
    with make_client(tmp_path, api) as c:
        rep = fc.crawl_festivals(c, conn, 2011, 2011, today=TODAY)
        assert c.stats.network_requests == 0 and c.stats.cache_hits == 5
    assert len(api.requests) == n
    assert rep.upsert.unchanged == 6 and rep.upsert.inserted == 0


def test_refresh_refetches(tmp_path: Path) -> None:
    api = FakeApi()
    conn = connect(tmp_path / "s.db")
    with make_client(tmp_path, api) as c:
        fc.crawl_festivals(c, conn, 2011, 2011, today=TODAY, categories=[12])
    with make_client(tmp_path, api, refresh=True) as c:
        fc.crawl_festivals(c, conn, 2011, 2011, today=TODAY, categories=[12])
    assert len(api.requests) == 2


def test_current_season_listing_expires(tmp_path: Path) -> None:
    api = FakeApi()
    conn = connect(tmp_path / "s.db")
    today = dt.date(2025, 12, 31)
    with make_client(tmp_path, api) as c:
        fc.crawl_festivals(c, conn, 2025, 2025, today=today, categories=[15],
                           current_max_age=3600)
        fc.crawl_festivals(c, conn, 2025, 2025, today=today, categories=[15],
                           current_max_age=3600)
    assert len(api.requests) == 2  # fresh cache: no refetch
    with make_client(tmp_path, api) as c:
        fc.crawl_festivals(c, conn, 2025, 2025, today=today, categories=[15],
                           current_max_age=-1)  # everything stale
    assert len(api.requests) == 4


def test_future_festivals_counted_not_stored(tmp_path: Path) -> None:
    api = FakeApi()
    conn = connect(tmp_path / "s.db")
    with make_client(tmp_path, api) as c:
        rep = fc.crawl_festivals(c, conn, 2025, 2025, today=dt.date(2025, 5, 31),
                                 categories=[15])
    assert rep.future == 2  # the two June festivals on page 2
    assert set(load_festivals(conn)) == {43884, 48706, 21094}


def test_request_cap(tmp_path: Path) -> None:
    api = FakeApi()
    conn = connect(tmp_path / "s.db")
    with make_client(tmp_path, api) as c:
        with pytest.raises(fc.CrawlLimitExceeded):
            fc.crawl_festivals(c, conn, 2011, 2012, today=TODAY, max_requests=3)
    assert len(api.requests) == 3


def test_listing_params_shape() -> None:
    p = dict(fc.listing_params(14, 2024))
    assert p["filter[field_category.tid]"] == "14"
    assert p["filter[date][condition][value][0]"] == "2024-01-01"
    assert p["filter[date][condition][value][1]"] == "2024-12-31"
    assert p["page[limit]"] == "50"
    assert "field_final_statistic_pdf" in p["include"]
