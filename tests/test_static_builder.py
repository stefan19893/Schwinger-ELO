"""Static exporter (Phase 5): JSON contracts of ``dist/data``, determinism, safe JSON.

Runs on the --sample data (always) and on the real data (skipped when absent)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src import cli
from src.config import Config, load_config
from src.exporter import static_builder as sb

ID_RE = re.compile(r"^[a-z0-9-]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TEILVERBAENDE = {"BKSV", "ISV", "NOSV", "NWSV", "SWSV", None}
CATEGORIES = {"ESAF", "Bergkranz", "Teilverband", "Kantonal", "Gauverband", "Regional"}
# nothing of this may be published (task brief: no residences, birthdays, licence numbers)
FORBIDDEN_KEYS = {"residence", "birthday", "birth_date", "licence", "license", "slug",
                  "portrait_slug", "esv_id", "name_raw"}


def load(path: Path) -> Any:
    def no_constants(name: str) -> Any:
        raise AssertionError(f"non-standard JSON constant {name} in {path}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=no_constants)


def table(obj: dict[str, Any]) -> list[dict[str, Any]]:
    assert set(obj) >= {"cols", "rows"}
    assert all(len(r) == len(obj["cols"]) for r in obj["rows"])
    return [dict(zip(obj["cols"], r)) for r in obj["rows"]]


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def keys_of(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        out = set(obj)
        for v in obj.values():
            out |= keys_of(v)
        if "cols" in obj and isinstance(obj["cols"], list):
            out |= set(obj["cols"])
        return out
    if isinstance(obj, list):
        out = set()
        for v in obj:
            out |= keys_of(v)
        return out
    return set()


class Site:
    def __init__(self, dist: Path, cfg: Config) -> None:
        self.dist, self.cfg, self.data = dist, cfg, dist / "data"
        self.meta = load(self.data / "meta.json")
        self.rankings = load(self.data / "rankings_latest.json")
        self.search = load(self.data / "athletes.json")
        self.alltime = load(self.data / "alltime_top200.json")
        self.seasons = load(self.data / "seasons.json")
        self.festivals = load(self.data / "festivals.json")
        self.ar = pd.read_parquet(cfg.processed_dir / "athlete_ratings.parquet")
        self.athletes = pd.read_parquet(cfg.processed_dir / "athletes.parquet")
        self.ratings = pd.read_parquet(cfg.processed_dir / "ratings.parquet")
        self.bouts = pd.read_parquet(cfg.processed_dir / "bouts.parquet")

        # athletes withheld by `publish_min_age`, derived independently of the builder
        year = int(self.meta["as_of"][:4]) if self.meta["as_of"] else 0
        too_young = self.athletes["birth_year"] >= year - cfg.publish_min_age
        self.withheld: set[str] = set(self.athletes.loc[too_young, "athlete_id"]) \
            if cfg.publish_min_age > 0 else set()

    def history(self, aid: str) -> dict[str, Any]:
        return load(self.data / "history" / f"history_{aid}.json")

    def fest(self, fid: int) -> dict[str, Any]:
        return load(self.data / "fests" / f"fest_{fid}.json")


def _clean_env() -> pytest.MonkeyPatch:
    mp = pytest.MonkeyPatch()
    for var in [v for v in os.environ if v.startswith("SCHWINGEN_")]:
        mp.delenv(var)
    return mp


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Site:
    root = tmp_path_factory.mktemp("site-sample")
    mp = _clean_env()
    mp.setenv("SCHWINGEN_DIST_DIR", str(root / "dist"))
    try:
        assert cli.main(["elo", "--sample", "--data-dir", str(root / "data")]) == 0
        assert cli.main(["build", "--sample", "--data-dir", str(root / "data")]) == 0
        cfg = load_config({"sample": True, "data_dir": root / "data"})
    finally:
        mp.undo()
    return Site(root / "dist", cfg)


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> Site:
    cfg = load_config(env={k: v for k, v in os.environ.items() if k == "SCHWINGEN_DATA_DIR"})
    if not all((cfg.processed_dir / f"{n}.parquet").is_file() for n in sb.INPUTS):
        pytest.skip(f"real data not available ({cfg.processed_dir})")
    if len(pd.read_parquet(cfg.processed_dir / "ratings.parquet", columns=["season"])) < 50_000:
        pytest.skip("data/processed holds only a partial history")
    dist = tmp_path_factory.mktemp("site-real") / "dist"
    cfg = load_config({"data_dir": cfg.data_dir, "dist_dir": dist}, env={})
    sb.build_site(cfg)
    return Site(dist, cfg)


@pytest.fixture(params=["sample", "real"])
def site(request: pytest.FixtureRequest) -> Site:
    return request.getfixturevalue(request.param)


# --------------------------------------------------------------------------- safe JSON
def test_dumps_never_emits_nan_and_unwraps_numpy() -> None:
    import datetime as dt
    obj = {"a": float("nan"), "b": np.float64("inf"), "c": np.int32(3), "d": pd.NA,
           "e": [np.nan, 1.5, None, np.bool_(True)], "f": dt.date(2024, 7, 1), "g": "Zürich"}
    text = sb.dumps(obj)
    assert text == '{"a":null,"b":null,"c":3,"d":null,"e":[null,1.5,null,true],' \
                   '"f":"2024-07-01","g":"Zürich"}'
    assert "NaN" not in text and "Infinity" not in text


@pytest.mark.parametrize("flags, schlussgang, expected", [
    ("", None, 0),
    ("", False, 0),                      # "not marked" is not "no Schlussgang"
    ("", True, sb.B_SCHLUSSGANG),
    ("extra_bout", None, sb.B_EXTRA),
    ("grade_missing", None, sb.B_NO_GRADE),
    ("one_sided", True, sb.B_NO_GRADE | sb.B_SCHLUSSGANG),
    ("gang_uncertain:1/2,gang_collision", None, sb.B_GANG_UNCERTAIN),
    ("column_ambiguous", None, 0),
])
def test_bout_flags(flags: str, schlussgang: Any, expected: int) -> None:
    assert sb._bout_flags(flags, schlussgang) == expected


def test_names_are_exported_verbatim_json_escaping_only() -> None:
    # HTML escaping is the pages' job (app.js `esc`); the JSON must stay loss-free
    text = sb.dumps({"name": 'Z\'Rotz <b>"Elias"</b> & Co'})
    assert json.loads(text)["name"] == 'Z\'Rotz <b>"Elias"</b> & Co'


# --------------------------------------------------------------------------- contracts
def test_top_level_files(site: Site) -> None:
    names = {p.name for p in site.data.iterdir()}
    assert names == {"meta.json", "rankings_latest.json", "athletes.json",
                     "alltime_top200.json", "seasons.json", "festivals.json", "fests",
                     "history"}
    assert (site.dist / sb.BUILD_MARKER).is_file()


def test_meta(site: Site) -> None:
    m = site.meta
    assert m["schema"] == sb.SCHEMA_VERSION and m["empty"] is False
    assert m["sample"] is site.cfg.sample
    assert DATE_RE.match(m["as_of"]) and m["as_of"] == str(site.ratings["date"].max())
    assert set(m["counts"]) == {"athletes", "ranked", "festivals", "festivals_partial",
                                "festivals_missing", "bouts", "history_rows", "withheld",
                                "withheld_ranked"}
    rated = set(site.ar.loc[site.ar["n_bouts"] > 0, "athlete_id"])
    assert m["counts"]["withheld"] == len(site.withheld & rated)
    assert m["counts"]["withheld_ranked"] == len(
        site.withheld & set(site.ar.loc[site.ar["ranked"], "athlete_id"]))
    assert m["publish"] == {
        "min_age": 18, "noindex": True,
        "withheld_from_birth_year": int(m["as_of"][:4]) - 18}
    assert m["contact"] is None  # no address unless the owner configures one
    statuses = [r[8] for r in site.festivals["rows"]]
    assert m["counts"]["festivals_partial"] == statuses.count("partial")
    assert m["counts"]["festivals_missing"] == statuses.count("none")
    assert m["counts"]["festivals"] == len(statuses) - statuses.count("none")
    assert m["counts"]["athletes"] == len(site.search["rows"])
    assert m["counts"]["ranked"] == len(site.rankings["rows"])
    model = m["model"]
    assert model["delta"] == site.cfg.season_reversion_delta
    assert model["k"]["ESAF"] == site.cfg.elo_k_scale * site.cfg.k_factors["ESAF"]
    assert model["provisional_min_bouts"] == site.cfg.provisional_min_bouts
    assert "built" not in m and "timestamp" not in m  # deterministic builds


def test_rankings_latest(site: Site) -> None:
    assert site.rankings["cols"] == sb.RANKING_COLS
    rows = table(site.rankings)
    ranked = site.ar[site.ar["ranked"] & ~site.ar["athlete_id"].isin(site.withheld)]
    assert len(rows) == len(ranked) > 0
    # places among the published athletes, in the engine's order
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert [r["id"] for r in rows] == list(ranked.sort_values(["rank", "athlete_id"])["athlete_id"])
    assert not {r["id"] for r in rows} & site.withheld
    assert [r["rating"] for r in rows] == sorted((r["rating"] for r in rows), reverse=True)
    by_id = site.ar.set_index("athlete_id")
    for r in rows:
        a = by_id.loc[r["id"]]
        assert bool(a["ranked"]) and not bool(a["provisional"]) and a["n_bouts"] > 0
        assert "not_a_name" not in a["identity_flags"]
        assert r["rating"] == round(a["rating"]) and r["unc"] == int(a["identity_uncertain"])
        assert DATE_RE.match(r["last"]) and r["idle"] == int(a["days_inactive"]) >= 0
        assert r["tv"] in TEILVERBAENDE and isinstance(r["name"], str) and r["name"]
        assert r["by"] is None or 1940 < r["by"] < 2020


def test_search_index(site: Site) -> None:
    assert site.search["cols"] == sb.SEARCH_COLS
    rows = table(site.search)
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids) and all(ID_RE.match(i) for i in ids)
    by_id = site.ar.set_index("athlete_id")
    expected = {a for a in by_id.index if by_id.at[a, "n_bouts"] > 0
                and "not_a_name" not in by_id.at[a, "identity_flags"]} - site.withheld
    assert set(ids) == expected
    shown_rank = {r[1]: r[0] for r in site.rankings["rows"]}
    assert [(r["name"].casefold(), r["id"]) for r in rows] == \
        sorted((r["name"].casefold(), r["id"]) for r in rows)
    for r in rows:
        a = by_id.loc[r["id"]]
        assert bool(r["flags"] & sb.F_RANKED) == bool(a["ranked"])
        assert r["rank"] == shown_rank.get(r["id"])  # the same rank wherever it is shown
        assert bool(r["flags"] & sb.F_UNCERTAIN) == bool(a["identity_uncertain"])
        assert bool(r["flags"] & sb.F_INACTIVE) == ("inactive" in a["provisional_reason"])
        assert bool(r["flags"] & sb.F_FEW_BOUTS) == ("few_bouts" in a["provisional_reason"])
        assert r["first"] <= r["last"] and isinstance(r["rating"], int)
        # every search hit has a profile file
    assert {p.name for p in (site.data / "history").iterdir()} == \
        {f"history_{i}.json" for i in ids}


def test_alltime_top200(site: Site) -> None:
    assert site.alltime["cols"] == sb.ALLTIME_COLS
    rows = table(site.alltime)
    with_peak = site.ar[site.ar["rating_peak"].notna() & (site.ar["n_bouts"] > 0)
                        & ~site.ar["athlete_id"].isin(site.withheld)]
    assert len(rows) == min(sb.ALLTIME_TOP_N, len(with_peak))
    assert [r["pos"] for r in rows] == list(range(1, len(rows) + 1))
    assert [r["peak"] for r in rows] == sorted((r["peak"] for r in rows), reverse=True)
    searchable = {r[0] for r in site.search["rows"]}
    for r in rows:
        assert r["id"] in searchable and DATE_RE.match(r["date"])
        assert isinstance(r["fest_id"], int) and isinstance(r["fest"], str)
        assert int(r["date"][:4]) >= site.cfg.elo_first_ranked_season


def test_seasons(site: Site) -> None:
    s = site.seasons
    assert s["min_bouts"] == site.meta["model"]["season_min_bouts"]
    seasons = s["seasons"]
    years = [x["season"] for x in seasons]
    assert years == sorted(set(site.ratings["season"].astype(int)), reverse=True)
    sr = pd.read_parquet(site.cfg.processed_dir / "season_ratings.parquet") \
        .set_index(["athlete_id", "season"])
    for x in seasons:
        assert x["cols"] == sb.SEASON_COLS
        assert x["status"] in {"ok", "current", "thin", "burn_in"}
        rows = table(x)
        assert len(rows) <= sb.SEASON_TOP_N and len(rows) <= x["n_listed"] <= x["n_athletes"]
        assert [r["rating"] for r in rows] == sorted((r["rating"] for r in rows), reverse=True)
        if x["season"] < site.cfg.elo_first_ranked_season:
            assert x["status"] == "burn_in"
        if x["status"] in ("ok", "current"):
            assert [r["pos"] for r in rows] == list(range(1, len(rows) + 1))
        else:
            assert all(r["pos"] is None for r in rows)
        for r in rows:
            row = sr.loc[(r["id"], x["season"])]
            assert r["bouts"] == row["n_bouts"] >= s["min_bouts"]
            assert not row["provisional"] and r["rating"] == round(row["rating_end"])
        if rows:
            assert x["peak"]["rating"] >= max(r["peak"] for r in rows)
            assert set(x["peak"]) == {"id", "name", "rating", "unc"}
            assert x["peak"]["unc"] == int(site.ar.set_index("athlete_id").at[
                x["peak"]["id"], "identity_uncertain"])
    assert seasons[0]["status"] == "current"


def test_festival_index_and_files(site: Site) -> None:
    assert site.festivals["cols"] == sb.FESTIVAL_COLS
    rows = table(site.festivals)
    assert [(r["date"], r["id"]) for r in rows] == \
        sorted(((r["date"], r["id"]) for r in rows), reverse=True)
    files = {p.name for p in (site.data / "fests").iterdir()}
    assert files == {f"fest_{r['id']}.json" for r in rows if r["status"] != "none"}
    for r in rows:
        assert r["status"] in {"ok", "partial", "unrated", "none"}
        assert r["cat"] in CATEGORIES and DATE_RE.match(r["date"])
        assert (r["bouts"] > 0) == (r["status"] != "none")
    n_bouts = site.bouts.groupby("fest_id").size()
    rated = site.ratings.set_index(["athlete_id", "fest_id"])
    searchable = {r[0] for r in site.search["rows"]}
    sample_ids = [r["id"] for r in rows if r["status"] != "none"]
    for fid in sample_ids[:: max(1, len(sample_ids) // 25)]:
        f = site.fest(fid)
        assert set(f) == {"id", "name", "date", "season", "category", "eidg_type", "location",
                          "url", "status", "n_gaenge", "athletes", "bouts"}
        assert f["athletes"]["cols"] == sb.FEST_ATHLETE_COLS
        assert f["bouts"]["cols"] == sb.FEST_BOUT_COLS
        athletes, bouts = table(f["athletes"]), table(f["bouts"])
        assert len(bouts) == n_bouts[fid]
        w = [0] * len(athletes); d = [0] * len(athletes); l = [0] * len(athletes)
        for b in bouts:
            assert 0 <= b["a"] < len(athletes) and 0 <= b["b"] < len(athletes) and b["a"] != b["b"]
            assert b["res"] in (0, 1, 2) and 1 <= b["gang"] <= 9 and 0 <= b["flags"] < 16
            for g in (b["ga"], b["gb"]):
                assert g is None or 8.0 <= g <= 10.0
            if b["res"] == 0:
                d[b["a"]] += 1; d[b["b"]] += 1
            else:
                win, lose = (b["a"], b["b"]) if b["res"] == 1 else (b["b"], b["a"])
                w[win] += 1; l[lose] += 1
        for i, a in enumerate(athletes):
            assert (a["w"], a["d"], a["l"]) == (w[i], d[i], l[i])
            assert a["id"] is None or a["id"] in searchable
            if a["anon"]:         # under the publication age: the bouts, nothing else
                assert (a["id"], a["name"], a["club"], a["tv"], a["unc"]) == \
                    (None, None, None, None, 0)
            elif a["id"] is None:   # no profile: at most the name is shown
                assert (a["club"], a["tv"], a["before"], a["after"], a["unc"]) == \
                    (None, None, None, None, 0)
            else:
                assert isinstance(a["name"], str) and a["name"]
            if a["id"] is not None and f["status"] != "unrated":
                row = rated.loc[(a["id"], fid)]
                assert a["before"] == round(row["rating_before"])
                assert a["after"] == round(row["rating_after"])
            if f["status"] == "unrated":
                assert a["before"] is None and a["after"] is None


def test_history_files(site: Site) -> None:
    ids = [r[0] for r in site.search["rows"]]
    by_id = site.ar.set_index("athlete_id")
    step = max(1, len(ids) // 150)
    n_rows = 0
    for aid in ids[::step]:
        h = site.history(aid)
        assert set(h) == {"id", "name", "club", "tv", "by", "first", "last", "bouts",
                          "festivals", "record", "rating", "rating_last", "last_date", "idle",
                          "peak", "peak_date", "peak_fest", "rank", "ranked", "provisional",
                          "unc", "unc_rows", "namesakes", "rev", "as_of", "seasons", "history"}
        a = by_id.loc[aid]
        assert h["id"] == aid and h["history"]["cols"] == sb.HISTORY_COLS
        assert h["seasons"]["cols"] == sb.ATHLETE_SEASON_COLS
        rows = table(h["history"])
        assert len(rows) == h["festivals"] == a["n_festivals"] >= 1
        assert sum(r["n"] for r in rows) == h["bouts"] == a["n_bouts"]
        assert sum(h["record"]) == h["bouts"]
        assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
        assert h["rating_last"] == rows[-1]["after"] and h["last_date"] == rows[-1]["date"]
        assert h["rating"] == round(a["rating"], 1)
        assert h["ranked"] == bool(a["ranked"]) and (h["rank"] is not None) == h["ranked"]
        if h["ranked"]:
            assert h["rank"] == next(r[0] for r in site.rankings["rows"] if r[1] == aid)
        assert set(h["provisional"]) <= {"few_bouts", "inactive"}
        assert h["as_of"] == site.meta["as_of"] and h["rev"][:2] == [
            site.cfg.season_reversion_delta, site.cfg.season_reversion_mean]
        for r in rows:
            assert r["cat"] in CATEGORIES and 0 <= r["score"] <= r["n"]
            assert (site.data / "fests" / f"fest_{r['fest_id']}.json").is_file()
        for s in table(h["seasons"]):
            assert s["pos"] is None or s["pos"] >= 1
        for n in h["namesakes"]:
            assert set(n) == {"id", "name", "club", "tv", "by", "first", "last", "unc"}
            assert n["unc"] == int(by_id.at[n["id"], "identity_uncertain"])
            assert n["id"] != aid and n["name"].casefold() == h["name"].casefold()
            assert (site.data / "history" / f"history_{n['id']}.json").is_file()
        n_rows += len(rows)
    assert n_rows > 0
    assert site.meta["counts"]["history_rows"] == int(
        site.ratings["athlete_id"].isin(ids).sum())


def test_reversion_in_history_matches_the_model(site: Site) -> None:
    """The chart draws the 1 April reversion from `rev`: after -> next before."""
    import datetime as dt
    checked = 0
    for aid in [r[1] for r in site.rankings["rows"][:40]]:
        h = site.history(aid)
        delta, mean, month = h["rev"]
        rows = table(h["history"])
        for prev, nxt in zip(rows, rows[1:]):
            d0, d1 = dt.date.fromisoformat(prev["date"]), dt.date.fromisoformat(nxt["date"])
            k = sum(1 for y in range(d0.year, d1.year + 1)
                    if d0 < dt.date(y, month, 1) <= d1)
            want = mean + (prev["after"] - mean) * (1 - delta) ** k
            assert abs(want - nxt["before"]) < 0.2, (aid, prev, nxt)
            checked += k
    assert checked > 0


def test_nothing_private_is_exported(site: Site) -> None:
    files = [site.data / n for n in ("meta.json", "rankings_latest.json", "athletes.json",
                                     "alltime_top200.json", "seasons.json", "festivals.json")]
    files += sorted((site.data / "history").iterdir())[:50]
    files += sorted((site.data / "fests").iterdir())[:20]
    for p in files:
        assert not (keys_of(load(p)) & FORBIDDEN_KEYS), p


def test_every_json_file_parses(sample: Site) -> None:
    files = sorted(sample.data.rglob("*.json"))
    assert len(files) > 100
    for p in files:
        load(p)
        raw = p.read_text(encoding="utf-8")
        assert "NaN" not in raw and "Infinity" not in raw and "\n" not in raw


def test_build_is_deterministic(sample: Site, tmp_path: Path) -> None:
    cfg = load_config({"sample": True, "data_dir": sample.cfg.data_dir,
                       "dist_dir": tmp_path / "again"}, env={})
    sb.build_site(cfg)
    assert tree_hash(tmp_path / "again") == tree_hash(sample.dist)
    sb.build_site(cfg)  # rebuilding over a previous build
    assert tree_hash(tmp_path / "again") == tree_hash(sample.dist)


def _copy_inputs(src: Path, dst: Path, change: Any = None) -> None:
    """Copy the builder's Parquet inputs; ``change(name, df)`` may return a modified frame."""
    dst.mkdir(parents=True)
    for name in sb.INPUTS:
        df = pd.read_parquet(src / f"{name}.parquet")
        if change is not None:
            df = change(name, df)
        df.to_parquet(dst / f"{name}.parquet")


