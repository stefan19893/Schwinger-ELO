"""CLI + config tests. Offline only; all output goes to tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest

from src import cli
from src.config import REPO_ROOT, SAMPLE_DATA_DIR, Config, load_config


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("SCHWINGEN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(tmp_path / "dist"))
    # never build from (or write to) the real data/ directory in these tests
    monkeypatch.setenv("SCHWINGEN_DATA_DIR", str(tmp_path / "data"))


# ------------------------------------------------------------------ config
def test_defaults() -> None:
    cfg = load_config(env={})
    assert cfg.from_year == 2011
    assert cfg.request_delay_min == 0.5 and cfg.request_delay_max == 1.0
    assert cfg.k_factors["ESAF"] == 48 and cfg.k_factors["Kantonal"] == 24
    assert cfg.k_factors["Gauverband"] == 24 and cfg.k_factors["Regional"] == 16
    assert cfg.raw_dir == cfg.data_dir / "raw"
    assert cfg.db_path == cfg.data_dir / "schwingen.db"


def test_env_overrides_defaults() -> None:
    env = {"SCHWINGEN_FROM_YEAR": "2015", "SCHWINGEN_DATA_DIR": "/x/data",
           "SCHWINGEN_SAMPLE": "true", "SCHWINGEN_MOV_ALPHA": "0.25"}
    cfg = load_config(env=env)
    assert cfg.from_year == 2015
    assert cfg.data_dir == Path("/x/data")
    assert cfg.sample is True
    assert cfg.mov_alpha == 0.25


def test_flag_overrides_env() -> None:
    cfg = load_config({"from_year": 2020, "to_year": None}, env={"SCHWINGEN_FROM_YEAR": "2015"})
    assert cfg.from_year == 2020  # flag wins, None falls through
    assert cfg.to_year == Config().to_year


@pytest.mark.parametrize("env", [
    {"SCHWINGEN_FROM_YEAR": "abc"},
    {"SCHWINGEN_SAMPLE": "maybe"},
    {"SCHWINGEN_FROM_YEAR": "2030", "SCHWINGEN_TO_YEAR": "2020"},
    {"SCHWINGEN_REQUEST_DELAY_MIN": "2.0"},
])
def test_invalid_config_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        load_config(env=env)


# ------------------------------------------------------------------ parser
@pytest.mark.parametrize("argv", [
    ["--sample", "--data-dir", "/d", "all"],
    ["all", "--sample", "--data-dir", "/d"],
])
def test_global_options_before_or_after_subcommand(argv: list[str]) -> None:
    args = cli.build_parser().parse_args(argv)
    assert args.sample is True and args.data_dir == "/d" and args.command == "all"


def test_crawl_year_options() -> None:
    args = cli.build_parser().parse_args(["crawl", "--from-year", "2012", "--to-year", "2013"])
    cfg = cli.config_from_args(args)
    assert (cfg.from_year, cfg.to_year) == (2012, 2013)


def test_serve_port_option() -> None:
    args = cli.build_parser().parse_args(["serve", "--port", "8123"])
    assert cli.config_from_args(args).port == 8123


def test_unknown_command_exits() -> None:
    with pytest.raises(SystemExit):
        cli.main(["bogus"])


# ------------------------------------------------------------------ stages
@pytest.mark.parametrize("cmd", ["crawl", "parse", "clean", "elo"])
def test_stub_stages_succeed(cmd: str, tmp_path: Path) -> None:
    assert cli.main([cmd, "--sample", "--data-dir", str(tmp_path / "data")]) == 0


PAGES = ("index.html", "athlete.html", "fests.html", "about.html")


@pytest.fixture(scope="module")
def sample_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``python -m src.cli all --sample`` once: crawl -> parse -> clean -> elo -> build."""
    root = tmp_path_factory.mktemp("all-sample")
    mp = pytest.MonkeyPatch()
    for key in list(__import__("os").environ):
        if key.startswith("SCHWINGEN_"):
            mp.delenv(key)
    mp.setenv("SCHWINGEN_DIST_DIR", str(root / "dist"))
    try:
        assert cli.main(["all", "--sample", "--data-dir", str(root / "data")]) == 0
    finally:
        mp.undo()
    return root / "dist"


