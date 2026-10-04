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
    # the site under test is the sample build, which runs without the unknown-birth-year rule
    monkeypatch.setenv("SCHWINGEN_PUBLISH_UNKNOWN_RECENT_SEASONS", "0")
    # ... and whose data begin in 2011 (the guard holds the site against `from_year`)
    monkeypatch.setenv("SCHWINGEN_FROM_YEAR", "2011")


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
    for publish, env in (({"min_age": 0, "noindex": True, "withheld_from_birth_year": None,
                           "unknown_recent_seasons": 0},
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


# --------------------------------------------------------------------------- the age filter
def _edit_baseline(path: Path, **top: Any) -> None:
    base = json.loads(path.read_text(encoding="utf-8"))
    for key, val in top.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            base[key].update(val)
        else:
            base[key] = val
    path.write_text(json.dumps(base), encoding="utf-8")


def test_fewer_withheld_athletes_fail(built: Path, tmp_path: Path) -> None:
    """The filter's collapse (reviewer's case: 718 withheld became 87 and it passed)."""
    meta = _site(built, tmp_path)
    w = meta["counts"]["withheld"]
    assert w >= 3
    path = _baseline(tmp_path, meta, withheld=w * 8)
    before = path.read_text(encoding="utf-8")
    assert cli.main(["check-site", "--record"]) == 1
    assert path.read_text(encoding="utf-8") == before
    assert cli.main(["check-site", "--accept-changes"]) == 0
    _baseline(tmp_path, meta, withheld=w)                 # more withheld is never a problem
    _site(built, tmp_path, withheld=w + 50)
    assert cli.main(["check-site"]) == 0


@pytest.mark.parametrize("withheld", [0, None])
def test_a_filter_that_withholds_nobody_is_fatal(built: Path, tmp_path: Path,
                                                 withheld: Any) -> None:
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta)
    _site(built, tmp_path, withheld=withheld)
    assert cli.main(["check-site"]) == 1
    assert cli.main(["check-site", "--accept-changes", "--record"]) == 1   # no override
    assert dg.read_meta(tmp_path / "data" / dg.BASELINE_NAME)["counts"]["withheld"] > 0
    (tmp_path / "data" / dg.BASELINE_NAME).unlink()       # ... also as a first deployment
    assert cli.main(["check-site", "--accept-changes", "--record"]) == 1
    assert not (tmp_path / "data" / dg.BASELINE_NAME).exists()


@pytest.mark.parametrize("key, factor, passes", [
    ("athletes", 1.02, True), ("athletes", 1.05, False),      # allowed rise 3 %
    ("ranked", 1.08, True), ("ranked", 1.15, False),          # allowed rise 10 %
])
def test_more_published_athletes_fail_beyond_the_tolerance(
        built: Path, tmp_path: Path, key: str, factor: float, passes: bool) -> None:
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta, **{key: int(meta["counts"][key] / factor)})
    assert (cli.main(["check-site"]) == 0) is passes
    assert cli.main(["check-site", "--accept-changes"]) == 0