def test_namesakes_of_an_uncertain_athlete_carry_the_marker(tmp_path: Path, sample: Site) -> None:
    """Two athletes of one name, one identity-uncertain: his entry in the other's
    namesake list has `unc` 1 (the page draws the `?` from it), and vice versa 0."""
    a, b = sample.rankings["rows"][0][1], sample.rankings["rows"][1][1]

    def change(name: str, df: pd.DataFrame) -> pd.DataFrame:
        if name == "athletes":
            df.loc[df["athlete_id"].isin([a, b]), "full_name"] = "Muster Hans"
        if name == "athlete_ratings":
            df.loc[df["athlete_id"] == b, "identity_uncertain"] = True
            df.loc[df["athlete_id"] == a, "identity_uncertain"] = False
        return df

    _copy_inputs(sample.cfg.processed_dir, tmp_path / "data" / "processed", change)
    cfg = load_config({"sample": True, "data_dir": tmp_path / "data",
                       "dist_dir": tmp_path / "dist"}, env={})
    hist = sb.build_site(cfg) / "data" / "history"
    ha, hb = load(hist / f"history_{a}.json"), load(hist / f"history_{b}.json")
    assert [(n["id"], n["unc"]) for n in ha["namesakes"]] == [(b, 1)]
    assert [(n["id"], n["unc"]) for n in hb["namesakes"]] == [(a, 0)]
    assert (ha["unc"], hb["unc"]) == (0, 1)
    # ... and on the season-peak link of the start page
    peaks = [s["peak"] for s in load(tmp_path / "dist" / "data" / "seasons.json")["seasons"]
             if s["peak"]]
    assert peaks and all(p["unc"] == int(p["id"] == b) for p in peaks if p["id"] in (a, b))