def test_all_sample_produces_dist(sample_site: Path) -> None:
    index = sample_site / "index.html"
    assert index.is_file()
    html = index.read_text(encoding="utf-8")
    assert "Schwinger-ELO" in html
    assert 'href="/' not in html and 'src="/' not in html  # relative URLs only
    assert (sample_site / ".nojekyll").is_file()


def test_all_sample_pages_and_assets_resolve(sample_site: Path) -> None:
    from tests.site_checks import check_page

    assert sorted(p.name for p in sample_site.glob("*.html")) == sorted(PAGES)
    for name in PAGES:
        page = check_page(sample_site / name)   # every href / src exists inside dist
        assert any(u == "css/style.css" for _, _, u in page.links)
    for asset in ("css/style.css", "js/app.js", "js/index.js", "js/athlete.js", "js/charts.js",
                  "js/fests.js", "js/about.js", "vendor/echarts.common.min.js",
                  "vendor/echarts.LICENSE.txt"):
        assert (sample_site / asset).stat().st_size > 0, asset
    # sources of the stylesheet and notes for developers are not published
    assert not list(sample_site.rglob("*.md")) and not (sample_site / "tailwind").exists()


def test_all_sample_data_is_valid_and_linked(sample_site: Path) -> None:
    import json

    data = sample_site / "data"
    files = sorted(data.rglob("*.json"))
    parsed = {p: json.loads(p.read_text(encoding="utf-8")) for p in files}  # all parse
    meta = parsed[data / "meta.json"]
    assert meta["sample"] is True and meta["empty"] is False
    assert meta["counts"]["festivals"] == 5 and meta["counts"]["athletes"] > 300
    rankings = parsed[data / "rankings_latest.json"]
    assert len(rankings["rows"]) == meta["counts"]["ranked"] > 50
    cols = rankings["cols"]
    ids = [r[cols.index("id")] for r in rankings["rows"]]
    for aid in ids:                                    # every ranked athlete has a profile
        assert (data / "history" / f"history_{aid}.json") in parsed
    search = parsed[data / "athletes.json"]
    assert len(search["rows"]) == meta["counts"]["athletes"]
    assert len(list((data / "history").iterdir())) == len(search["rows"])
    festivals = parsed[data / "festivals.json"]
    assert {f"fest_{r[0]}.json" for r in festivals["rows"]} == \
        {p.name for p in (data / "fests").iterdir()}
    top = parsed[data / "history" / f"history_{ids[0]}.json"]
    assert top["rank"] == 1 and top["history"]["rows"]
    for row in top["history"]["rows"]:                 # profile -> festival links resolve
        assert (data / "fests" / f"fest_{row[1]}.json") in parsed
    assert len(parsed[data / "seasons.json"]["seasons"]) == 4
    assert parsed[data / "alltime_top200.json"]["rows"]