def test_a_longer_history_is_refused_until_accepted_and_says_what_changed(
        built: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Phase 10: the data begin seven seasons earlier, so more athletes are named and
    fewer of the rated ones have a birth year. Both findings are those of a broken age
    filter - the guard still refuses (the override is the owner's), but its lines name
    the first season, and the last line gives the one-time command. Nothing is recorded
    by a refused run."""
    meta = _site(built, tmp_path)
    c = meta["counts"]
    base = json.loads(json.dumps(meta))
    base["first_season"] = meta["first_season"] + 7
    base["counts"].update(athletes=int(c["athletes"] / 1.4),
                          birth_year_known=min(c["rated"], int(c["birth_year_known"] * 1.3)))
    path = tmp_path / "data" / dg.BASELINE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(base), encoding="utf-8")
    before = path.read_bytes()
    cfg = load_config(env=dict(os.environ))
    rep = dg.check_site(cfg, tmp_path / "dist", base)
    assert not rep.ok and not rep.fatal and len(rep.changes) == 2, rep.changes
    old, new = base["first_season"], meta["first_season"]
    assert all(f"The data now begin in {new} instead of {old}" in line
               for line in rep.changes)
    assert f"first season of the data: {old} -> {new}" in rep.notes
    with caplog.at_level("INFO"):
        assert cli.main(["check-site", "--record"]) == 1
    assert "check-site --accept-changes --record" in caplog.text
    assert "accept_changes" in caplog.text and "2 change(s)" in caplog.text
    assert path.read_bytes() == before                      # a refusal records nothing
    assert cli.main(["check-site", "--accept-changes", "--record"]) == 0
    assert dg.read_meta(path)["first_season"] == new
    assert cli.main(["check-site"]) == 0                    # ... once: the next run passes
    # without a change of the first season the findings carry no such explanation
    _baseline(tmp_path, meta, athletes=int(c["athletes"] / 1.4))
    rep = dg.check_site(cfg, tmp_path / "dist", dg.read_meta(path))
    assert len(rep.changes) == 1 and "The data now begin" not in rep.changes[0]


def test_new_code_on_a_state_without_the_old_seasons_is_never_deployable(
        built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    """Phase 10 review: a merge deployed before the state bundle was replaced builds the
    old range with the new settings - same counts as the baseline, so nothing else trips -
    and would publish the cold-start season with places. Both signs are fatal, no override
    lifts them, nothing is recorded, and the message says what to do."""
    meta = _site(built, tmp_path)                       # data from 2011, ranked from 2012
    path = _baseline(tmp_path, meta)
    before = path.read_bytes()
    assert cli.main(["check-site"]) == 0
    # the code expects the history from 2004 and ranks from 2005
    monkeypatch.setenv("SCHWINGEN_FROM_YEAR", "2004")
    _site(built, tmp_path, model={**meta["model"], "first_ranked_season": 2005})
    cfg = load_config(env=dict(os.environ))
    rep = dg.check_site(cfg, tmp_path / "dist", dg.read_meta(path), accept_changes=True)
    assert not rep.ok and len(rep.fatal) == 2, rep.fatal
    assert "first ranked season (2005" in rep.fatal[0] and "(2011)" in rep.fatal[0]
    assert "the data begin in 2011, but this code expects them to begin in 2004" in rep.fatal[1]
    assert "seasons 2004-2010" in rep.fatal[1]
    assert all("replace the pipeline state" in line and "Updating the site" in line
               for line in rep.fatal)
    with caplog.at_level("INFO"):
        assert cli.main(["check-site", "--accept-changes", "--record"]) == 1
    assert "must not be deployed" in caplog.text and path.read_bytes() == before
    # each sign alone is enough
    plain = _site(built, tmp_path)                      # ranked from 2012 again
    rep = dg.check_site(cfg, tmp_path / "dist", plain, accept_changes=True)
    assert len(rep.fatal) == 1 and "expects them to begin in 2004" in rep.fatal[0]
    monkeypatch.setenv("SCHWINGEN_FROM_YEAR", "2011")
    cfg = load_config(env=dict(os.environ))
    _site(built, tmp_path, model={**meta["model"], "first_ranked_season": 2011})
    rep = dg.check_site(cfg, tmp_path / "dist", plain, accept_changes=True)
    assert len(rep.fatal) == 1 and "must not be ranked" in rep.fatal[0]
    # a longer burn-in than one season is a setting, not an error
    _site(built, tmp_path, model={**meta["model"], "first_ranked_season": 2013})
    assert dg.check_site(cfg, tmp_path / "dist", plain).ok


def test_a_site_that_lost_its_first_seasons_needs_the_override(built: Path,
                                                               tmp_path: Path) -> None:
    meta = _site(built, tmp_path)
    base = json.loads(json.dumps(meta))
    base["first_season"] = meta["first_season"] - 3
    rep = dg.check_site(load_config(env=dict(os.environ)), tmp_path / "dist", base)
    assert not rep.ok and not rep.fatal
    assert [c for c in rep.changes if "loses seasons" in c]


def test_year_rollover_releases_exactly_the_announced_cohort(built: Path,
                                                             tmp_path: Path) -> None:
    """In January the oldest withheld cohort becomes publishable: no override needed for
    the number the baseline announced, an override for anything beyond it."""
    meta = _site(built, tmp_path)
    c, year = meta["counts"], int(meta["as_of"][:4])
    freed, freed_ranked = 60, 40                         # far above the 3 % / 10 % rises
    old = {"athletes": c["athletes"] - freed, "ranked": c["ranked"] - freed_ranked,
           "withheld": c["withheld"] + freed}
    path = _baseline(tmp_path, meta, **old)
    last_year = f"{year - 1}-10-01"
    _edit_baseline(path, as_of=last_year,
                   publish={"release_next_year": {"athletes": freed, "ranked": freed_ranked}})
    assert cli.main(["check-site"]) == 0
    # the same counts without a year change: the filter lost people
    _edit_baseline(path, as_of=meta["as_of"])
    assert cli.main(["check-site"]) == 1
    # a year change, but fewer were announced than have appeared
    _edit_baseline(path, as_of=last_year,
                   publish={"release_next_year": {"athletes": 10, "ranked": 5}})
    assert cli.main(["check-site"]) == 1
    # two years at once, or a baseline that announced nothing: not comparable
    _edit_baseline(path, as_of=f"{year - 2}-10-01",
                   publish={"release_next_year": {"athletes": freed, "ranked": freed_ranked}})
    assert cli.main(["check-site"]) == 1
    _edit_baseline(path, as_of=last_year, publish={"release_next_year": None})
    assert cli.main(["check-site"]) == 1
    assert cli.main(["check-site", "--accept-changes"]) == 0


def test_falling_share_of_known_birth_years_fails(built: Path, tmp_path: Path) -> None:
    meta = _site(built, tmp_path)
    c = meta["counts"]
    _baseline(tmp_path, meta, birth_year_known=c["birth_year_known"] + c["rated"] // 100)
    assert cli.main(["check-site"]) == 0                  # one point: within two
    _baseline(tmp_path, meta, birth_year_known=c["birth_year_known"] + c["rated"] // 20)
    assert cli.main(["check-site"]) == 1                  # five points
    assert cli.main(["check-site", "--accept-changes"]) == 0
    path = _baseline(tmp_path, meta)
    base = json.loads(path.read_text(encoding="utf-8"))
    del base["counts"]["birth_year_known"], base["counts"]["withheld"]
    path.write_text(json.dumps(base), encoding="utf-8")   # a baseline from before the check
    assert cli.main(["check-site"]) == 1
    assert cli.main(["check-site", "--accept-changes", "--record"]) == 0
    assert cli.main(["check-site"]) == 0


def _portraits_db(tmp_path: Path, birthdays: list[str | None]) -> None:
    import sqlite3
    (tmp_path / "data").mkdir(exist_ok=True)
    conn = sqlite3.connect(tmp_path / "data" / "schwingen.db")
    conn.execute("DROP TABLE IF EXISTS portraits")
    conn.execute("CREATE TABLE portraits (portrait_id INTEGER PRIMARY KEY, birthday TEXT)")
    conn.executemany("INSERT INTO portraits (birthday) VALUES (?)", [(b,) for b in birthdays])
    conn.commit()
    conn.close()


def test_portraits_losing_their_birthdays_fail(built: Path, tmp_path: Path) -> None:
    """The input itself: the baseline remembers how many portraits had a birthday."""
    _site(built, tmp_path)
    _portraits_db(tmp_path, ["2000-01-01"] * 100)
    assert cli.main(["check-site", "--accept-changes", "--record"]) == 0
    base = dg.read_meta(tmp_path / "data" / dg.BASELINE_NAME)
    assert base[dg.INPUTS_KEY] == {"portraits": 100, "portraits_with_birthday": 100}
    _portraits_db(tmp_path, ["2000-01-01"] * 99 + [None])
    assert cli.main(["check-site"]) == 0
    _portraits_db(tmp_path, ["2000-01-01"] * 90 + [None] * 5 + [""] * 5)
    assert cli.main(["check-site"]) == 1
    assert cli.main(["check-site", "--accept-changes"]) == 0
    (tmp_path / "data" / "schwingen.db").unlink()          # cannot be verified at all
    assert cli.main(["check-site"]) == 1
    _portraits_db(tmp_path, [None] * 100)                  # no birthday left: no override
    assert cli.main(["check-site", "--accept-changes"]) == 1


def test_collapsed_filter_of_the_review_fails(built: Path, tmp_path: Path) -> None:
    """Phase 6 review M1 with its numbers: 718 / 523 withheld became 87 / 81, the site
    grew from 6314 to 6953 athletes and from 1497 to 1938 ranked - and passed."""
    meta = _site(built, tmp_path, athletes=6953, ranked=1938, withheld=87, withheld_ranked=81,
                 rated=7040, birth_year_known=900)
    path = _baseline(tmp_path, meta, athletes=6314, ranked=1497, withheld=718,
                     withheld_ranked=523, rated=7032, birth_year_known=5287)
    before = path.read_text(encoding="utf-8")
    cfg = load_config(env=dict(os.environ))
    rep = dg.check_site(cfg, tmp_path / "dist", dg.read_meta(path))
    assert not rep.ok and not rep.fatal and len(rep.changes) == 4, rep.changes
    assert cli.main(["check-site", "--record"]) == 1
    assert path.read_text(encoding="utf-8") == before


def test_unknown_birth_year_setting_is_guarded(built: Path, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    meta = _site(built, tmp_path)
    path = _baseline(tmp_path, meta)
    monkeypatch.setenv("SCHWINGEN_PUBLISH_UNKNOWN_RECENT_SEASONS", "3")
    assert cli.main(["check-site", "--accept-changes"]) == 1   # built with 0, configured 3
    monkeypatch.setenv("SCHWINGEN_PUBLISH_UNKNOWN_RECENT_SEASONS", "0")
    _edit_baseline(path, publish={"unknown_recent_seasons": 3})
    assert cli.main(["check-site"]) == 1                       # looser than the accepted site
    assert cli.main(["check-site", "--accept-changes"]) == 0
    cfg = load_config(env={})
    assert (cfg.guard_max_rise, cfg.guard_max_rise_ranked, cfg.guard_max_drop_withheld,
            cfg.guard_max_drop_birth_known) == (0.03, 0.10, 0.02, 0.02)
    with pytest.raises(ValueError, match="guard_max_rise"):
        load_config({"guard_max_rise": 1.5}, env={})


def test_per_athlete_files_must_match_the_search_index(built: Path, tmp_path: Path) -> None:
    """A selectable athlete without profile / comparison file, or a file for somebody who
    is not in the search index, is never deployable."""
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta)
    cfg = load_config(env=dict(os.environ))
    dist = tmp_path / "dist"
    assert dg.check_site(cfg, dist, meta).ok
    victim = sorted((dist / "data" / "bouts").iterdir())[0]
    moved = victim.with_name("bouts_niemand-p0.json")
    victim.rename(moved)
    rep = dg.check_site(cfg, dist, meta, accept_changes=True)
    assert not rep.ok and any("data/bouts" in f and "1 missing, 1 without" in f
                              for f in rep.fatal), rep.fatal
    moved.rename(victim)
    assert dg.check_site(cfg, dist, meta).ok
    sorted((dist / "data" / "history").iterdir())[0].unlink()
    rep = dg.check_site(cfg, dist, meta, accept_changes=True)
    assert any("data/history" in f for f in rep.fatal), rep.fatal
    shutil.rmtree(dist / "data" / "bouts")
    shutil.copytree(built / "data" / "history", dist / "data" / "history", dirs_exist_ok=True)
    rep = dg.check_site(cfg, dist, meta, accept_changes=True)
    assert any("data/bouts: 0 files" in f for f in rep.fatal), rep.fatal


def _edit(path: Path, change: Any) -> None:
    obj = json.loads(path.read_text(encoding="utf-8"))
    change(obj)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _set_first_id(table: dict[str, Any], value: Any) -> None:
    table["rows"][0][table["cols"].index("id")] = value


GHOST = "niemand-geheim-p0"          # an id that is not in the search index
REFERENCES = {
    # (an id without a name is a broken shape, see SHAPES below: here id and name are added)
    "opponent list": ("bouts", lambda o: (o["opps"].append(GHOST), o["names"].append("X Y"))),
    "ranking row": ("rankings_latest.json", lambda o: _set_first_id(o, GHOST)),
    "all-time row": ("alltime_top200.json", lambda o: _set_first_id(o, GHOST)),
    "season row": ("seasons.json", lambda o: _set_first_id(o["seasons"][0], GHOST)),
    "season peak": ("seasons.json", lambda o: next(
        s for s in o["seasons"] if s["peak"])["peak"].update(id=GHOST)),
    "festival row": ("fests", lambda o: _set_first_id(o["athletes"], GHOST)),
    "namesake": ("history", lambda o: o["namesakes"].append({"id": GHOST, "unc": 0})),
    "twin of the search index": ("athletes.json",
                                 lambda o: o.setdefault("twins", {}).update({GHOST: [1, 6]})),
    "not a string": ("bouts", lambda o: (o["opps"].append(["x"]), o["names"].append("X Y"))),
}


SHAPES = {
    "a name without an id": lambda o: o["names"].append("Muster Hans"),
    "a name that is no text": lambda o: o["names"].__setitem__(0, None),
    "names missing": lambda o: o.pop("names"),
    "marker outside the list": lambda o: o["unc"].append(len(o["opps"])),
}


@pytest.mark.parametrize("case", sorted(SHAPES))
def test_opponent_names_must_match_the_opponent_ids(built: Path, tmp_path: Path,
                                                    case: str) -> None:
    """A comparison file names its opponents: exactly one name per id. A name without an
    id would pass the id check, so the shape itself is fatal."""
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta)
    cfg = load_config(env=dict(os.environ))
    dist = tmp_path / "dist"
    target = next(p for p in sorted((dist / "data" / "bouts").iterdir())
                  if json.loads(p.read_bytes())["opps"])
    _edit(target, SHAPES[case])
    rep = dg.check_site(cfg, dist, meta, accept_changes=True)
    assert not rep.ok and len(rep.fatal) == 1, rep.fatal
    assert rep.fatal[0].startswith("data/bouts: 1 file(s) missing, unreadable or not in the "
                                   "expected shape"), rep.fatal


def _rated(o: dict[str, Any]) -> list[Any]:
    """First rated row of a bouts file (the one with a contribution)."""
    return next(r for f in o["fests"] for r in f[1] if r[6] is not None)


def _first_fest_with_rated(o: dict[str, Any]) -> list[Any]:
    return next(f for f in o["fests"] if any(r[6] is not None for r in f[1]))


# What a file would look like that says something about a bout against an athlete who is
# not published, or that carries more than its contract (phase 9 review: such builds
# passed the guard). Each case is one tampered copy of a real bouts file.
TAMPERED = {
    "a remainder key": lambda o: o.update(rest=[[o["fests"][0][0], 2, -12.3]]),
    "a count of hidden bouts": lambda o: o.update(hidden=3),
    "a key missing": lambda o: o.pop("other"),
    "a row without an opponent": lambda o: _first_fest_with_rated(o)[1].append(
        [7, None, 1, 9.75, 8.5, 0, 4.2]),
    "a row with opponent -1": lambda o: _first_fest_with_rated(o)[1].append(
        [7, -1, 1, 9.75, 8.5, 0, 4.2]),
    "a row with an opponent beyond the list": lambda o: _first_fest_with_rated(o)[1].append(
        [7, len(o["opps"]), 1, 9.75, 8.5, 0, 4.2]),
    "a row with a true as opponent": lambda o: _rated(o).__setitem__(1, True),
    "an eighth value": lambda o: _rated(o).append(1432.5),
    "a row that is too short": lambda o: _rated(o).pop(),
    # a hidden bout passed off under a published opponent: the history file counts the
    # festival's rated bouts, the listed ones cannot be more
    "more rated rows than the festival has bouts": lambda o: _first_fest_with_rated(o)[1].extend(
        [list(_rated(o)) for _ in range(12)]),
    "rated rows at a festival outside the history": lambda o: o["fests"].append(
        [987654321, [list(_rated(o))]]),
    "a contribution at an unrated bout": lambda o: _rated(o).__setitem__(5, dg.BOUT_UNRATED),
    "no contribution at a rated bout": lambda o: _rated(o).__setitem__(6, None),
    "a contribution that is text": lambda o: _rated(o).__setitem__(6, "4.2 (1 hidden)"),
    "another name for an opponent": lambda o: o["names"].__setitem__(0, o["names"][0] + " jun."),
    "an opponent listed twice": lambda o: (o["opps"].append(o["opps"][0]),
                                           o["names"].append(o["names"][0])),
    "other columns": lambda o: o["cols"].append("exp"),
    "an extra key in the other festivals": lambda o: o["other"].update(hidden=[1]),
    "a festival entry with a third value": lambda o: o["fests"][0].append({"rest": -3.1}),
    "another build's stamp": lambda o: o.update(build="0123456789ab"),
    "no stamp": lambda o: o.pop("build"),
}


@pytest.mark.parametrize("case", sorted(TAMPERED))
def test_bout_files_must_keep_to_their_contract(built: Path, tmp_path: Path, case: str,
                                                caplog: pytest.LogCaptureFixture) -> None:
    """A bouts file lists bouts between published athletes and says nothing about any
    other bout. A file with a key, a row or a value beyond that is not deployable, and no
    override lifts it; the log line carries a count, never the file's athlete."""
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta)
    cfg = load_config(env=dict(os.environ))
    dist = tmp_path / "dist"
    assert dg.check_site(cfg, dist, meta).ok
    target = next(p for p in sorted((dist / "data" / "bouts").iterdir())
                  if any(r[6] is not None for f in json.loads(p.read_bytes())["fests"]
                         for r in f[1]))
    _edit(target, TAMPERED[case])
    rep = dg.check_site(cfg, dist, meta, accept_changes=True)
    assert not rep.ok and len(rep.fatal) == 1, rep.fatal
    assert rep.fatal[0] == ("data/bouts: 1 file(s) missing, unreadable or not in the "
                            "expected shape - rebuild"), rep.fatal
    with caplog.at_level("INFO"):
        assert cli.main(["check-site", "--accept-changes"]) == 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    slug_logged = target.stem[len("bouts_"):] in logged
    assert "must not be deployed" in logged and not slug_logged


def test_guard_contract_is_the_exporters() -> None:
    """The guard keeps its own copy of the bouts-file contract; it must be the one the
    exporter writes (the stamp key is added by the build, see the exporter)."""
    from src.exporter import static_builder as sb
    assert dg.BOUT_COLS == sb.BOUT_SIDE_COLS
    assert dg.BOUT_OTHER_COLS == sb.OTHER_FEST_COLS
    assert dg.BOUT_UNRATED == sb.B_UNRATED
    assert dg.FEST_ATHLETE_COLS == sb.FEST_ATHLETE_COLS
    assert dg.FEST_BOUT_COLS == sb.FEST_BOUT_COLS


def _col(o: dict[str, Any], table: str, name: str) -> int:
    return o[table]["cols"].index(name)


def _anon_row(o: dict[str, Any]) -> list[Any]:
    return next(r for r in o["athletes"]["rows"] if r[_col(o, "athletes", "anon")])


def _hidden_bout(o: dict[str, Any]) -> list[Any]:
    """A bout with a withheld athlete on one side."""
    anon = {i for i, r in enumerate(o["athletes"]["rows"]) if r[_col(o, "athletes", "anon")]}
    return next(b for b in o["bouts"]["rows"] if b[1] in anon or b[2] in anon)


def _listed_bout(o: dict[str, Any]) -> list[Any]:
    return next(b for b in o["bouts"]["rows"] if b[7] is not None)


FEST_TAMPERED = {
    # a contribution where none may be: it would give away the hidden side's rating
    "a contribution at a bout against a withheld athlete":
        lambda o: _hidden_bout(o).__setitem__(7, -12.3),
    "a contribution at a festival that does not count": lambda o: o.update(status="unrated"),
    "a contribution that is text": lambda o: _listed_bout(o).__setitem__(7, "+4.2"),
    "a ninth value in a bout row": lambda o: _listed_bout(o).append(0.61),
    "a bout pointing outside the athlete rows":
        lambda o: _listed_bout(o).__setitem__(2, len(o["athletes"]["rows"])),
    # a withheld athlete's own row: no rating value, no name
    "a rating on a withheld athlete's row":
        lambda o: _anon_row(o).__setitem__(_col(o, "athletes", "before"), 1512.3),
    "an expected score on a withheld athlete's row":
        lambda o: _anon_row(o).__setitem__(_col(o, "athletes", "exp"), 2.4),
    "a name on a withheld athlete's row":
        lambda o: _anon_row(o).__setitem__(_col(o, "athletes", "name"), "Muster Hans"),
    "a remainder column": lambda o: (o["athletes"]["cols"].append("rest"),
                                     [r.append(-3.1) for r in o["athletes"]["rows"]]),
}


@pytest.mark.parametrize("case", sorted(FEST_TAMPERED))
def test_festival_files_carry_no_rating_value_for_unpublished_athletes(
        built: Path, tmp_path: Path, case: str, caplog: pytest.LogCaptureFixture) -> None:
    """Phase 10: the festival files carry the contribution per Gang - only between two
    published athletes. A file with a contribution at any other bout, or with a rating
    value on a withheld athlete's row, is not deployable, and no override lifts it."""
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta)
    cfg = load_config(env=dict(os.environ))
    dist = tmp_path / "dist"
    assert dg.check_site(cfg, dist, meta).ok

    def usable(p: Path) -> bool:          # a rated festival with listed and hidden bouts
        o = json.loads(p.read_bytes())
        anon = {i for i, r in enumerate(o["athletes"]["rows"]) if r[-1]}
        return bool(anon) and any(b[7] is not None for b in o["bouts"]["rows"])

    target = next(p for p in sorted((dist / "data" / "fests").iterdir()) if usable(p))
    _edit(target, FEST_TAMPERED[case])
    rep = dg.check_site(cfg, dist, meta, accept_changes=True)
    assert not rep.ok and len(rep.fatal) == 1, rep.fatal
    assert rep.fatal[0] == ("data/fests: 1 file(s) missing, unreadable or not in the "
                            "expected shape - rebuild"), rep.fatal
    with caplog.at_level("INFO"):
        assert cli.main(["check-site", "--accept-changes"]) == 1
    assert "must not be deployed" in caplog.text