def test_athlete_without_rating_is_listed_by_name_without_profile(
        tmp_path: Path, sample: Site) -> None:
    """Someone who fought only at an unrated festival (abroad, team event): the festival
    shows his name - no id, club, rating or profile - instead of "name not readable"."""
    fid = int(sample.festivals["rows"][0][0])
    present = {a["id"] for a in table(sample.fest(fid)["athletes"])}
    # everybody whose only festival this is ends up without rated bouts
    victims = {r["id"]: r["name"] for r in table(sample.search)
               if r["id"] in present and sample.history(r["id"])["festivals"] == 1}
    assert len(victims) >= 3

    def change(name: str, df: pd.DataFrame) -> pd.DataFrame:
        if name in ("festivals", "bouts"):      # the festival does not count
            df.loc[df["fest_id"] == fid, "elo_eligible"] = False
        if name == "ratings":
            df = df[df["fest_id"] != fid]
        if name == "season_ratings":
            df = df[~df["athlete_id"].isin(victims)]
        if name == "athlete_ratings":
            sel = df["athlete_id"].isin(victims)
            df.loc[sel, ["rating", "rating_peak"]] = np.nan
            df.loc[sel, ["n_bouts", "n_festivals"]] = 0
            df.loc[sel, "ranked"] = False
        return df

    _copy_inputs(sample.cfg.processed_dir, tmp_path / "data" / "processed", change)
    cfg = load_config({"sample": True, "data_dir": tmp_path / "data",
                       "dist_dir": tmp_path / "dist"}, env={})
    data = sb.build_site(cfg) / "data"
    fest = load(data / "fests" / f"fest_{fid}.json")
    assert fest["status"] == "unrated"
    rows = table(fest["athletes"])
    assert all(r["name"] for r in rows if not r["anon"])          # nobody is "unreadable"
    without = [r for r in rows if r["id"] is None and not r["anon"]]
    assert sorted(r["name"] for r in without) == sorted(victims.values())
    for r in without:
        assert (r["club"], r["tv"], r["before"], r["after"], r["unc"]) == \
            (None, None, None, None, 0)
        assert r["w"] + r["d"] + r["l"] > 0
    searchable = {r[0] for r in load(data / "athletes.json")["rows"]}
    text = "".join(p.read_text(encoding="utf-8") for p in data.rglob("*.json"))
    for victim in victims:
        assert not (data / "history" / f"history_{victim}.json").exists()
        assert victim not in searchable
        assert f'"{victim}"' not in text                          # the id is not published


