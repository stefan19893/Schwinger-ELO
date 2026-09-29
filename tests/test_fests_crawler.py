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
    assert f23[22371].category == "ESAF"  # Unspunnen: eidg. Kranz -> ESAF tier (K=48)
    assert f23[22371].eidg_type == "Unspunnen"
    assert f19[24110].eidg_type == "ESAF"  # the real ESAF
    assert f19[24360].eidg_type is None
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
    (11, "Kilchberger Schwinget Kilchberg 2021", "ESAF"),
    (11, "Unspunnen-Schwinget Interlaken 2017", "ESAF"),
    (11, "Jubiläumsschwingfest 125 Jahre ESV Appenzell 2024", "ESAF"),
    (11, "Irgendein Eidgenössischer Anlass 2030", "Bergkranz"),  # unknown eidg. -> fallback
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
    ("Thurgauer Kantonalschwingfest Frauenfeld 2019", None, "active"),  # place, not women
    ("Mittelländisches Schwingfest Frauenkappelen 2023", "Aktivschwinger", "active"),
    ("Nationalturnen am Eidgenössischen Turnfest Lausanne 2025", None, "non_competition"),
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


class FakeUtc:
    def __init__(self, iso: str) -> None:
        self.now = dt.datetime.fromisoformat(iso)

    def set(self, iso: str) -> None:
        self.now = dt.datetime.fromisoformat(iso)

    def __call__(self) -> dt.datetime:
        return self.now


def test_listing_final_after() -> None:
    assert fc.listing_final_after(2025, 60) == dt.datetime(
        2026, 3, 1, 23, 59, 59, tzinfo=dt.timezone.utc)
    assert fc.listing_final_after(2025, 0).date() == dt.date(2025, 12, 31)


def test_year_listing_stays_fresh_until_fetched_after_grace(tmp_path: Path) -> None:
    """Year rollover: 2025 listings expire after 24 h until fetched after 2026-03-01."""
    api = FakeApi()
    conn = connect(tmp_path / "s.db")
    clock = FakeUtc("2025-12-31T10:00:00+00:00")

    def crawl() -> int:
        before = len(api.requests)
        with make_client(tmp_path, api, utcnow=clock) as c:
            fc.crawl_festivals(c, conn, 2025, 2025, today=clock().date(), categories=[15],
                               current_max_age=24 * 3600, final_grace_days=60)
        return len(api.requests) - before

    assert crawl() == 2                       # first fetch (2 pages)
    clock.set("2026-01-01T09:00:00+00:00")
    assert crawl() == 0                       # < 24 h old
    clock.set("2026-01-02T11:00:00+00:00")
    assert crawl() == 2                       # > 24 h and still within the grace period
    clock.set("2026-03-05T08:00:00+00:00")
    assert crawl() == 2                       # cached copy predates the final moment
    clock.set("2027-06-01T08:00:00+00:00")
    assert crawl() == 0                       # fetched after 2026-03-01: final forever
    clock.set("2031-01-01T08:00:00+00:00")
    assert crawl() == 0


def test_past_year_cached_before_final_is_refetched_once(tmp_path: Path) -> None:
    """A listing cached during its own season is not trusted forever (review finding)."""
    api = FakeApi()
    conn = connect(tmp_path / "s.db")
    clock = FakeUtc("2011-09-01T00:00:00+00:00")
    with make_client(tmp_path, api, utcnow=clock) as c:
        fc.crawl_festivals(c, conn, 2011, 2011, today=dt.date(2011, 9, 1), categories=[12])
    clock.set("2026-09-29T00:00:00+00:00")
    for _ in range(2):
        with make_client(tmp_path, api, utcnow=clock) as c:
            fc.crawl_festivals(c, conn, 2011, 2011, today=TODAY, categories=[12])
    assert len(api.requests) == 2  # initial + exactly one refresh


def test_offline_reports_cache_misses(tmp_path: Path) -> None:
    api = FakeApi()
    conn = connect(tmp_path / "s.db")
    with make_client(tmp_path, api) as c:
        fc.crawl_festivals(c, conn, 2011, 2011, today=TODAY, categories=[12])
    with make_client(tmp_path, api, offline=True) as c:
        rep = fc.crawl_festivals(c, conn, 2011, 2012, today=TODAY, categories=[12, 14])
    assert len(api.requests) == 1  # offline run made no requests
    assert rep.cache_misses == ["tid=14 year=2011", "tid=12 year=2012", "tid=14 year=2012"]
    assert len(rep.festivals) == 6  # cached 2011 Bergkranz still processed