def test_site_works_under_a_sub_path(sample_site: Path, tmp_path: Path) -> None:
    """Served as on GitHub Pages (/Schwinger-ELO/): every page, asset and data URL the
    pages use resolves relative to the page and answers 200; unknown athletes give 404
    (the not-found page of athlete.html relies on it)."""
    import functools
    import http.server
    import json
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request

    from tests.site_checks import Page, local_target

    root = tmp_path / "www"
    root.mkdir()
    (root / "Schwinger-ELO").symlink_to(sample_site, target_is_directory=True)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/Schwinger-ELO/"

    def get(url: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(url, timeout=10) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    try:
        for name in PAGES:
            page_url = urllib.parse.urljoin(base, name)
            status, body = get(page_url)
            assert status == 200 and b"Schwinger" in body
            for _, _, url in Page(sample_site / name).links:
                if local_target(url):
                    resolved = urllib.parse.urljoin(page_url, url)
                    assert resolved.startswith(base), resolved   # stays inside the sub-path
                    assert get(resolved)[0] == 200, resolved
        page_url = urllib.parse.urljoin(base, "athlete.html?id=x")
        for rel in ("data/meta.json", "data/rankings_latest.json", "data/athletes.json",
                    "data/seasons.json", "data/alltime_top200.json", "data/festivals.json"):
            status, body = get(urllib.parse.urljoin(page_url, rel))
            assert status == 200 and json.loads(body), rel
        first = json.loads(get(urllib.parse.urljoin(page_url, "data/rankings_latest.json"))[1])
        aid = first["rows"][0][first["cols"].index("id")]
        assert get(urllib.parse.urljoin(page_url, f"data/history/history_{aid}.json"))[0] == 200
        assert get(urllib.parse.urljoin(page_url, "data/history/history_nobody-p0.json"))[0] == 404
        assert get(f"http://127.0.0.1:{httpd.server_address[1]}/data/meta.json")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_build_without_data_still_gives_a_site(tmp_path: Path) -> None:
    assert cli.main(["build"]) == 0   # SCHWINGEN_DATA_DIR points to an empty temp dir
    from tests.site_checks import check_page

    for name in PAGES:
        check_page(tmp_path / "dist" / name)
    assert '"empty":true' in (tmp_path / "dist" / "data" / "meta.json").read_text()


def test_build_is_idempotent(tmp_path: Path) -> None:
    assert cli.main(["build"]) == 0
    assert cli.main(["build"]) == 0
    assert (tmp_path / "dist" / "index.html").is_file()


def test_build_refuses_unsafe_dist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(REPO_ROOT))
    assert cli.main(["build"]) == 1
    assert (REPO_ROOT / "src").is_dir()


def test_build_refuses_foreign_nonempty_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(target))
    assert cli.main(["build"]) == 1
    assert (target / "keep.txt").is_file()