def test_build_without_data_fails_unless_allowed(tmp_path: Path, sample: Site) -> None:
    cfg = load_config({"data_dir": tmp_path / "nodata", "dist_dir": tmp_path / "dist"}, env={})
    with pytest.raises(sb.EmptyBuildError, match="missing"):
        sb.build_site(cfg)
    assert not (tmp_path / "dist").exists() and not (tmp_path / "nodata").exists()
    # a previous build survives a failed one
    old = load_config({"sample": True, "data_dir": sample.cfg.data_dir,
                       "dist_dir": tmp_path / "dist"}, env={})
    sb.build_site(old)
    before = tree_hash(tmp_path / "dist")
    with pytest.raises(sb.EmptyBuildError):
        sb.build_site(cfg)
    assert tree_hash(tmp_path / "dist") == before
    # one missing file is enough
    part = tmp_path / "part" / "processed"
    _copy_inputs(sample.cfg.processed_dir, part)
    (part / "season_ratings.parquet").unlink()
    with pytest.raises(sb.EmptyBuildError, match="season_ratings.parquet missing"):
        sb.build_site(load_config({"data_dir": tmp_path / "part",
                                   "dist_dir": tmp_path / "d2"}, env={}))
    # all files present but no rating rows (e.g. `elo` on an empty database)
    _copy_inputs(sample.cfg.processed_dir, tmp_path / "norows" / "processed",
                 lambda name, df: df.iloc[0:0] if name == "ratings" else df)
    cfg0 = load_config({"data_dir": tmp_path / "norows", "dist_dir": tmp_path / "d3"}, env={})
    with pytest.raises(sb.EmptyBuildError, match="no rows"):
        sb.build_site(cfg0)
    assert load(sb.build_site(cfg0, allow_empty=True) / "data" / "meta.json")["empty"] is True