def test_offline_serves_stale_cache(tmp_path: Path) -> None:
    api = FakeApi()
    conn = connect(tmp_path / "s.db")
    clock = FakeUtc("2026-06-01T00:00:00+00:00")
    with make_client(tmp_path, api, utcnow=clock) as c:
        fc.crawl_festivals(c, conn, 2026, 2026, today=TODAY, categories=[12])
    clock.set("2026-09-29T00:00:00+00:00")  # stale, but offline must not fetch
    with make_client(tmp_path, api, utcnow=clock, offline=True) as c:
        rep = fc.crawl_festivals(c, conn, 2026, 2026, today=TODAY, categories=[12])
    assert len(api.requests) == 1 and rep.cache_misses == []


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


# ------------------------------------------------------------------ legacy PDF picking
def _legacy(items: list[tuple[str, str]]) -> tuple[dict[tuple[str, str], Any], list[Any]]:
    """(description, filename) pairs -> (included index, field_event_pdf refs)."""
    inc: dict[tuple[str, str], Any] = {}
    refs = []
    for i, (desc, fname) in enumerate(items):
        key = ("file--file", f"f{i}")
        inc[key] = {"type": key[0], "id": key[1], "attributes": {
            "filename": fname, "uri": {"url": f"/sites/default/files/{fname}"}}}
        refs.append({"type": key[0], "id": key[1], "meta": {"description": desc}})
    return inc, refs