def test_festival_contract_check_is_not_vacuous(built: Path, tmp_path: Path) -> None:
    """On the built site the festival files pass, and they do hold what the check is
    about: contributions between published athletes, bouts against withheld athletes
    without one, rows of withheld athletes without any rating value."""
    _site(built, tmp_path)
    listed = hidden = anon_rows = 0
    for p in sorted((tmp_path / "dist" / "data" / "fests").iterdir()):
        o = json.loads(p.read_bytes())
        assert dg._fest_ids(o, p.stem, dg._Seen(names={})) is not None
        anon = {i for i, r in enumerate(o["athletes"]["rows"]) if r[-1]}
        anon_rows += len(anon)
        for b in o["bouts"]["rows"]:
            if b[1] in anon or b[2] in anon:
                assert b[7] is None
                hidden += 1
            elif b[7] is not None:
                listed += 1
    assert listed > 0 and hidden > 0 and anon_rows > 0


def test_untampered_bout_files_pass_and_are_held_against_the_history(built: Path,
                                                                     tmp_path: Path) -> None:
    """The contract check is not vacuous: on the built site every bouts file passes, the
    files hold rated rows, and the history files gave the reader the bouts per festival."""
    _site(built, tmp_path)
    dist = tmp_path / "dist"
    people = dg._published(dist)
    assert people and all(isinstance(n, str) and n for n in people.values())
    stamp = json.loads((dist / "data" / "meta.json").read_bytes())["build"]
    assert isinstance(stamp, str) and len(stamp) == 12
    seen = dg._Seen(names=people, build=stamp)
    for p in sorted((dist / "data" / "history").iterdir()):
        dg._history_ids(json.loads(p.read_bytes()), p.stem, seen)
    assert len(seen.fest_bouts) == len(people)
    rows = 0
    for p in sorted((dist / "data" / "bouts").iterdir()):
        obj = json.loads(p.read_bytes())
        dg._bout_file_ids(obj, p.stem, seen)
        rows += sum(1 for f in obj["fests"] for r in f[1] if r[6] is not None)
    assert rows > 1000