def test_build_allow_empty_writes_an_empty_site(tmp_path: Path) -> None:
    cfg = load_config({"data_dir": tmp_path / "nodata", "dist_dir": tmp_path / "dist"}, env={})
    dist = sb.build_site(cfg, allow_empty=True)
    meta = load(dist / "data" / "meta.json")
    assert meta["empty"] is True and meta["as_of"] is None
    assert load(dist / "data" / "rankings_latest.json")["rows"] == []
    assert load(dist / "data" / "athletes.json")["cols"] == sb.SEARCH_COLS
    assert load(dist / "data" / "seasons.json")["seasons"] == []
    assert not (tmp_path / "nodata").exists()  # build never creates data


def test_unexportable_athletes_are_not_linked(tmp_path: Path, sample: Site) -> None:
    """A `not_a_name` row keeps its bouts in the festival but gets no id, name or file."""
    victim = sample.rankings["rows"][0][1]
    victim_name = sample.rankings["rows"][0][2]
    assert sum(1 for r in sample.search["rows"] if r[1] == victim_name) == 1

    def change(name: str, df: pd.DataFrame) -> pd.DataFrame:
        if name == "athletes":
            df.loc[df["athlete_id"] == victim, "evidence"] = "not_a_name"
        if name == "athlete_ratings":
            df.loc[df["athlete_id"] == victim, "identity_flags"] = "not_a_name"
        return df

    _copy_inputs(sample.cfg.processed_dir, tmp_path / "data" / "processed", change)
    cfg = load_config({"sample": True, "data_dir": tmp_path / "data",
                       "dist_dir": tmp_path / "dist"}, env={})
    dist = sb.build_site(cfg)
    assert not (dist / "data" / "history" / f"history_{victim}.json").exists()
    assert victim not in {r[0] for r in load(dist / "data" / "athletes.json")["rows"]}
    # stays ranked in the Parquet file (we did not touch `ranked`) but must not be listed
    assert victim not in {r[1] for r in load(dist / "data" / "rankings_latest.json")["rows"]}
    text = "".join(p.read_text(encoding="utf-8") for p in (dist / "data").rglob("*.json"))
    assert victim not in text
    # in his festivals the row stays (its bouts count) but without a name
    assert json.dumps(victim_name, ensure_ascii=False) not in text
    fid = sample.history(victim)["history"]["rows"][0][1]
    nameless = [a for a in table(load(dist / "data" / "fests" / f"fest_{fid}.json")["athletes"])
                if a["name"] is None]
    assert len(nameless) == 1 and nameless[0]["id"] is None


