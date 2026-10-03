"""Deploy guard (`check-site`): an empty or shrunken site must not be deployed."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from src import cli
from src.config import load_config
from src.exporter import deploy_guard as dg


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("SCHWINGEN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(tmp_path / "dist"))
    monkeypatch.setenv("SCHWINGEN_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A built site (the sample data relabelled as real: `meta.sample` false)."""
    root = tmp_path_factory.mktemp("guard")
    mp = pytest.MonkeyPatch()
    for key in list(os.environ):
        if key.startswith("SCHWINGEN_"):
            mp.delenv(key)
    mp.setenv("SCHWINGEN_DIST_DIR", str(root / "dist"))
    try:
        assert cli.main(["build", "--sample", "--data-dir", str(root / "data")]) == 0
    finally:
        mp.undo()
    return root / "dist"


def _site(built: Path, tmp_path: Path, **changes: Any) -> dict[str, Any]:
    """Copy the built site to tmp_path/dist as a "real" one; returns its meta."""
    dist = tmp_path / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    shutil.copytree(built, dist)
    meta = json.loads((dist / "data" / "meta.json").read_text(encoding="utf-8"))
    meta["sample"] = False
    for key, val in changes.items():
        if key in meta["counts"]:
            meta["counts"][key] = val
        else:
            meta[key] = val
    (dist / "data" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def _baseline(tmp_path: Path, meta: dict[str, Any], **counts: int) -> Path:
    base = json.loads(json.dumps(meta))
    base["counts"].update(counts)
    path = tmp_path / "data" / dg.BASELINE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(base), encoding="utf-8")
    return path


def test_first_deployment_needs_an_explicit_accept(built: Path, tmp_path: Path) -> None:
    meta = _site(built, tmp_path)
    path = tmp_path / "data" / dg.BASELINE_NAME
    assert cli.main(["check-site"]) == 1                    # no baseline: not by itself
    assert cli.main(["check-site", "--record"]) == 1 and not path.exists()
    assert cli.main(["check-site", "--accept-changes"]) == 0 and not path.exists()
    assert cli.main(["check-site", "--accept-changes", "--record"]) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == meta
    assert cli.main(["check-site"]) == 0                    # from now on it compares


def test_unchanged_and_grown_sites_pass(built: Path, tmp_path: Path) -> None:
    meta = _site(built, tmp_path)
    c = meta["counts"]
    _baseline(tmp_path, meta, athletes=c["athletes"] - 5, bouts=c["bouts"] - 100)
    assert cli.main(["check-site", "--record"]) == 0
    # recording moved the baseline up to the checked site
    assert dg.read_meta(tmp_path / "data" / dg.BASELINE_NAME)["counts"] == c


@pytest.mark.parametrize("key, factor, passes", [
    ("athletes", 1.01, True), ("athletes", 1.05, False),      # tolerance 2 %
    ("festivals", 1.5, False), ("bouts", 1.03, False),
    ("ranked", 1.2, True), ("ranked", 1.5, False),           # tolerance 25 %
])
def test_shrunken_site_fails_beyond_the_tolerance(built: Path, tmp_path: Path, key: str,
                                                  factor: float, passes: bool) -> None:
    meta = _site(built, tmp_path)
    path = _baseline(tmp_path, meta, **{key: int(meta["counts"][key] * factor) + 1})
    before = path.read_text(encoding="utf-8")
    assert (cli.main(["check-site", "--record"]) == 0) is passes
    if not passes:
        assert path.read_text(encoding="utf-8") == before   # a failed check records nothing
        # the override for an intended drop (e.g. a raised publish_min_age)
        assert cli.main(["check-site", "--accept-changes", "--record"]) == 0
        assert dg.read_meta(path)["counts"][key] == meta["counts"][key]
        assert cli.main(["check-site"]) == 0


def test_tolerances_come_from_the_config(built: Path, tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta, athletes=int(meta["counts"]["athletes"] * 1.05) + 1)
    assert cli.main(["check-site"]) == 1
    monkeypatch.setenv("SCHWINGEN_GUARD_MAX_DROP", "0.10")
    assert cli.main(["check-site"]) == 0
    cfg = load_config(env={})
    assert (cfg.guard_max_drop, cfg.guard_max_drop_ranked) == (0.02, 0.25)
    with pytest.raises(ValueError, match="guard_max_drop"):
        load_config({"guard_max_drop": 1.5}, env={})