def _pick(items: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    inc, refs = _legacy(items)
    stat = fc._pick_legacy_pdf(inc, refs, fc._STAT_RE)
    rank = (fc._pick_legacy_pdf(inc, refs, fc._RANK_RE, avoid=fc._STAT_RE)
            or fc._pick_legacy_pdf(inc, refs, fc._RANK_RE))
    name = lambda u: u.rsplit("/", 1)[-1] if u else None  # noqa: E731
    return name(stat), name(rank)


@pytest.mark.parametrize(("items", "stat", "rank"), [
    # ESAF 2016: "Komplette ..." plus many intermediate lists
    ([("Komplette Schlussrangliste", "rl.pdf"), ("Zwischenrangliste nach 7 Gängen", "z7.pdf"),
      ("Statistik nach 7 Gängen", "s7.pdf"), ("Komplette Statistik", "stat.pdf"),
      ("Schlussrangliste 40-kg-Stein", "stein.pdf")], "stat.pdf", "rl.pdf"),
    # empty descriptions -> filenames (Berchtold 2012)
    ([("", "rl_zuerich12.pdf"), ("", "stat_zuerich12.pdf")], "stat_zuerich12.pdf",
     "rl_zuerich12.pdf"),
    ([("", "schlussrangliste-thorigen.pdf"), ("", "notenblatter.pdf")], "notenblatter.pdf",
     "schlussrangliste-thorigen.pdf"),
    # typo + youth-inclusive lists
    ([("Schlussrangliste", "a.pdf"), ("Statisik", "b.pdf")], "b.pdf", "a.pdf"),
    ([("Schlussrangliste (inkl. Nachwuchs)", "a.pdf"), ("Statistik (inkl. Nachwuchs)", "b.pdf")],
     "b.pdf", "a.pdf"),
    # combined document serves as both
    ([("Schlussrangliste mit Statistik", "c.pdf")], "c.pdf", "c.pdf"),
    # shortened festival: "(nach 5 Gängen)" is the final
    ([("Schlussrangliste (nach 5 Gängen)", "r5.pdf"), ("Statistik (nach 5 Gängen)", "s5.pdf")],
     "s5.pdf", "r5.pdf"),
    # only a ranking -> no bout source
    ([("Schlussrangliste", "a.pdf")], None, "a.pdf"),
    ([], None, None),
])
def test_pick_legacy_pdf(items: list[tuple[str, str]], stat: str | None, rank: str | None) -> None:
    assert _pick(items) == (stat, rank)


@pytest.mark.parametrize(("tid", "name", "expected"), [
    (11, "Eidgenössisches Schwing- und Älplerfest Glarnerland 2025", "ESAF"),
    (11, "Eidgenössisches Schwingfest Estavayer 2016", "ESAF"),
    (11, "Kilchberger Schwinget Kilchberg 2026", "Kilchberg"),
    (11, "Unspunnen-Schwinget Interlaken 2011", "Unspunnen"),
    (11, "Jubiläumsschwingfest 125 Jahre ESV Appenzell 2024", "Jubilaeum"),
    (14, "Jubiläums-Schwingfest 100 Jahre UKSV Altdorf 2017", None),  # cantonal jubilee
    (12, "Rigi-Schwinget 2025", None),
])
def test_eidg_type(tid: int, name: str, expected: str | None) -> None:
    assert fc.eidg_type(tid, name) == expected


# ------------------------------------------------------------------ reference validation
REFERENCE = json.loads(
    (Path(__file__).parent.parent / "src" / "scraper" / "reference"
     / "schwingfeste_schweiz.json").read_text(encoding="utf-8"))["schwingfeste_schweiz"]


def _reference_cases() -> list[tuple[int, str, str]]:
    """(schlussgang tid, name, expected category) for every festival in the reference."""
    cases = [(11, t["name"], "ESAF") for t in REFERENCE["eidgenoessische_feste"]["turniere"]]
    cases += [(12, t["fest"], "Bergkranz") for t in REFERENCE["bergkranzfeste"]["turniere"]]
    cases += [(13, t["fest"], "Teilverband") for t in REFERENCE["teilverbandsfeste"]["turniere"]]
    for tv, names in REFERENCE["kantonal_und_gauverbandsfeste"][
            "unterteilung_nach_teilverband"].items():
        cases += [(14, n, "Gauverband" if tv == "BKSV" else "Kantonal") for n in names]
    return cases


def test_reference_has_expected_shape() -> None:
    cases = _reference_cases()
    assert sum(c == "ESAF" for _, _, c in cases) == 4
    assert sum(c == "Bergkranz" for _, _, c in cases) == 6
    assert sum(c == "Teilverband" for _, _, c in cases) == 5
    assert sum(c == "Gauverband" for _, _, c in cases) == 5  # reference omits Bern-Jura


@pytest.mark.parametrize(("tid", "name", "expected"), _reference_cases())
def test_mapping_agrees_with_reference(tid: int, name: str, expected: str) -> None:
    from src.scraper.festival_reference import reference_category

    assert fc.classify_kind(name, "Aktivschwinger") == "active"
    assert fc.map_category(tid, name) == expected
    assert reference_category(name) == expected


@pytest.mark.parametrize(("tid", "name", "expected"), [
    # schlussgang / fixture spellings of the reference festivals
    (12, "Weissenstein-Schwinget ob Solothurn 2011", "Bergkranz"),
    (12, "Stoos-Schwinget Ibach 2021", "Bergkranz"),
    (13, "Innerschweizerisches Schwingfest Giswil 2027", "Teilverband"),
    (13, "Innerschweizer Schwingfest Seedorf 2025", "Teilverband"),
    (13, "Nordwestschweizerisches Schwingfest Lenzburg 2025", "Teilverband"),
    (13, "Berner Kantonalschwingfest Langnau im Emmental 2025", "Teilverband"),
    (13, "Südwestschweizer Schwingfest Neuenburg 2025", "Teilverband"),
    (14, "Bündner-Glarner Schwingertag Davos 2024", "Kantonal"),
    (14, "Glarner-Bündner Schwingertag Glarus 2024", "Kantonal"),
    (14, "Basellandschaftliches Kantonalschwingfest Pratteln 2024", "Kantonal"),
    (14, "Baselstädtischer Schwingertag Basel 2013", "Kantonal"),
    (14, "Ob- und Nidwaldner Kantonalschwingfest Lungern 2024", "Kantonal"),
    (14, "Bern-Jurassisches Schwingfest Raimeux 2024", "Gauverband"),  # not in reference
    (11, "Jubiläumsschwingfest 125 Jahre ESV Appenzell 2024", "ESAF"),
    (11, "Kilchberger Schwinget Kilchberg 2021", "ESAF"),
])
def test_name_variants_agree_with_reference(tid: int, name: str, expected: str) -> None:
    from src.scraper.festival_reference import reference_category

    assert fc.map_category(tid, name) == expected
    assert reference_category(name) == expected


@pytest.mark.parametrize("name", [
    "Surenen-Schwinget 2025", "Allweg-Schwinget Ennetmoos 2026", "Lueg-Schwinget 2024",
    "Hallenschwinget Sarnen 2025", "Jahresschwinget Thun 2025", "Gibel-Schwinget Bonstetten 2025",
    "Toggenburger Verbandsschwingfest 2025",
])
def test_non_kranzfeste_stay_regional(name: str) -> None:
    from src.scraper.festival_reference import reference_category

    assert fc.map_category(15, name) == "Regional"
    assert reference_category(name) is None


def test_all_fixture_kranzfeste_agree_with_reference() -> None:
    for name in FIX.glob("events_*.json"):
        for f in fc.parse_listing(load(name.name)).festivals:
            assert fc.check_reference(f) in (None, ""), f


def test_crawl_reports_reference_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(fc._SIMPLE_MAP, 12, "Regional")  # simulate a mapping bug
    api = FakeApi()
    with make_client(tmp_path, api) as c:
        rep = fc.crawl_festivals(c, connect(tmp_path / "s.db"), 2011, 2011, today=TODAY,
                                 categories=[12])
    assert len(rep.reference_mismatches) == 6
    assert {exp for _, exp in rep.reference_mismatches} == {"Bergkranz"}
