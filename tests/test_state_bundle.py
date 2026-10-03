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


@pytest.fixture(scope="module")
def warm_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small but complete state: the sample pipeline's database and Parquet files plus
    a cache entry for every statistic PDF (as a real run leaves it behind)."""
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
    with client_from_config(cfg, offline=True) as client:
        for i, url in enumerate(urls):
            body, meta = client.cache_paths(url)
            body.parent.mkdir(parents=True, exist_ok=True)
            body.write_bytes(b"%PDF-1.4 fake " + bytes([i]) * 2000)
            meta.write_text(json.dumps({"url": url, "status": 200,
                                        "fetched_at": "2026-09-29T19:59:20+00:00",
                                        "content_type": "application/pdf"}), encoding="utf-8")
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
    bodies = sorted(warm.raw_dir.rglob("*.body"))
    for body in bodies[1:]:
        body.unlink()
    assert any("statistic PDFs" in p for p in sbd.state_problems(warm))
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
                       "to_year": 2011, "crawl_pdfs": False}, env={})
    assert cli.cmd_crawl(cfg, transport=httpx.MockTransport(api), portraits=False) == 1
    assert api.requests                       # the state check passed, the crawl ran (HTTP 500)


def test_require_state_is_ignored_for_sample_and_warned_elsewhere(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert cli.main(["crawl", "--sample", "--require-state", "--data-dir", str(tmp_path / "s")]) == 0
    with caplog.at_level("WARNING"):
        cli.main(["build", "--require-state", "--allow-empty"])
    assert "--require-state has no effect on `build`" in caplog.text