@pytest.mark.parametrize("case", sorted(REFERENCES))
def test_every_referenced_athlete_must_be_published(built: Path, tmp_path: Path, case: str,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    """The published athletes are the search index. A data file that names any other
    athlete id - an opponent, a ranking row, a festival row - stops the deployment, also
    when the per-athlete files match one to one, and no override lifts it."""
    meta = _site(built, tmp_path)
    _baseline(tmp_path, meta)
    cfg = load_config(env=dict(os.environ))
    dist = tmp_path / "dist"
    rep = dg.check_site(cfg, dist, meta)
    assert rep.ok and any("athlete ids: every id in" in n for n in rep.notes), rep.notes
    name, change = REFERENCES[case]
    target = dist / "data" / name
    if target.is_dir():
        target = sorted(target.iterdir())[3]
    _edit(target, change)
    rep = dg.check_site(cfg, dist, meta, accept_changes=True)
    hits = [f for f in rep.fatal if f.startswith(f"data/{name}: 1 athlete id(s) in 1 file(s)")]
    assert not rep.ok and len(hits) == 1 and len(rep.fatal) == 1, rep.fatal
    # the line goes into a public workflow log: counts, not the id (a name slug)
    named = GHOST in hits[0]
    assert not named
    with caplog.at_level("INFO"):
        assert cli.main(["check-site", "--accept-changes", "--record"]) == 1
    logged = "\n".join(r.getMessage() for r in caplog.records)
    in_log = GHOST in logged
    assert "must not be deployed" in logged and not in_log
    assert dg.read_meta(tmp_path / "data" / dg.BASELINE_NAME) == meta   # nothing recorded


def test_reference_check_reads_the_real_shapes(built: Path, tmp_path: Path) -> None:
    """The check must not pass because it looked at nothing: it reads every file that
    names athletes, accepts rows without an id (festival rows of athletes without a
    profile) and fails on a file it cannot read or that holds somebody else's data."""
    meta = _site(built, tmp_path)
    cfg = load_config(env=dict(os.environ))
    dist = tmp_path / "dist"
    data = dist / "data"
    problems, n_read = dg._reference_problems(dist)
    per_athlete = len(json.loads((data / "athletes.json").read_text(encoding="utf-8"))["rows"])
    assert problems == [] and per_athlete > 100
    assert n_read == 4 + len(list((data / "fests").iterdir())) + 2 * per_athlete
    anon = sum(1 for p in (data / "fests").iterdir() for r in json.loads(
        p.read_text(encoding="utf-8"))["athletes"]["rows"] if r[0] is None)
    assert anon > 0                                   # rows without an id exist and pass
    # a bout file that holds another athlete's data (both are published)
    first, second = sorted((data / "bouts").iterdir())[:2]
    kept = first.read_bytes()
    first.write_bytes(second.read_bytes())
    rep = dg.check_site(cfg, dist, meta, accept_changes=True)
    assert rep.fatal == ["data/bouts: 1 file(s) missing, unreadable or not in the expected "
                         "shape - rebuild"]
    first.write_bytes(kept)
    assert dg.check_site(cfg, dist, meta, accept_changes=True).ok
    # a truncated file, and a list that lost its id column
    victim = sorted((data / "history").iterdir())[0]
    victim.write_bytes(victim.read_bytes()[:200])
    _edit(data / "rankings_latest.json", lambda o: o["cols"].__setitem__(1, "athlete"))
    rep = dg.check_site(cfg, dist, meta, accept_changes=True)
    assert sorted(f.split(":")[0] for f in rep.fatal) == ["data/history",
                                                          "data/rankings_latest.json"]