# --------------------------------------------------------------------------- publication
def _young_site(tmp_path: Path, sample: Site, **cfg_kw: Any) -> tuple[Path, list[str], Config]:
    """The sample site with the athletes ranked 2nd and 4th made 17 at the data date."""
    young = [sample.rankings["rows"][1][1], sample.rankings["rows"][3][1]]
    year = int(sample.meta["as_of"][:4])

    def change(name: str, df: pd.DataFrame) -> pd.DataFrame:
        if name == "athletes":
            df.loc[df["athlete_id"] == young[0], "birth_year"] = year - 17
            df.loc[df["athlete_id"] == young[1], "birth_year"] = year - 18  # 17 or 18
            # 19 in the data year: certainly of age; unknown birth year: cannot be filtered
            df.loc[df["athlete_id"] == sample.rankings["rows"][0][1], "birth_year"] = year - 19
            df.loc[df["athlete_id"] == sample.rankings["rows"][2][1], "birth_year"] = np.nan
        return df

    _copy_inputs(sample.cfg.processed_dir, tmp_path / "data" / "processed", change)
    cfg = load_config({"sample": True, "data_dir": tmp_path / "data",
                       "dist_dir": tmp_path / "dist", **cfg_kw}, env={})
    return sb.build_site(cfg), young, cfg


def test_min_birth_year_withheld() -> None:
    assert sb.min_birth_year_withheld("2026-09-27", 18) == 2008
    assert sb.min_birth_year_withheld("2026-01-01", 16) == 2010
    assert sb.min_birth_year_withheld("2026-09-27", 0) is None
    assert sb.min_birth_year_withheld(None, 18) is None


