"""Pipeline state bundle (`state-export` / `state-import`) and `--require-state`."""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path

import httpx
import pytest

from src import cli
from src import state_bundle as sbd
from src.config import Config, load_config
from src.scraper.client import client_from_config


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("SCHWINGEN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(tmp_path / "dist"))
    monkeypatch.setenv("SCHWINGEN_DATA_DIR", str(tmp_path / "unused"))


def _pdf_urls(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute(
            "SELECT statistic_pdf_url FROM festivals WHERE kind = 'active' AND NOT cancelled "
            "AND statistic_pdf_url IS NOT NULL ORDER BY fest_id")]
    finally:
        conn.close()


def _cache_urls(db: Path) -> dict[str, list[str]]:
    """Every URL `--require-state` expects in the cache besides the statistic PDFs."""
    from src.scraper import fests_crawler as fc
    from src.scraper import portraits as pt
    from src.scraper.client import build_url

    conn = sqlite3.connect(db)
    try:
        ranking = [r[0] for r in conn.execute(
            "SELECT DISTINCT ranking_pdf_url FROM festivals WHERE kind = 'active' AND NOT "
            "cancelled AND ranking_pdf_url IS NOT NULL AND ranking_pdf_url != ''")]
        first, last = conn.execute("SELECT MIN(date), MAX(date) FROM festivals").fetchone()
    finally:
        conn.close()
    years = range(int(first[:4]), int(last[:4]) + 1)
    return {
        "ranking": ranking,
        "listings": [build_url(fc.API_URL, fc.listing_params(t, y))
                     for y in years for t in fc.SOURCE_CATEGORIES],
        "links": [build_url(fc.API_URL, pt.event_portrait_params(t, y))
                  for y in years if y >= pt.PORTRAIT_LINK_FIRST_YEAR
                  for t in fc.SOURCE_CATEGORIES],
        "portraits": [build_url(pt.PORTRAIT_API_URL, pt.portrait_list_params())],
    }


def _uncache(cfg: Config, urls: list[str]) -> None:
    with client_from_config(cfg, offline=True) as client:
        for url in urls:
            for path in client.cache_paths(url):
                path.unlink()


@pytest.fixture(scope="module")
def warm_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small but complete state: the sample pipeline's database and Parquet files plus
    a cache entry for everything a crawl would otherwise request again - statistic and
    ranking PDFs, the listing queries, the portrait pages (as a real run leaves it)."""
    root = tmp_path_factory.mktemp("state")
    mp = pytest.MonkeyPatch()
    for key in list(os.environ):
        if key.startswith("SCHWINGEN_"):
            mp.delenv(key)
    mp.setenv("SCHWINGEN_DIST_DIR", str(root / "dist"))
    try:
        assert cli.main(["all", "--sample", "--data-dir", str(root / "data")]) == 0
    finally:
        mp.undo()
    cfg = load_config({"data_dir": root / "data"}, env={})
    urls = _pdf_urls(cfg.db_path)
    assert len(urls) >= 4
    groups = _cache_urls(cfg.db_path)
    assert all(groups.values())
    with client_from_config(cfg, offline=True) as client:
        def put(url: str, content: bytes, kind: str) -> None:
            body, meta = client.cache_paths(url)
            body.parent.mkdir(parents=True, exist_ok=True)
            body.write_bytes(content)
            meta.write_text(json.dumps({"url": url, "status": 200,
                                        "fetched_at": "2026-09-29T19:59:20+00:00",
                                        "content_type": kind}), encoding="utf-8")

        for i, url in enumerate(urls):
            put(url, b"%PDF-1.4 fake " + bytes([i]) * 2000, "application/pdf")
        for i, url in enumerate(groups["ranking"]):
            put(url, b"%PDF-1.4 ranking " + bytes([i]) * 500, "application/pdf")
        for url in groups["listings"] + groups["links"]:
            put(url, b'{"data":[],"links":{}}', "application/vnd.api+json")
        n = sqlite3.connect(cfg.db_path).execute("SELECT COUNT(*) FROM portraits").fetchone()[0]
        put(groups["portraits"][0], json.dumps({"data": [{"id": str(i)} for i in range(n)],
                                                "links": {}}).encode(),
            "application/vnd.api+json")
    (root / "data" / "published_meta.json").write_text('{"counts":{"athletes":1}}',
                                                       encoding="utf-8")
    return root / "data"


@pytest.fixture
def warm(warm_template: Path, tmp_path: Path) -> Config:
    shutil.copytree(warm_template, tmp_path / "data")
    return load_config({"data_dir": tmp_path / "data"}, env={})


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_round_trip_restores_every_file(warm: Config, tmp_path: Path) -> None:
    out = tmp_path / "bundles"
    out.mkdir()
    assert cli.main(["state-export", str(out), "--data-dir", str(warm.data_dir)]) == 0
    [bundle] = list(out.iterdir())
    assert bundle.name.startswith(sbd.BUNDLE_PREFIX) and bundle.name.endswith(sbd.BUNDLE_SUFFIX)
    target = tmp_path / "restored"
    assert cli.main(["state-import", str(out), "--data-dir", str(target)]) == 0
    before, after = _tree(warm.data_dir), _tree(target)
    db = before.pop("schwingen.db"), after.pop("schwingen.db")
    assert before == after and len(after) > 10          # cache, Parquet, baseline: identical
    assert "published_meta.json" in after and any(k.startswith("raw/") for k in after)
    for table in ("festivals", "bouts", "athletes_raw"):  # the database is a snapshot
        rows = [sqlite3.connect(p).execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
                for p in (warm.db_path, target / "schwingen.db")]
        assert rows[0] == rows[1] and rows[0]
    assert len(db[0]) > 0 and not list(target.glob(".state-import-*"))
    restored = load_config({"data_dir": target}, env={})
    assert sbd.state_problems(restored) == []
    # the restored state carries on: the site builds from it
    assert cli.main(["build", "--data-dir", str(target)]) == 0


def test_bundle_layout_and_manifest(warm: Config, tmp_path: Path) -> None:
    info = sbd.export_state(warm, tmp_path / "s.tar.gz")
    with tarfile.open(info.path) as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        manifest = json.loads(tar.extractfile(sbd.MANIFEST).read())
    assert names[0] == sbd.HEADER and names[-1] == sbd.MANIFEST
    assert all(m.isreg() for m in members)
    assert {n.split("/")[0] for n in names} == {"bundle.json", "raw", "processed",
                                                "schwingen.db", "published_meta.json",
                                                "manifest.json"}
    assert not any(n.startswith(("sample", "/", "..")) for n in names)
    assert [f["path"] for f in manifest["files"]] == names[1:-1]
    assert manifest["counts"]["festivals"] == 5 and manifest["counts"]["bouts"] > 0
    assert manifest["baseline"] is True and info.counts == manifest["counts"]
    assert sbd.read_header(info.path)["created_at"] == manifest["created_at"]
    assert not list(tmp_path.glob("*.part")) and not list(tmp_path.glob(".state-db-*"))


def test_import_picks_the_newest_bundle_and_skips_damaged_ones(warm: Config,
                                                               tmp_path: Path) -> None:
    import datetime as dt

    out = tmp_path / "b"
    out.mkdir()
    old = sbd.export_state(warm, out / f"{sbd.BUNDLE_PREFIX}zzz-old{sbd.BUNDLE_SUFFIX}",
                           now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    (warm.data_dir / "published_meta.json").write_text('{"newer":true}', encoding="utf-8")
    new = sbd.export_state(warm, out / f"{sbd.BUNDLE_PREFIX}aaa-new{sbd.BUNDLE_SUFFIX}",
                           now=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc))
    (out / f"{sbd.BUNDLE_PREFIX}broken{sbd.BUNDLE_SUFFIX}").write_bytes(b"not a tar")
    assert sbd.find_bundle(out) == new.path != old.path     # by creation time, not by name
    target = load_config({"data_dir": tmp_path / "t"}, env={})
    assert sbd.import_state(target, out).created_at.startswith("2026-06-01")
    assert (tmp_path / "t" / "published_meta.json").read_text(encoding="utf-8") == '{"newer":true}'


def test_import_refuses_to_overwrite_without_force(warm: Config, tmp_path: Path) -> None:
    bundle = sbd.export_state(warm, tmp_path / "s.tar.gz").path
    marker = warm.raw_dir / "keep.me"
    marker.write_text("local", encoding="utf-8")
    assert cli.main(["state-import", str(bundle), "--data-dir", str(warm.data_dir)]) == 1
    assert marker.is_file()                                   # untouched
    (warm.data_dir / "sample").mkdir()
    (warm.data_dir / "sample" / "x").write_text("demo", encoding="utf-8")
    assert cli.main(["state-import", str(bundle), "--data-dir", str(warm.data_dir),
                     "--force"]) == 0
    assert not marker.exists()                                # replaced, not merged
    assert (warm.data_dir / "sample" / "x").is_file()         # other directories stay


def test_missing_state_fails_the_import(tmp_path: Path) -> None:
    """The CI run after a lost state: nothing to import -> exit 1, nothing created."""
    empty = tmp_path / "downloaded"
    assert cli.main(["state-import", str(empty), "--data-dir", str(tmp_path / "d")]) == 1
    empty.mkdir()
    assert cli.main(["state-import", str(empty), "--data-dir", str(tmp_path / "d")]) == 1
    assert not (tmp_path / "d" / "schwingen.db").exists()


def _rewrite(bundle: Path, out: Path, change) -> Path:  # noqa: ANN001
    """Copy a bundle member by member; ``change(info, data)`` returns the new pair or None."""
    with tarfile.open(bundle) as src, gzip.open(out, "wb") as gz, \
            tarfile.open(fileobj=gz, mode="w") as dst:
        for m in src:
            data = src.extractfile(m).read() if m.isreg() else b""
            res = change(m, data)
            if res is None:
                continue
            m, data = res
            m.size = len(data)
            dst.addfile(m, io.BytesIO(data))
    return out


def test_damaged_bundles_are_rejected_and_leave_nothing_behind(warm: Config,
                                                               tmp_path: Path) -> None:
    good = sbd.export_state(warm, tmp_path / "good.tar.gz").path

    def flip(m: tarfile.TarInfo, data: bytes):  # noqa: ANN202
        return (m, b"X" + data[1:]) if m.name.endswith(".body") else (m, data)

    def drop_manifest(m: tarfile.TarInfo, data: bytes):  # noqa: ANN202
        return None if m.name == sbd.MANIFEST else (m, data)

    def drop_db(m: tarfile.TarInfo, data: bytes):  # noqa: ANN202
        return None if m.name == sbd.DB_NAME else (m, data)

    def escape(m: tarfile.TarInfo, data: bytes):  # noqa: ANN202
        if m.name.endswith(".body"):
            m.name = "raw/../../escaped"
        return m, data

    def absolute(m: tarfile.TarInfo, data: bytes):  # noqa: ANN202
        if m.name == sbd.DB_NAME:
            m.name = str(tmp_path / "abs.db")
        return m, data

    def foreign(m: tarfile.TarInfo, data: bytes):  # noqa: ANN202
        if m.name == sbd.BASELINE_NAME:
            m.name = "dist/index.html"
        return m, data

    def link(m: tarfile.TarInfo, data: bytes):  # noqa: ANN202
        if m.name == sbd.BASELINE_NAME:
            m.type, m.linkname = tarfile.SYMTYPE, "/etc/passwd"
            return m, b""
        return m, data

    truncated = tmp_path / "truncated.tar.gz"
    truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    cases = {"flip": flip, "drop_manifest": drop_manifest, "drop_db": drop_db,
             "escape": escape, "absolute": absolute, "foreign": foreign, "link": link}
    bundles = [_rewrite(good, tmp_path / f"{k}.tar.gz", fn) for k, fn in cases.items()]
    for i, bundle in enumerate([*bundles, truncated]):
        target = load_config({"data_dir": tmp_path / f"t{i}"}, env={})
        with pytest.raises(sbd.StateError):
            sbd.import_state(target, bundle)
        assert not list(target.data_dir.rglob("*")), bundle     # nothing moved into place
    assert not (tmp_path / "escaped").exists() and not (tmp_path / "abs.db").exists()


def test_bundle_carries_the_per_bout_ratings(warm: Config, tmp_path: Path) -> None:
    """Every file in processed/ travels, without being listed anywhere."""
    assert (warm.processed_dir / "bout_ratings.parquet").is_file()
    info = sbd.export_state(warm, tmp_path / "s.tar.gz")
    with tarfile.open(info.path) as tar:
        assert "processed/bout_ratings.parquet" in tar.getnames()
    target = load_config({"data_dir": tmp_path / "restored"}, env={})
    sbd.import_state(target, info.path)
    assert (target.processed_dir / "bout_ratings.parquet").read_bytes() == \
        (warm.processed_dir / "bout_ratings.parquet").read_bytes()


def test_state_from_before_the_per_bout_ratings_still_works(warm: Config,
                                                            tmp_path: Path) -> None:
    """A bundle made before `elo` wrote bout_ratings.parquet: export and import accept
    it, and the `elo` stage (part of `all --skip-crawl`) writes the file again."""
    reference = (warm.processed_dir / "bout_ratings.parquet").read_bytes()
    ratings = (warm.processed_dir / "ratings.parquet").read_bytes()
    (warm.processed_dir / "bout_ratings.parquet").unlink()
    out = tmp_path / "old.tar.gz"
    assert cli.main(["state-export", str(out), "--data-dir", str(warm.data_dir)]) == 0
    target = tmp_path / "restored"
    assert cli.main(["state-import", str(out), "--data-dir", str(target)]) == 0
    restored = load_config({"data_dir": target}, env={})
    assert sbd.state_problems(restored) == []
    assert not (restored.processed_dir / "bout_ratings.parquet").exists()
    assert cli.main(["elo", "--sample", "--data-dir", str(target)]) == 0
    assert (restored.processed_dir / "bout_ratings.parquet").read_bytes() == reference
    assert (restored.processed_dir / "ratings.parquet").read_bytes() == ratings


def test_export_refuses_an_incomplete_state(warm: Config, tmp_path: Path) -> None:
    out = tmp_path / "s.tar.gz"
    assert cli.main(["state-export", str(out), "--sample"]) == 1
    (warm.processed_dir / "ratings.parquet").unlink()
    assert cli.main(["state-export", str(out), "--data-dir", str(warm.data_dir)]) == 1
    shutil.rmtree(warm.raw_dir)
    assert cli.main(["state-export", str(out), "--data-dir", str(warm.data_dir)]) == 1
    assert cli.main(["state-export", str(out), "--data-dir", str(tmp_path / "none")]) == 1
    assert not out.exists() and not list(tmp_path.glob("*.part"))


# --------------------------------------------------------------------------- cold start
def test_state_problems(warm: Config, tmp_path: Path) -> None:
    assert sbd.state_problems(warm) == []
    cold = load_config({"data_dir": tmp_path / "cold"}, env={})
    assert any("does not exist" in p for p in sbd.state_problems(cold))
    # most PDFs gone from the cache: the crawl would download them again
    _uncache(warm, _pdf_urls(warm.db_path)[1:])
    assert [p for p in sbd.state_problems(warm) if "statistic PDFs" in p]
    assert len(sbd.state_problems(warm)) == 1
    shutil.rmtree(warm.raw_dir)
    assert any("no cached responses" in p for p in sbd.state_problems(warm))
    conn = sqlite3.connect(warm.db_path)
    conn.execute("DELETE FROM bouts")
    conn.commit()
    conn.close()
    assert any("`bouts` is empty" in p for p in sbd.state_problems(warm))


class _Counting:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return httpx.Response(500)


@pytest.mark.parametrize("portraits_only", [False, True])
def test_crawl_with_require_state_makes_no_request_on_a_cold_runner(
        tmp_path: Path, portraits_only: bool) -> None:
    api = _Counting()
    cfg = load_config({"data_dir": tmp_path / "cold", "require_state": True}, env={})
    rc = cli.cmd_crawl(cfg, transport=httpx.MockTransport(api), portraits_only=portraits_only)
    assert rc == 1 and api.requests == []
    assert not (tmp_path / "cold").exists()      # not even an empty database is created


def test_all_with_require_state_stops_before_any_stage(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """`all --require-state` on a runner without state: exit 1, no stage runs (the autouse
    network guard would raise on any request; here not even the crawl is entered)."""
    called: list[str] = []
    monkeypatch.setattr(cli, "cmd_crawl", lambda *a, **k: called.append("crawl") or 0)
    monkeypatch.setattr(cli, "cmd_parse", lambda *a, **k: called.append("parse") or 0)
    cold = str(tmp_path / "cold")
    assert cli.main(["all", "--require-state", "--data-dir", cold]) == 1
    assert cli.main(["all", "--skip-crawl", "--require-state", "--data-dir", cold]) == 1
    assert called == [] and not (tmp_path / "dist").exists()
    monkeypatch.undo()
    monkeypatch.setenv("SCHWINGEN_REQUIRE_STATE", "1")   # the environment switch
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(tmp_path / "dist"))
    assert cli.main(["crawl", "--data-dir", cold]) == 1
    assert cli.main(["crawl", "--portraits-only", "--data-dir", cold]) == 1
    assert not Path(cold).exists()


def test_require_state_lets_a_warm_run_through(warm: Config) -> None:
    api = _Counting()
    cfg = load_config({"data_dir": warm.data_dir, "require_state": True, "from_year": 2011,
                       "to_year": 2011, "crawl_pdfs": False, "refresh": True}, env={})
    assert cli.cmd_crawl(cfg, transport=httpx.MockTransport(api), portraits=False) == 1
    assert api.requests                       # the state check passed, the crawl ran (HTTP 500)


def test_require_state_is_ignored_for_sample_and_warned_elsewhere(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert cli.main(["crawl", "--sample", "--require-state", "--data-dir", str(tmp_path / "s")]) == 0
    with caplog.at_level("WARNING"):
        cli.main(["build", "--require-state", "--allow-empty"])
    assert "--require-state has no effect on `build`" in caplog.text


@pytest.mark.parametrize("group, label", [
    ("ranking", "ranking PDFs"), ("listings", "festival listings"),
    ("links", "event-portrait listings"), ("portraits", "portraits"),
])
def test_half_empty_cache_is_not_a_state(warm: Config, group: str, label: str) -> None:
    """Phase 6 review S1: database and statistic PDFs alone passed `--require-state`; a
    live run would then have fetched the listings, ranking PDFs and portraits again."""
    assert all(c == e for c, e in sbd.cache_coverage(warm).values())
    urls = _cache_urls(warm.db_path)[group]
    _uncache(warm, urls[: max(1, len(urls) // 5)] if len(urls) >= 10 else urls)
    problems = sbd.state_problems(warm)
    assert len(problems) == 1 and label in problems[0] and "request" in problems[0]


def test_reviewers_half_empty_state_makes_no_request(warm: Config) -> None:
    for urls in _cache_urls(warm.db_path).values():
        _uncache(warm, urls)
    assert any(warm.raw_dir.rglob("*.body"))               # the statistic PDFs are there
    assert len(sbd.state_problems(warm)) == 4
    api = _Counting()
    cfg = load_config({"data_dir": warm.data_dir, "require_state": True}, env={})
    assert cli.cmd_crawl(cfg, transport=httpx.MockTransport(api)) == 1
    assert cli.main(["all", "--skip-crawl", "--require-state",
                     "--data-dir", str(warm.data_dir)]) == 1
    assert api.requests == []
    assert cli.main(["state-export", str(warm.data_dir.parent / "x.tar.gz"),
                     "--data-dir", str(warm.data_dir)]) == 1  # never uploaded either


# --------------------------------------------------------------------------- review S5 / S6
def _manifest_edit(bundle: Path, out: Path, edit, keep=lambda name: True) -> Path:  # noqa: ANN001
    def change(m: tarfile.TarInfo, data: bytes):  # noqa: ANN202
        if m.name == sbd.MANIFEST:
            manifest = json.loads(data)
            manifest["files"] = [f for f in manifest["files"] if keep(f["path"])]
            edit(manifest)
            return m, json.dumps(manifest).encode()
        return (m, data) if keep(m.name) else None
    return _rewrite(bundle, out, change)


@pytest.mark.parametrize("case", ["no_path", "not_a_dict", "bad_size", "bad_counts"])
def test_malformed_manifest_is_a_clean_error(warm: Config, tmp_path: Path, case: str) -> None:
    good = sbd.export_state(warm, tmp_path / "good.tar.gz").path

    def edit(manifest: dict) -> None:  # noqa: ANN001
        if case == "no_path":
            del manifest["files"][0]["path"]
        elif case == "not_a_dict":
            manifest["files"][0] = "raw/x"
        elif case == "bad_size":
            manifest["files"][0]["size"] = "big"
        else:
            manifest["counts"] = ["festivals"]

    bad = _manifest_edit(good, tmp_path / "bad.tar.gz", edit)
    target = tmp_path / "t"
    with pytest.raises(sbd.StateError, match="manifest"):
        sbd.import_state(load_config({"data_dir": target}, env={}), bad)
    assert cli.main(["state-import", str(bad), "--data-dir", str(target)]) == 1   # no traceback
    assert not list(target.rglob("*"))


def test_import_is_bounded_in_size(warm: Config, tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """A small archive can unpack to gigabytes; the import stops at the cap."""
    assert sbd.MAX_UNPACKED_BYTES >= 4 * 1024 ** 3 and sbd.MAX_FILES >= 50_000
    good = sbd.export_state(warm, tmp_path / "good.tar.gz")
    target = load_config({"data_dir": tmp_path / "t"}, env={})
    monkeypatch.setattr(sbd, "MAX_UNPACKED_BYTES", good.n_bytes - 1)
    with pytest.raises(sbd.StateError, match="unpacks to more than"):
        sbd.import_state(target, good.path)
    assert not list(target.data_dir.rglob("*"))
    monkeypatch.setattr(sbd, "MAX_UNPACKED_BYTES", good.n_bytes)
    monkeypatch.setattr(sbd, "MAX_FILES", good.n_files - 1)
    with pytest.raises(sbd.StateError, match="more than"):
        sbd.import_state(target, good.path)
    monkeypatch.setattr(sbd, "MAX_FILES", good.n_files)
    monkeypatch.setattr(sbd, "MAX_JSON_BYTES", 100)
    with pytest.raises(sbd.StateError, match="larger than"):
        sbd.import_state(target, good.path)
    assert not list(target.data_dir.rglob("*"))
    monkeypatch.undo()
    assert sbd.import_state(target, good.path).n_files == good.n_files


def test_force_never_replaces_good_state_with_a_useless_bundle(warm: Config,
                                                               tmp_path: Path) -> None:
    """Well-formed (manifest and hashes agree) but not a state: database plus one cache
    file. Before the fix `--force` moved it into place and failed afterwards."""
    good = sbd.export_state(warm, tmp_path / "good.tar.gz").path
    keep_one = sorted(p.relative_to(warm.data_dir).as_posix()
                      for p in warm.raw_dir.rglob("*.body"))[0]
    useless = _manifest_edit(
        good, tmp_path / "useless.tar.gz", lambda m: None,
        keep=lambda n: not n.startswith("raw/") or n == keep_one)
    before = _tree(warm.data_dir)
    assert cli.main(["state-import", str(useless), "--data-dir", str(warm.data_dir),
                     "--force"]) == 1
    assert _tree(warm.data_dir) == before and sbd.state_problems(warm) == []
    assert not list(warm.data_dir.glob(".state-import-*"))
    with pytest.raises(sbd.StateError, match="not a usable pipeline state"):
        sbd.import_state(load_config({"data_dir": tmp_path / "fresh"}, env={}), useless)
    assert not list((tmp_path / "fresh").rglob("*"))


def test_export_creates_the_directory_and_a_private_bundle(warm: Config,
                                                           tmp_path: Path) -> None:
    """README step: `state-export <new directory>` (before: a file of that name, 0644)."""
    out = tmp_path / "private" / "schwingen-state"
    old = os.umask(0o022)
    try:
        assert cli.main(["state-export", str(out), "--data-dir", str(warm.data_dir)]) == 0
        named = tmp_path / "named" / "x.tar.gz"
        assert cli.main(["state-export", str(named), "--data-dir", str(warm.data_dir)]) == 0
    finally:
        os.umask(old)
    [bundle] = list(out.glob(f"{sbd.BUNDLE_PREFIX}*{sbd.BUNDLE_SUFFIX}"))
    assert out.is_dir() and bundle.stat().st_mode & 0o777 == 0o600
    assert out.stat().st_mode & 0o077 == 0            # the new directory is private too
    assert named.is_file() and named.stat().st_mode & 0o777 == 0o600
    assert cli.main(["state-import", str(out), "--data-dir", str(tmp_path / "t")]) == 0
    # a plain file in the way is an error, not something to overwrite
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert cli.main(["state-export", str(tmp_path / "file"),
                     "--data-dir", str(warm.data_dir)]) == 1
    assert (tmp_path / "file").read_text(encoding="utf-8") == "x"