def test_build_replaces_previous_build_and_empty_dir(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()  # empty dir is fine
    assert cli.main(["build"]) == 0
    (dist / "stale.json").write_text("{}")
    assert cli.main(["build"]) == 0  # previous build (has marker) is replaced
    assert not (dist / "stale.json").exists()


def test_serve_without_dist_fails(tmp_path: Path) -> None:
    assert cli.main(["serve"]) == 1


@pytest.mark.parametrize("port", ["0", "99999", "-1"])
def test_serve_rejects_invalid_port(port: str) -> None:
    assert cli.main(["serve", "--port", port]) == 2


def test_serve_port_in_use_fails_cleanly(tmp_path: Path) -> None:
    import socket

    assert cli.main(["build"]) == 0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert cli.main(["serve", "--port", str(port)]) == 1


# ------------------------------------------------------------------ sample isolation
def test_sample_uses_separate_data_dir() -> None:
    assert load_config({"sample": True}, env={}).data_dir == SAMPLE_DATA_DIR
    assert load_config(env={}).data_dir == REPO_ROOT / "data"
    # explicit data dir (flag or env) still wins
    assert load_config({"sample": True, "data_dir": "/d"}, env={}).data_dir == Path("/d")
    env = {"SCHWINGEN_SAMPLE": "1", "SCHWINGEN_DATA_DIR": "/e"}
    assert load_config(env=env).data_dir == Path("/e")


# ------------------------------------------------------------------ ignored options
@pytest.mark.parametrize("argv, warned", [
    (["parse", "--skip-crawl"], "--skip-crawl"),
    (["crawl", "--sample", "--skip-crawl"], "--skip-crawl"),  # --sample: stay offline
    (["elo", "--sample", "--refresh"], "--refresh"),  # --sample: builds its own inputs
])
def test_warns_on_ignored_options(argv: list[str], warned: str, tmp_path: Path,
                                  caplog: pytest.LogCaptureFixture) -> None:
    assert cli.main([*argv, "--data-dir", str(tmp_path)]) == 0
    assert any(warned in r.getMessage() and "no effect" in r.getMessage() for r in caplog.records)


def test_no_warning_for_scoped_options(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert cli.main(["all", "--sample", "--skip-crawl", "--refresh", "--data-dir", str(tmp_path)]) == 0
    assert not any("no effect" in r.getMessage() for r in caplog.records)


def test_user_agent_has_contact_url() -> None:
    assert "https://github.com/" in Config().user_agent


# ------------------------------------------------------------------ crawl
@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record instead of performing the client's politeness sleeps."""
    import time

    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    return slept


def _crawl_cfg(tmp_path: Path, **kw: object) -> Config:
    base: dict[str, object] = {"data_dir": tmp_path / "data", "from_year": 2011,
                               "to_year": 2011, "crawl_pdfs": False}
    return load_config({**base, **kw}, env={})


def test_crawl_sample_is_offline_and_fills_db(tmp_path: Path) -> None:
    from src.db import connect, load_festivals

    assert cli.main(["crawl", "--sample", "--data-dir", str(tmp_path / "d")]) == 0
    fests = load_festivals(connect(tmp_path / "d" / "schwingen.db"))
    assert sorted(fests) == [24110, 26400, 37052, 45965, 46055]
    assert fests[26400].name == "Brünig-Schwinget 2011"
    assert fests[24110].category == "ESAF" and fests[24110].eidg_type == "ESAF"
    assert fests[45965].event_flags == "hallenschwinget" and fests[45965].elo_eligible
    assert not (tmp_path / "d" / "raw").exists()  # nothing fetched or cached


def test_crawl_command_with_mock_api(tmp_path: Path, no_sleep: list[float]) -> None:
    import httpx

    from src.db import connect, load_festivals
    from tests.test_fests_crawler import FakeApi

    api = FakeApi()
    cfg = _crawl_cfg(tmp_path)
    assert cli.cmd_crawl(cfg, transport=httpx.MockTransport(api), portraits=False) == 0
    assert len(api.requests) == 5  # one listing query per crawled category
    assert len(load_festivals(connect(cfg.db_path))) == 6
    assert cli.cmd_crawl(cfg, transport=httpx.MockTransport(api), portraits=False) == 0
    assert len(api.requests) == 5  # second run served from data/raw cache
    refresh = _crawl_cfg(tmp_path, refresh=True)
    assert cli.cmd_crawl(refresh, transport=httpx.MockTransport(api), portraits=False) == 0
    assert len(api.requests) == 10


def test_crawl_command_downloads_portraits(tmp_path: Path, no_sleep: list[float]) -> None:
    """Default crawl: listings, then the portrait list and the 2023+ festival ->
    portrait listings; --portraits-only skips everything else."""
    import httpx

    from tests.test_portraits import FakeApi

    api = FakeApi()
    cfg = _crawl_cfg(tmp_path, to_year=2023)
    assert cli.cmd_crawl(cfg, transport=httpx.MockTransport(api)) == 0
    paths = [r.url.path.rsplit("/", 1)[-1] for r in api.requests]
    # 13 years x 5 categories of listings, 2 portrait pages, 5 portrait-link queries (2023)
    assert paths == ["event"] * 65 + ["portrait"] * 2 + ["event"] * 5
    assert cli.cmd_crawl(cfg, transport=httpx.MockTransport(api), portraits_only=True) == 0
    assert len(api.requests) == 72  # all cached
    other = _crawl_cfg(tmp_path / "other", to_year=2023)
    api2 = FakeApi()
    assert cli.cmd_crawl(other, transport=httpx.MockTransport(api2), portraits_only=True) == 0
    assert [r.url.path.rsplit("/", 1)[-1] for r in api2.requests] == ["portrait"] * 2 + ["event"] * 5
    assert not other.db_path.exists()  # no database needed for the download
    assert cli.cmd_crawl(_crawl_cfg(tmp_path / "x", offline=True),
                         transport=httpx.MockTransport(api2), portraits_only=True) == 1


def test_portrait_cli_options() -> None:
    args = cli.build_parser().parse_args(["crawl", "--portraits-only"])
    assert args.portraits_only and not args.no_portraits
    assert cli.build_parser().parse_args(["all", "--no-portraits"]).no_portraits
    assert cli.main(["crawl", "--sample", "--portraits-only", "--no-portraits"]) == 1


def test_crawl_command_request_cap(tmp_path: Path, no_sleep: list[float]) -> None:
    import httpx

    from tests.test_fests_crawler import FakeApi

    api = FakeApi()
    cfg = _crawl_cfg(tmp_path, crawl_max_requests=2)
    assert cli.cmd_crawl(cfg, transport=httpx.MockTransport(api)) == 1
    assert len(api.requests) == 2


def test_crawl_command_http_error_exits_1(tmp_path: Path, no_sleep: list[float]) -> None:
    import httpx

    cfg = _crawl_cfg(tmp_path, max_retries=1)
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    assert cli.cmd_crawl(cfg, transport=transport) == 1


# ------------------------------------------------------------------ politeness floor / offline
@pytest.mark.parametrize("env", [
    {"SCHWINGEN_REQUEST_DELAY_MIN": "0.1"},
    {"SCHWINGEN_REQUEST_DELAY_MIN": "0", "SCHWINGEN_REQUEST_DELAY_MAX": "0"},
    {"SCHWINGEN_REQUEST_DELAY_MIN": "0.49"},
])
def test_delay_floor_rejects_env_override(env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="politeness"):
        load_config(env=env)


def test_delay_floor_rejected_by_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHWINGEN_REQUEST_DELAY_MIN", "0.01")
    assert cli.main(["crawl", "--sample"]) == 2


def test_delay_at_floor_is_allowed() -> None:
    cfg = load_config(env={"SCHWINGEN_REQUEST_DELAY_MIN": "0.5",
                           "SCHWINGEN_REQUEST_DELAY_MAX": "0.5"})
    assert cfg.request_delay_min == 0.5


def test_offline_flag_and_env() -> None:
    args = cli.build_parser().parse_args(["crawl", "--offline"])
    assert cli.config_from_args(args).offline is True
    assert load_config(env={"SCHWINGEN_OFFLINE": "1"}).offline is True
    assert load_config(env={}).offline is False
    with pytest.raises(ValueError):
        load_config({"offline": True, "refresh": True}, env={})


def test_crawl_offline_uses_cache_and_reports_misses(tmp_path: Path,
                                                     no_sleep: list[float]) -> None:
    import httpx

    from tests.test_fests_crawler import FakeApi

    api = FakeApi()
    assert cli.cmd_crawl(_crawl_cfg(tmp_path), transport=httpx.MockTransport(api)) == 0
    n = len(api.requests)
    offline = _crawl_cfg(tmp_path, offline=True)
    assert cli.cmd_crawl(offline, transport=httpx.MockTransport(api)) == 0  # all cached
    wider = _crawl_cfg(tmp_path, offline=True, to_year=2012)
    assert cli.cmd_crawl(wider, transport=httpx.MockTransport(api)) == 1  # 2012 not cached
    assert len(api.requests) == n  # offline never fetched


def test_elo_exclude_flags_validated() -> None:
    assert load_config(env={"SCHWINGEN_ELO_EXCLUDE_FLAGS": "team"}).elo_exclude_flags == "team"
    with pytest.raises(ValueError, match="unknown flags"):
        load_config(env={"SCHWINGEN_ELO_EXCLUDE_FLAGS": "team,indoor"})



# ------------------------------------------------------------------ parse (Phase 2)
def test_parse_sample_offline(tmp_path: Path) -> None:
    import sqlite3

    d = str(tmp_path / "d")
    assert cli.main(["crawl", "--sample", "--data-dir", d]) == 0
    assert cli.main(["parse", "--sample", "--data-dir", d]) == 0
    conn = sqlite3.connect(tmp_path / "d" / "schwingen.db")
    q = lambda s: conn.execute(s).fetchall()  # noqa: E731
    assert dict(q("SELECT fest_id, status FROM festival_parse")) == {
        24110: "ok", 26400: "ok", 37052: "partial", 45965: "ok", 46055: "ok"}
    n_bouts, max_gang = q("SELECT COUNT(*), MAX(gang_nr) FROM bouts")[0]
    assert n_bouts > 1500 and max_gang == 8  # includes the ESAF (8 Gänge)
    assert q("SELECT COUNT(*) FROM bouts WHERE fest_id = 46055") == [(274,)]
    # extra bouts (ESAF 2019, Kirchberg, Scheidegg) are stored with a NULL grade
    assert q("SELECT COUNT(*) FROM bouts WHERE flags LIKE '%extra_bout%'") == [(4,)]
    assert not (tmp_path / "d" / "raw").exists()
    # identity evidence from the sample ranking lists and portraits (Phase 3)
    assert dict(q("SELECT fest_id, status FROM ranking_parse")) == {
        24110: "ok", 26400: "ok", 37052: "ok", 45965: "ok", 46055: "ok"}
    assert q("SELECT COUNT(*) FROM ranking_entries WHERE athlete_raw_id IS NULL") == [(0,)]
    assert q("SELECT COUNT(*) FROM athlete_evidence") == q("SELECT COUNT(*) FROM athletes_raw")
    assert q("SELECT residence, club, sub_association, sub_assoc_source, portrait_slug "
             "FROM athlete_evidence e JOIN athletes_raw a USING (athlete_raw_id) "
             "WHERE a.fest_id = 46055 AND a.rank = '1'") == [
        ("Uffikon", "Surental", "ISV", "club", "fabian-scherrer")]
    assert q("SELECT COUNT(*) > 30 FROM athlete_evidence WHERE portrait_slug IS NOT NULL") == [(1,)]
    assert q("SELECT name, sub_association, esv_id FROM clubs WHERE club_key = 'surental'") == [
        ("Surental", "ISV", 206)]
    # incremental: nothing re-parsed, --force re-parses everything
    assert cli.main(["parse", "--sample", "--data-dir", d]) == 0
    assert cli.main(["parse", "--sample", "--force", "--data-dir", d]) == 0
    assert q("SELECT COUNT(*) FROM bouts WHERE fest_id = 46055") == [(274,)]


def test_all_sample_runs_crawl_and_parse(tmp_path: Path) -> None:
    import sqlite3

    assert cli.main(["all", "--sample", "--data-dir", str(tmp_path / "d")]) == 0
    conn = sqlite3.connect(tmp_path / "d" / "schwingen.db")
    assert conn.execute("SELECT COUNT(*) FROM bouts").fetchone()[0] > 1500


def test_parse_real_mode_is_offline(tmp_path: Path) -> None:
    """Without a cache every festival is reported as pdf_not_cached; no network."""
    import sqlite3

    from src.db import connect, upsert_festivals
    from tests.test_parse_runner import fest

    conn = connect(tmp_path / "d" / "schwingen.db")
    upsert_festivals(conn, [fest(46055)])
    conn.close()
    assert cli.main(["parse", "--data-dir", str(tmp_path / "d")]) == 0
    conn2 = sqlite3.connect(tmp_path / "d" / "schwingen.db")
    assert conn2.execute("SELECT status FROM festival_parse").fetchall() == [("pdf_not_cached",)]


def test_parse_force_flag() -> None:
    assert cli.build_parser().parse_args(["parse", "--force"]).force is True
    assert cli.build_parser().parse_args(["crawl", "--no-pdfs"]).no_pdfs is True