def test_athletes_under_the_publication_age_are_not_published(tmp_path: Path,
                                                              sample: Site) -> None:
    dist, young, _ = _young_site(tmp_path, sample)
    data = dist / "data"
    names = [json.dumps(r[2], ensure_ascii=False) for r in sample.rankings["rows"]
             if r[1] in young]
    text = "".join(p.read_text(encoding="utf-8") for p in data.rglob("*.json"))
    for aid, name in zip(young, names):
        assert aid not in text and name not in text      # neither id nor name, anywhere
        assert not (data / "history" / f"history_{aid}.json").exists()
    # ranks are the places among the published athletes: no gap, same order as before
    before = [r[1] for r in sample.rankings["rows"] if r[1] not in young]
    rows = load(data / "rankings_latest.json")["rows"]
    assert [r[1] for r in rows] == before
    assert [r[0] for r in rows] == list(range(1, len(before) + 1))
    assert rows[1][1] == sample.rankings["rows"][2][1]   # unknown birth year stays, now 2nd
    search = {r["id"]: r for r in table(load(data / "athletes.json"))}
    for r in rows:
        assert search[r[1]]["rank"] == r[0] == load(
            data / "history" / f"history_{r[1]}.json")["rank"]
    meta = load(data / "meta.json")
    for key in ("withheld", "withheld_ranked"):
        assert meta["counts"][key] == sample.meta["counts"][key] + 2
    assert meta["counts"]["ranked"] == len(rows) == sample.meta["counts"]["ranked"] - 2
    assert meta["counts"]["athletes"] == sample.meta["counts"]["athletes"] - 2
    assert meta["counts"]["bouts"] == sample.meta["counts"]["bouts"]  # they still count
    # the festivals keep their rows and bouts, without name, club, id
    for aid in young:
        for fid in {r[1] for r in sample.history(aid)["history"]["rows"]}:
            was = table(sample.fest(fid)["athletes"])
            now = table(load(data / "fests" / f"fest_{fid}.json")["athletes"])
            assert len(was) == len(now)
            anon = [a for a in now if a["anon"]]
            assert anon and all((a["id"], a["name"], a["club"], a["tv"]) == (None,) * 4
                                for a in anon)
            old = next(a for a in was if a["id"] == aid)
            assert any((a["w"], a["d"], a["l"], a["pts"], a["after"]) ==
                       (old["w"], old["d"], old["l"], old["pts"], old["after"]) for a in anon)
            assert len(load(data / "fests" / f"fest_{fid}.json")["bouts"]["rows"]) == \
                len(sample.fest(fid)["bouts"]["rows"])
    # nobody else lists them: seasons, peaks, namesakes
    for s in load(data / "seasons.json")["seasons"]:
        assert not {r[1] for r in s["rows"]} & set(young)
        assert s["peak"] is None or s["peak"]["id"] not in young


def test_publish_min_age_zero_publishes_everyone(tmp_path: Path, sample: Site) -> None:
    dist, young, _ = _young_site(tmp_path, sample, publish_min_age=0)
    data = dist / "data"
    ranked = sample.ar[sample.ar["ranked"]].sort_values(["rank", "athlete_id"])
    assert [(r[0], r[1]) for r in load(data / "rankings_latest.json")["rows"]] == \
        [(int(r), a) for r, a in zip(ranked["rank"], ranked["athlete_id"])]  # the engine's ranks
    meta = load(data / "meta.json")
    assert meta["counts"]["withheld"] == 0 and meta["publish"]["withheld_from_birth_year"] is None
    assert meta["counts"]["athletes"] == \
        sample.meta["counts"]["athletes"] + sample.meta["counts"]["withheld"]
    for aid in young:
        assert (data / "history" / f"history_{aid}.json").is_file()
    assert not any(a[-1] for p in (data / "fests").iterdir() for a in load(p)["athletes"]["rows"])


def test_noindex_is_on_every_page_and_switchable(tmp_path: Path, sample: Site) -> None:
    pages = sorted(p.name for p in sample.dist.glob("*.html"))
    assert len(pages) == 4
    for name in pages:
        html = (sample.dist / name).read_text(encoding="utf-8")
        assert html.count(sb.ROBOTS_META) == 1
        assert html.index("<head>") < html.index(sb.ROBOTS_META) < html.index("</head>")
    robots = (sample.dist / "robots.txt").read_text(encoding="utf-8")
    assert "User-agent: *" in robots and "Disallow: /" in robots
    cfg = load_config({"sample": True, "data_dir": sample.cfg.data_dir, "site_noindex": False,
                       "dist_dir": tmp_path / "indexable"}, env={})
    dist = sb.build_site(cfg)
    assert not (dist / "robots.txt").exists()
    assert not any("robots" in p.read_text(encoding="utf-8") for p in dist.glob("*.html"))
    assert load(dist / "data" / "meta.json")["publish"]["noindex"] is False
    # the source pages stay free of the tag: it is a build setting
    assert not any("robots" in p.read_text(encoding="utf-8") for p in cfg.web_dir.glob("*.html"))


def test_noindex_fails_on_a_page_without_head(tmp_path: Path) -> None:
    (tmp_path / "broken.html").write_text("<p>no head</p>", encoding="utf-8")
    with pytest.raises(ValueError, match="no <head>"):
        sb.apply_indexing(load_config(env={}), tmp_path)


def test_contact_email_only_when_configured(tmp_path: Path, sample: Site) -> None:
    assert sample.meta["contact"] is None
    assert "@" not in (sample.dist / "about.html").read_text(encoding="utf-8")
    cfg = load_config({"sample": True, "data_dir": sample.cfg.data_dir,
                       "contact_email": "kontakt@example.org", "dist_dir": tmp_path / "d"},
                      env={})
    assert load(sb.build_site(cfg) / "data" / "meta.json")["contact"] == "kontakt@example.org"
    assert load_config(env={"SCHWINGEN_CONTACT_EMAIL": "a.b@example.org"}).contact_email \
        == "a.b@example.org"
    for bad in ("not an address", "x@y", "a@b.ch?subject=x", "<a@b.ch>", "a@b.ch\nBcc: c@d.ch"):
        with pytest.raises(ValueError, match="contact_email"):
            load_config({"contact_email": bad}, env={})