@pytest.mark.parametrize("changes", [
    {"empty": True}, {"athletes": 0}, {"ranked": 0}, {"festivals": 0}, {"bouts": 0},
    {"bouts": None}, {"sample": True},
])
def test_empty_or_demo_site_is_never_deployable(built: Path, tmp_path: Path,
                                                changes: dict[str, Any]) -> None:
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta)
    assert cli.main(["check-site"]) == 0
    dist = tmp_path / "dist"
    broken = json.loads((dist / "data" / "meta.json").read_text(encoding="utf-8"))
    for key, val in changes.items():
        (broken["counts"] if key in broken["counts"] else broken)[key] = val
    (dist / "data" / "meta.json").write_text(json.dumps(broken), encoding="utf-8")
    assert cli.main(["check-site"]) == 1
    assert cli.main(["check-site", "--accept-changes"]) == 1   # no override for these


def test_missing_site_or_files_fail(built: Path, tmp_path: Path) -> None:
    assert cli.main(["check-site", "--accept-changes"]) == 1   # no dist at all
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta)
    (tmp_path / "dist" / "data" / "rankings_latest.json").unlink()
    assert cli.main(["check-site", "--accept-changes"]) == 1
    _site(built, tmp_path)
    (tmp_path / "dist" / "data" / "meta.json").write_text("not json", encoding="utf-8")
    assert cli.main(["check-site", "--accept-changes"]) == 1
    _site(built, tmp_path)
    (tmp_path / "data" / dg.BASELINE_NAME).write_text("{broken", encoding="utf-8")
    assert cli.main(["check-site", "--accept-changes"]) == 1   # unreadable baseline


def test_allow_empty_build_does_not_pass(tmp_path: Path) -> None:
    assert cli.main(["build", "--allow-empty"]) == 0
    assert cli.main(["check-site", "--accept-changes"]) == 1


def test_publication_settings_are_guarded(built: Path, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta)
    # checked with other settings than the site was built with: always fatal
    monkeypatch.setenv("SCHWINGEN_PUBLISH_MIN_AGE", "0")
    assert cli.main(["check-site", "--accept-changes"]) == 1
    monkeypatch.delenv("SCHWINGEN_PUBLISH_MIN_AGE")
    # a site that publishes more than the accepted one (age filter lowered, indexable)
    for publish, env in (({"min_age": 0, "noindex": True, "withheld_from_birth_year": None},
                          {"SCHWINGEN_PUBLISH_MIN_AGE": "0"}),
                         ({**meta["publish"], "noindex": False},
                          {"SCHWINGEN_SITE_NOINDEX": "0"})):
        _site(built, tmp_path, publish=publish)
        if publish["noindex"] is False:
            (tmp_path / "dist" / "robots.txt").unlink()
        for key, val in env.items():
            monkeypatch.setenv(key, val)
        assert cli.main(["check-site"]) == 1
        assert cli.main(["check-site", "--accept-changes"]) == 0
        for key in env:
            monkeypatch.delenv(key)


def test_older_data_date_fails(built: Path, tmp_path: Path) -> None:
    meta = _site(built, tmp_path)
    path = _baseline(tmp_path, meta)
    base = json.loads(path.read_text(encoding="utf-8"))
    base["as_of"] = "2999-01-01"
    path.write_text(json.dumps(base), encoding="utf-8")
    assert cli.main(["check-site"]) == 1
    assert cli.main(["check-site", "--accept-changes"]) == 0


def test_sample_site_is_checked_without_baseline(tmp_path: Path) -> None:
    """CI: `all --sample` then `check-site --sample` (never recorded, never deployed)."""
    data = str(tmp_path / "data")
    assert cli.main(["all", "--sample", "--data-dir", data]) == 0
    assert cli.main(["check-site", "--sample", "--data-dir", data, "--record"]) == 0
    assert not (tmp_path / "data" / dg.BASELINE_NAME).exists()
    assert cli.main(["check-site", "--data-dir", data]) == 1   # not as a real site


def test_baseline_option(built: Path, tmp_path: Path) -> None:
    meta = _site(built, tmp_path)
    other = tmp_path / "elsewhere" / "meta.json"
    assert cli.main(["check-site", "--baseline", str(other)]) == 1
    assert cli.main(["check-site", "--baseline", str(other), "--accept-changes", "--record"]) == 0
    assert dg.read_meta(other) == meta and not (tmp_path / "data" / dg.BASELINE_NAME).exists()