def test_publication_defaults_and_validation() -> None:
    cfg = load_config(env={})
    assert (cfg.publish_min_age, cfg.site_noindex, cfg.contact_email) == (18, True, "")
    assert load_config(env={"SCHWINGEN_PUBLISH_MIN_AGE": "0",
                            "SCHWINGEN_SITE_NOINDEX": "false"}).site_noindex is False
    with pytest.raises(ValueError, match="publish_min_age"):
        load_config({"publish_min_age": -1}, env={})


# --------------------------------------------------------------------------- real data only
def test_real_age_filter(real: Site) -> None:
    """The default withholds the athletes born in or after `data year - 18`."""
    c = real.meta["counts"]
    assert c["withheld"] > 300 and 0 < c["withheld_ranked"] < c["withheld"]
    assert all(r["by"] is None or r["by"] < real.meta["publish"]["withheld_from_birth_year"]
               for r in table(real.search))
    assert any(r["by"] is None for r in table(real.search))   # unknown birth years stay
    sample_files = sorted((real.data / "fests").iterdir())[-40:]
    assert any(a[-1] for p in sample_files for a in load(p)["athletes"]["rows"])


def test_real_known_weaknesses_are_represented(real: Site) -> None:
    seasons = {s["season"]: s for s in real.seasons["seasons"]}
    assert seasons[2011]["status"] == "burn_in"
    assert seasons[2020]["status"] == "thin" and seasons[2020]["n_festivals"] < 20
    assert all(seasons[y]["status"] == "ok" for y in (2012, 2016, 2019, 2021, 2025))
    # the season lists need 12 season bouts: no place on a single festival
    assert all(r[8] >= 12 for s in seasons.values() for r in s["rows"])
    # inactive greats: current rating far below the last-active rating and the peak
    hits = [r for r in table(real.search) if r["name"] == "Sempach Matthias"]
    best = max(hits, key=lambda r: r["peak"] or 0)
    h = real.history(best["id"])
    assert "inactive" in h["provisional"] and not h["ranked"]
    assert h["peak"] > h["rating_last"] > h["rating"] and h["idle"] > 2000
    assert len(h["namesakes"]) == len(hits) - 1
    # a ranked name that exists twice lists the other one
    twice = next(r for r in table(real.rankings)
                 if sum(1 for x in table(real.rankings) if x["name"] == r["name"]) > 1)
    assert real.history(twice["id"])["namesakes"]
    # identity-uncertain athletes carry the marker wherever they are listed
    unc = set(real.ar.loc[real.ar["identity_uncertain"], "athlete_id"])
    listed = {r["id"]: r["unc"] for r in table(real.rankings)}
    assert sum(listed.values()) == len(unc & set(listed)) > 0
    assert all(listed[a] == 1 for a in unc & set(listed))


def test_real_uncertain_marker_on_every_namesake_entry(real: Site) -> None:
    """Review fix: 161 of 644 namesake entries pointed to an uncertain athlete unmarked."""
    unc = set(real.ar.loc[real.ar["identity_uncertain"], "athlete_id"])
    entries = marked = 0
    for p in (real.data / "history").iterdir():
        for n in load(p)["namesakes"]:
            entries += 1
            marked += n["unc"]
            assert n["unc"] == int(n["id"] in unc), (p.name, n)
    assert entries > 300 and marked > 50
    for s in real.seasons["seasons"]:
        if s["peak"]:
            assert s["peak"]["unc"] == int(s["peak"]["id"] in unc)


def test_real_unrated_festival_shows_names(real: Site) -> None:
    """Review fix: athletes who only fought at an unrated festival (Newark 2025) were
    labelled "Name nicht lesbar". They are listed by name, without id / profile."""
    unrated = [r["id"] for r in table(real.festivals) if r["status"] == "unrated"]
    assert unrated
    searchable = {r[0] for r in real.search["rows"]}
    names_only = 0
    for fid in unrated:
        for a in table(real.fest(fid)["athletes"]):
            if a["anon"]:   # under the publication age: listed without name
                assert a["id"] is None and a["name"] is None
                continue
            assert isinstance(a["name"], str) and a["name"].strip()
            assert a["id"] is None or a["id"] in searchable
            if a["id"] is None:
                names_only += 1
                assert a["name"] not in {r[1] for r in real.search["rows"]}
    assert names_only > 0


def test_real_namesakes_can_be_told_apart(real: Site) -> None:
    rows = table(real.search)
    seen: dict[tuple[Any, ...], int] = {}
    for r in rows:
        key = (r["name"], r["club"], r["by"], r["tv"], r["first"], r["last"])
        seen[key] = seen.get(key, 0) + 1
    assert [k for k, n in seen.items() if n > 1] == []
    ranked = [(r["name"], r["club"], r["by"], r["tv"]) for r in table(real.rankings)]
    assert len(ranked) == len(set(ranked))


def test_real_payload_sizes(real: Site) -> None:
    size = lambda n: (real.data / n).stat().st_size  # noqa: E731
    assert size("rankings_latest.json") < 300_000
    assert size("athletes.json") < 900_000
    assert size("seasons.json") < 250_000 and size("festivals.json") < 300_000
    biggest = max(p.stat().st_size for p in (real.data / "history").iterdir())
    assert biggest < 40_000
    n_files, n_bytes = sb.dist_stats(real.dist)
    assert n_files < 12_000 and n_bytes < 80_000_000
