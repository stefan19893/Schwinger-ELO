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


def leaks(data: Path, needles: dict[str, str]) -> list[str]:
    """Where the published JSON files contain one of ``needles`` (label -> text), as short
    "<label> in <file>" lines; the labels are ids, never a withheld athlete's name.

    The privacy tests assert on this list and not on ``needle not in text``: when such an
    assertion fails, pytest explains it with a character diff (difflib) of the whole text,
    which does not finish on megabytes of one-line JSON - a real leak would show up in CI
    as a timeout instead of a message."""
    out = []
    for p in sorted(data.rglob("*.json")):
        raw = p.read_text(encoding="utf-8")
        out += [f"{label} in {p.relative_to(data)}" for label, needle in needles.items()
                if needle in raw]
    return out


def no_leak(data: Path, needles: dict[str, str]) -> None:
    found = leaks(data, needles)
    n = len(found)
    assert n == 0, f"{n} leaks in the published data, e.g. {'; '.join(found[:5])}"


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
        # ... and by `publish_unknown_recent_seasons` (no birth year, recent debut)
        self.withheld_unknown: set[str] = set()
        if cfg.publish_min_age > 0 and cfg.publish_unknown_recent_seasons > 0:
            a = self.athletes
            new = a["birth_year"].isna() \
                & (a["first_season"] > year - cfg.publish_unknown_recent_seasons) \
                & ~a["evidence"].fillna("").str.contains("not_a_name")
            self.withheld_unknown = set(a.loc[new, "athlete_id"]) - {
                r for r, f in zip(self.ar["athlete_id"], self.ar["identity_flags"])
                if "not_a_name" in str(f)}
            self.withheld |= self.withheld_unknown

    def history(self, aid: str) -> dict[str, Any]:
        return load(self.data / "history" / f"history_{aid}.json")

    def fest(self, fid: int) -> dict[str, Any]:
        return load(self.data / "fests" / f"fest_{fid}.json")

    def bout_file(self, aid: str) -> dict[str, Any]:
        return load(self.data / "bouts" / f"bouts_{aid}.json")


def side_rows(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows of a `bouts_<id>.json` as dicts, with `fest_id` and the opponent's id."""
    assert obj["cols"] == sb.BOUT_SIDE_COLS
    out = []
    for fid, rows in obj["fests"]:
        for r in rows:
            d = dict(zip(obj["cols"], r), fest_id=fid)
            d["opp"] = obj["opps"][d["opp"]]
            out.append(d)
    return out


def expected_sides(site: "Site", published: set[str]) -> dict[str, list[tuple[Any, ...]]]:
    """Independently of the builder: every athlete's bouts against published opponents as
    (fest_id, gang, opponent, res, own grade, opponent's grade), straight from Parquet."""
    b = site.bouts
    both = b[b["athlete_a_id"].isin(published) & b["athlete_b_id"].isin(published)]
    num = lambda g: None if pd.isna(g) else round(float(g), 2)  # noqa: E731
    out: dict[str, list[tuple[Any, ...]]] = {a: [] for a in published}
    for f, g, a, c, o, ga, gb in zip(both["fest_id"], both["gang_nr"], both["athlete_a_id"],
                                     both["athlete_b_id"], both["outcome"], both["grade_a"],
                                     both["grade_b"]):
        mine = {"WIN_A": 1, "DRAW": 0, "WIN_B": 2}[o]
        theirs = {"WIN_A": 2, "DRAW": 0, "WIN_B": 1}[o]
        out[a].append((int(f), int(g), c, mine, num(ga), num(gb)))
        out[c].append((int(f), int(g), a, theirs, num(gb), num(ga)))
    return out


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
                     "history", "bouts"}
    assert (site.dist / sb.BUILD_MARKER).is_file()


def test_meta(site: Site) -> None:
    m = site.meta
    assert m["schema"] == sb.SCHEMA_VERSION and m["empty"] is False
    assert m["sample"] is site.cfg.sample
    assert DATE_RE.match(m["as_of"]) and m["as_of"] == str(site.ratings["date"].max())
    assert set(m["counts"]) == {"athletes", "ranked", "festivals", "festivals_partial",
                                "festivals_missing", "bouts", "history_rows", "withheld",
                                "withheld_ranked", "withheld_unknown", "rated",
                                "birth_year_known", "bouts_one_sided", "name_only"}
    # the reach of the data, read from the data (the pages quote it instead of fixed years)
    seasons = {int(str(d)[:4]) for d in site.ratings["date"]}
    assert m["first_season"] == min(seasons) and m["last_season"] == max(seasons)
    b = site.bouts[site.bouts["elo_eligible"].astype(bool)]
    one = b["flags"].fillna("").str.contains("unlisted_opponent")
    assert m["counts"]["bouts_one_sided"] == int(one.sum())
    printed = set(b["athlete_a_id"]) | set(b.loc[~one, "athlete_b_id"])
    assert m["counts"]["name_only"] == len(set(b.loc[one, "athlete_b_id"]) - printed
                                           - site.withheld)
    fests = pd.read_parquet(site.cfg.processed_dir / "festivals.parquet")
    regional = fests[(fests["category"] == "Regional") & (fests["n_bouts"] > 0)
                     & fests["elo_eligible"].astype(bool)]
    assert m["first_regional_season"] == (int(regional["year"].min()) if len(regional)
                                          else None)
    rated = set(site.ar.loc[site.ar["n_bouts"] > 0, "athlete_id"])
    assert m["counts"]["withheld"] == len(site.withheld & rated)
    assert m["counts"]["withheld_unknown"] == len(site.withheld_unknown & rated)
    assert m["counts"]["rated"] == m["counts"]["athletes"] + m["counts"]["withheld"]
    assert 0 < m["counts"]["birth_year_known"] <= m["counts"]["rated"]
    assert m["counts"]["withheld_ranked"] == len(
        site.withheld & set(site.ar.loc[site.ar["ranked"], "athlete_id"]))
    year, n = int(m["as_of"][:4]), site.cfg.publish_unknown_recent_seasons
    release = m["publish"].pop("release_next_year")
    assert m["publish"] == {
        "min_age": 18, "noindex": True, "withheld_from_birth_year": year - 18,
        "unknown_recent_seasons": n, "withheld_from_first_season": year - n + 1 if n else None}
    m["publish"]["release_next_year"] = release
    # the cohort that is certainly 18 next year, counted independently of the builder
    a = site.athletes[site.athletes["athlete_id"].isin(site.withheld & rated)]
    freed = set(a.loc[(a["birth_year"] == year - 18)
                      | (a["birth_year"].isna() & (a["first_season"] == year - n + 1)),
                      "athlete_id"])
    assert release == {"athletes": len(freed), "ranked": len(
        freed & set(site.ar.loc[site.ar["ranked"], "athlete_id"]))}
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
    assert site.search["cols"] == sb.SEARCH_COLS and set(site.search) == {"cols", "rows", "twins"}
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
            if a["anon"]:   # not certainly of age: the bouts, nothing else - no rating
                assert (a["id"], a["name"], a["club"], a["tv"], a["before"], a["after"],
                        a["exp"], a["unc"]) == (None, None, None, None, None, None, None, 0)
            elif a["id"] is None:   # no profile: at most the name is shown
                assert (a["club"], a["tv"], a["before"], a["after"], a["exp"], a["unc"]) == \
                    (None, None, None, None, None, 0)
            else:
                assert isinstance(a["name"], str) and a["name"]
            if a["id"] is not None and f["status"] != "unrated":
                # the values of the athlete's history file (one decimal), expected score too
                row = rated.loc[(a["id"], fid)]
                assert a["before"] == round(row["rating_before"], 1)
                assert a["after"] == round(row["rating_after"], 1)
                assert a["exp"] == round(row["expected"], 1)
            if f["status"] == "unrated":
                assert a["before"] is None and a["after"] is None and a["exp"] is None


def test_festival_contributions(site: Site) -> None:
    """Phase 10 task 7b: every festival file, every bout. `d` is the engine's contribution
    for athlete `a` (one decimal) exactly at the rated bouts between two published
    athletes and null everywhere else; the file's bouts are the festival's bouts; and for
    a published athlete without hidden bouts the contributions add up to his change."""
    br = pd.read_parquet(site.cfg.processed_dir / "bout_ratings.parquet")
    side_a = br[br["side"] == "A"]
    delta = dict(zip(side_a["bout_id"], side_a["delta"]))
    published = {r[0] for r in site.search["rows"]}
    want: dict[int, list[tuple[Any, ...]]] = {}
    b = site.bouts
    for bid, fid, gang, a, c, ok in zip(b["bout_id"], b["fest_id"], b["gang_nr"],
                                        b["athlete_a_id"], b["athlete_b_id"],
                                        b["elo_eligible"]):
        both = a in published and c in published
        want.setdefault(int(fid), []).append(
            (int(gang), a if a in published else None, c if c in published else None,
             round(delta[bid], 1) + 0.0 if ok and both else None))
    n_listed = n_hidden = n_sums = n_files = 0
    for p in sorted((site.data / "fests").iterdir()):
        f = load(p)
        ids = [r[0] for r in f["athletes"]["rows"]]
        got = sorted(((r[0], ids[r[1]], ids[r[2]], r[7]) for r in f["bouts"]["rows"]),
                     key=repr)
        assert got == sorted(want[f["id"]], key=repr), f["id"]
        n_files += 1
        athletes = table(f["athletes"])
        total = [0.0] * len(athletes)
        hidden = [0] * len(athletes)
        for r in f["bouts"]["rows"]:
            assert r[7] is None or (isinstance(r[7], float) and repr(r[7]) != "-0.0")
            for i, sign in ((r[1], 1), (r[2], -1)):
                if r[7] is None:
                    hidden[i] += 1
                else:
                    total[i] += sign * r[7]
            n_listed += r[7] is not None
            n_hidden += r[7] is None
        if f["status"] == "unrated":
            assert all(r[7] is None for r in f["bouts"]["rows"])
            continue
        for a, t, h in zip(athletes, total, hidden):
            if a["anon"] or a["id"] is None:
                continue
            if h == 0:      # rounding: one decimal per bout and per rating
                n = a["w"] + a["d"] + a["l"]
                assert abs(t - (a["after"] - a["before"])) <= 0.05 * n + 0.1 + 1e-9, f["id"]
                n_sums += 1
    assert n_files == len(want) and n_listed > 0 and n_sums > 0
    if not site.cfg.sample:
        assert n_hidden > 20_000          # the real site has withheld athletes in bouts


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
                          "unc", "unc_rows", "namesakes", "rev", "as_of", "build", "seasons",
                          "history"}
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
            assert set(n) == {"id", "name", "club", "tv", "by", "first", "last", "unc",
                                  "nf"}
            assert n["unc"] == int(by_id.at[n["id"], "identity_uncertain"])
            assert n["id"] != aid and n["name"].casefold() == h["name"].casefold()
            assert (site.data / "history" / f"history_{n['id']}.json").is_file()
        n_rows += len(rows)
    assert n_rows > 0
    assert site.meta["counts"]["history_rows"] == int(
        site.ratings["athlete_id"].isin(ids).sum())


def test_bout_files(site: Site) -> None:
    """`bouts/bouts_<id>.json`: one per published athlete, his bouts against published
    opponents from his side - compared with the Parquet bouts for every athlete."""
    published = {r[0] for r in site.search["rows"]}
    files = {p.name for p in (site.data / "bouts").iterdir()}
    assert files == {f"bouts_{a}.json" for a in published}      # also for an empty list
    want = expected_sides(site, published)
    index = {r["id"]: (r["name"], bool(r["flags"] & sb.F_UNCERTAIN)) for r in table(site.search)}
    fest_date = {r[0]: r[2] for r in site.festivals["rows"]}
    unrated_fests = {r[0] for r in site.festivals["rows"] if r[8] == "unrated"}
    rated = site.ratings.groupby("athlete_id")["fest_id"].agg(lambda s: {int(x) for x in s})
    key = lambda r: (r[0], r[1], r[2])  # noqa: E731
    n_sides = n_unrated = 0
    for aid in sorted(published):
        obj = site.bout_file(aid)
        assert set(obj) == {"id", "build", "opps", "names", "unc", "cols", "fests", "other"}
        assert obj["id"] == aid
        # the opponents' names and identity markers are the search index's, id by id
        assert obj["names"] == [index[o][0] for o in obj["opps"]]
        assert obj["unc"] == [i for i, o in enumerate(obj["opps"]) if index[o][1]]
        assert all(len(r) == len(sb.BOUT_SIDE_COLS) for _, part in obj["fests"] for r in part)
        assert obj["opps"] == sorted(set(obj["opps"])) and aid not in obj["opps"]
        assert set(obj["opps"]) <= published
        rows = side_rows(obj)
        assert set(obj["opps"]) == {r["opp"] for r in rows}      # no unused opponent
        got = [(r["fest_id"], r["gang"], r["opp"], r["res"], r["g"], r["go"]) for r in rows]
        assert sorted(got, key=key) == sorted(want[aid], key=key), aid
        # festivals in order of date, each once; rows by Gang
        fids = [f for f, _ in obj["fests"]]
        assert len(fids) == len(set(fids))
        assert [(fest_date[f], f) for f in fids] == sorted((fest_date[f], f) for f in fids)
        for _, part in obj["fests"]:
            assert part and [r[0] for r in part] == sorted(r[0] for r in part)
        for r in rows:
            assert r["res"] in (0, 1, 2) and 0 <= r["flags"] < 32
            assert bool(r["flags"] & sb.B_UNRATED) == (r["fest_id"] in unrated_fests)
            assert (r["d"] is None) == bool(r["flags"] & sb.B_UNRATED)   # rated = has a value
            n_unrated += bool(r["flags"] & sb.B_UNRATED)
            if r["g"] is None or r["go"] is None:
                assert r["flags"] & (sb.B_EXTRA | sb.B_NO_GRADE)
        # festivals the history file cannot name are named here
        assert obj["other"]["cols"] == sb.OTHER_FEST_COLS
        other = table(obj["other"])
        assert [o["id"] for o in other] == [f for f in fids if f not in rated.get(aid, set())]
        for o in other:
            assert o["name"] and o["date"] == fest_date[o["id"]]
        n_sides += len(rows)
    assert n_sides > 0 and n_sides % 2 == 0
    if not site.cfg.sample:
        assert n_unrated > 0 and n_sides > 800_000


def test_bout_contributions(site: Site) -> None:
    """`d` of every row is the engine's contribution for that bout and side
    (`bout_ratings.parquet`, one decimal), for every athlete; per festival the listed
    contributions plus nothing else explain `after - before` exactly when the athlete met
    published opponents only - and the file says nothing about the other bouts."""
    published = {r[0] for r in site.search["rows"]}
    br = pd.read_parquet(site.cfg.processed_dir / "bout_ratings.parquet")
    mine = br[br["athlete_id"].isin(published)]
    shown = mine[mine["opponent_id"].isin(published)]
    want: dict[tuple[str, int], list[tuple[int, str, float]]] = {}
    for a, f, g, o, d in zip(shown["athlete_id"], shown["fest_id"], shown["gang_nr"],
                             shown["opponent_id"], shown["delta"]):
        want.setdefault((a, int(f)), []).append((int(g), o, round(float(d), 1) + 0.0))
    n_all = mine.groupby(["athlete_id", "fest_id"]).size().to_dict()
    change = {(a, int(f)): (b, c) for a, f, b, c in zip(
        site.ratings["athlete_id"], site.ratings["fest_id"], site.ratings["rating_before"],
        site.ratings["rating_after"]) if a in published}
    n_rows = n_exact = n_partial = wrong = off = 0
    seen: set[tuple[str, int]] = set()
    for aid in sorted(published):
        by_fest: dict[int, list[dict[str, Any]]] = {}
        for r in side_rows(site.bout_file(aid)):
            if r["d"] is not None:
                by_fest.setdefault(r["fest_id"], []).append(r)
        for fid, rows in by_fest.items():
            seen.add((aid, fid))
            got = sorted((r["gang"], r["opp"], r["d"]) for r in rows)
            wrong += got != sorted(want.get((aid, fid), []))
            n_rows += len(rows)
            assert all(isinstance(r["d"], float) and -200 < r["d"] < 200 for r in rows)
            before, after = change[(aid, fid)]
            if len(rows) == n_all[(aid, fid)]:          # no hidden bout at this festival
                n_exact += 1
                # each value is rounded to 0.05, so is nothing else missing
                off += abs(sum(r["d"] for r in rows) - (after - before)) > 0.05 * len(rows) + 1e-6
            else:
                n_partial += 1
    # counts, not ids: an id is a name
    assert wrong == 0, f"{wrong} athlete-festivals differ from bout_ratings.parquet"
    assert off == 0, f"{off} festivals whose contributions do not add up to the change"
    assert seen == set(want), f"{len(seen ^ set(want))} athlete-festivals missing or extra"
    assert n_rows == len(shown) > 0 and n_exact > 0
    if not site.cfg.sample:
        assert n_rows > 900_000 and n_partial > 10_000


def test_bout_files_agree_with_the_festival_files(site: Site) -> None:
    """The same bout, read from either athlete's file and from the festival file."""
    ids = [r[1] for r in site.rankings["rows"]][:25]
    checked = 0
    for aid in ids:
        for r in side_rows(site.bout_file(aid)):
            mirror = [m for m in side_rows(site.bout_file(r["opp"]))
                      if m["opp"] == aid and m["fest_id"] == r["fest_id"]
                      and m["gang"] == r["gang"]]
            assert len(mirror) == 1, (aid, r)
            m = mirror[0]
            assert (m["res"], m["g"], m["go"], m["flags"]) == \
                ({0: 0, 1: 2, 2: 1}[r["res"]], r["go"], r["g"], r["flags"])
            # what one gains the other loses (zero-sum update), also after rounding
            assert (m["d"] is None and r["d"] is None) or m["d"] == -r["d"], (aid, r)
            checked += 1
        fid = site.history(aid)["history"]["rows"][-1][1]
        f = site.fest(fid)
        names = [a[0] for a in f["athletes"]["rows"]]
        me = names.index(aid)
        in_fest = sorted(
            (b[0], names[b[2] if b[1] == me else b[1]],
             0 if b[3] == 0 else (1 if (b[3] == 1) == (b[1] == me) else 2),
             b[4] if b[1] == me else b[5], b[5] if b[1] == me else b[4], b[6])
            for b in f["bouts"]["rows"]
            if me in (b[1], b[2]) and names[b[2] if b[1] == me else b[1]] is not None)
        mine = sorted((r["gang"], r["opp"], r["res"], r["g"], r["go"], r["flags"] & 15)
                      for r in side_rows(site.bout_file(aid)) if r["fest_id"] == fid)
        assert mine == in_fest, (aid, fid)
    assert checked > 100


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
    files += sorted((site.data / "bouts").iterdir())[:50]
    for p in files:
        assert not (keys_of(load(p)) & FORBIDDEN_KEYS), p


def test_every_json_file_parses(sample: Site) -> None:
    files = sorted(sample.data.rglob("*.json"))
    assert len(files) > 100
    for p in files:
        load(p)
        raw = p.read_text(encoding="utf-8")
        bad = [t for t in ("NaN", "Infinity", "\n") if t in raw]   # not `not in raw`: see leaks()
        assert bad == [], p.name


def test_build_is_deterministic(sample: Site, tmp_path: Path) -> None:
    cfg = load_config({"sample": True, "data_dir": sample.cfg.data_dir,
                       "dist_dir": tmp_path / "again"}, env={})
    sb.build_site(cfg)
    assert tree_hash(tmp_path / "again") == tree_hash(sample.dist)
    sb.build_site(cfg)  # rebuilding over a previous build
    assert tree_hash(tmp_path / "again") == tree_hash(sample.dist)


def test_build_stamp_is_one_value_in_every_per_athlete_file(site: Site) -> None:
    """History and bouts file of an athlete are fetched separately; the page combines them
    only when they carry the same stamp. One build writes one stamp everywhere."""
    stamp = site.meta["build"]
    assert isinstance(stamp, str) and len(stamp) == 12 and int(stamp, 16) >= 0
    n = 0
    for folder in ("history", "bouts"):
        for p in (site.data / folder).iterdir():
            other = load(p)["build"] != stamp
            assert not other, folder
            n += 1
    assert n == 2 * len(site.search["rows"]) > 0


def test_build_stamp_follows_the_inputs_and_the_settings(sample: Site, tmp_path: Path) -> None:
    """Not a clock: the same inputs give the same stamp (`test_build_is_deterministic`
    compares whole trees); other data or other publication settings give another one."""
    def stamp(**overrides: Any) -> str:
        cfg = load_config({"sample": True, "data_dir": sample.cfg.data_dir,
                           "dist_dir": tmp_path / "dist", **overrides}, env={})
        return load(sb.build_site(cfg) / "data" / "meta.json")["build"]

    assert stamp() == sample.meta["build"]
    assert stamp(publish_min_age=sample.cfg.publish_min_age + 1) != sample.meta["build"]

    def rename(name: str, df: pd.DataFrame) -> pd.DataFrame:
        if name == "festivals":
            df = df.copy()
            df.loc[df.index[0], "name"] = "Anderes Fest"
        return df

    _copy_inputs(sample.cfg.processed_dir, tmp_path / "other" / "processed", rename)
    assert stamp(data_dir=tmp_path / "other") != sample.meta["build"]


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
    for victim in victims:
        assert not (data / "bouts" / f"bouts_{victim}.json").exists()
        assert not (data / "history" / f"history_{victim}.json").exists()
        assert not searchable & {victim}, victim
    no_leak(data, {f"id {v}": f'"{v}"' for v in victims})        # the id is not published


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
    # bout_ratings.parquet from another run (an old state): not an empty site either
    _copy_inputs(sample.cfg.processed_dir, tmp_path / "stale" / "processed",
                 lambda name, df: df.iloc[: len(df) // 2] if name == "bout_ratings" else df)
    stale = load_config({"data_dir": tmp_path / "stale", "dist_dir": tmp_path / "d4"}, env={})
    for allow in (False, True):
        with pytest.raises(sb.EmptyBuildError, match="run `python -m src.cli elo` first"):
            sb.build_site(stale, allow_empty=allow)
    assert not (tmp_path / "d4").exists()
    (tmp_path / "stale" / "processed" / "bout_ratings.parquet").unlink()
    with pytest.raises(sb.EmptyBuildError, match="bout_ratings.parquet missing"):
        sb.build_site(stale)
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
    assert not (dist / "data" / "bouts" / f"bouts_{victim}.json").exists()
    assert victim not in {r[0] for r in load(dist / "data" / "athletes.json")["rows"]}
    # stays ranked in the Parquet file (we did not touch `ranked`) but must not be listed
    assert victim not in {r[1] for r in load(dist / "data" / "rankings_latest.json")["rows"]}
    # in his festivals the row stays (its bouts count) but without id and name
    no_leak(dist / "data", {f"id {victim}": victim,
                            f"name of {victim}": json.dumps(victim_name, ensure_ascii=False)})
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
    for aid, name in zip(young, names):                  # neither id nor name, anywhere
        no_leak(data, {f"id {aid}": aid, f"name of {aid}": name})
        assert not (data / "history" / f"history_{aid}.json").exists()
    # the comparison data: no file for them, and nobody's opponent list contains them -
    # their bouts are simply absent there (the festival file keeps them, unnamed)
    assert not any((data / "bouts" / f"bouts_{aid}.json").exists() for aid in young)
    dropped = 0
    for p in sorted((data / "bouts").iterdir()):
        now = side_rows(load(p))
        was = side_rows(sample.bout_file(p.stem.removeprefix("bouts_")))
        assert [r for r in was if r["opp"] not in young] == now, p.name
        dropped += len(was) - len(now)
        assert not set(load(p)["opps"]) & set(young)
    assert dropped == sum(len(side_rows(sample.bout_file(a))) for a in young) \
        - 2 * sum(1 for r in side_rows(sample.bout_file(young[0])) if r["opp"] == young[1])
    assert dropped > 0
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
            assert any((a["w"], a["d"], a["l"], a["pts"]) ==
                       (old["w"], old["d"], old["l"], old["pts"]) for a in anon)
            # no rating value on a withheld row: before / after would chain into a history
            assert old["after"] is not None
            assert all(a["before"] is None and a["after"] is None for a in anon)
            assert len(load(data / "fests" / f"fest_{fid}.json")["bouts"]["rows"]) == \
                len(sample.fest(fid)["bouts"]["rows"])
    # nobody else lists them: seasons, peaks, namesakes
    for s in load(data / "seasons.json")["seasons"]:
        assert not {r[1] for r in s["rows"]} & set(young)
        assert s["peak"] is None or s["peak"]["id"] not in young


def test_min_first_season_withheld() -> None:
    assert sb.min_first_season_withheld("2026-09-27", 18, 3) == 2024
    assert sb.min_first_season_withheld("2026-09-27", 18, 1) == 2026
    assert sb.min_first_season_withheld("2026-09-27", 18, 0) is None
    assert sb.min_first_season_withheld("2026-09-27", 0, 3) is None   # age rule off
    assert sb.min_first_season_withheld(None, 18, 3) is None


def test_recent_debutants_without_birth_year_are_withheld(tmp_path: Path,
                                                          sample: Site) -> None:
    """"Not certainly 18" also means: no birth year and new in the last N seasons."""
    year = int(sample.meta["as_of"][:4])
    by_id = sample.athletes.set_index("athlete_id")
    ranked = [r[1] for r in sample.rankings["rows"]]
    new = next(a for a in ranked if by_id.at[a, "first_season"] == year)
    edge_in = next(a for a in ranked if a != new and by_id.at[a, "first_season"] >= year - 1)
    old = next(a for a in ranked if by_id.at[a, "first_season"] < year - 2)
    known = next(a for a in ranked if a not in (new, edge_in, old))

    def change(name: str, df: pd.DataFrame) -> pd.DataFrame:
        if name == "athletes":
            df["birth_year"] = df["birth_year"].where(df["birth_year"] < year - 18, year - 30)
            ix = df.set_index("athlete_id").index
            df.loc[ix.isin([new, edge_in, old]), "birth_year"] = np.nan
            df.loc[ix == edge_in, "first_season"] = year - 2      # third-last season: in
            df.loc[ix == old, "first_season"] = year - 3          # fourth-last: out
            df.loc[ix == known, ["birth_year", "first_season"]] = [year - 25, year]
            rest = ~ix.isin([new, edge_in, old, known]) & df["birth_year"].isna()
            df.loc[rest, "birth_year"] = year - 30
        return df

    _copy_inputs(sample.cfg.processed_dir, tmp_path / "data" / "processed", change)
    kw = {"sample": True, "data_dir": tmp_path / "data"}
    cfg = load_config({**kw, "dist_dir": tmp_path / "dist",
                       "publish_unknown_recent_seasons": 3}, env={})
    data = sb.build_site(cfg) / "data"
    meta = load(data / "meta.json")
    assert meta["counts"]["withheld"] == meta["counts"]["withheld_unknown"] == 2
    assert meta["counts"]["withheld_ranked"] == 2
    assert meta["publish"]["unknown_recent_seasons"] == 3
    assert meta["publish"]["withheld_from_first_season"] == year - 2
    # next year only the debutant of `year - 2` is released
    assert meta["publish"]["release_next_year"] == {"athletes": 1, "ranked": 1}
    for aid in (new, edge_in):
        no_leak(data, {f"id {aid}": aid, f"name of {aid}": json.dumps(
            by_id.at[aid, "full_name"], ensure_ascii=False)})
        assert not (data / "history" / f"history_{aid}.json").exists()
        assert not (data / "bouts" / f"bouts_{aid}.json").exists()
    for aid in (old, known):
        assert (data / "history" / f"history_{aid}.json").is_file()
        assert (data / "bouts" / f"bouts_{aid}.json").is_file()
    assert sum(a[-1] for p in (data / "fests").iterdir()
               for a in load(p)["athletes"]["rows"]) >= 2
    # the rule is a switch of its own, and it is off when the age rule is off
    for extra in ({"publish_unknown_recent_seasons": 0},
                  {"publish_unknown_recent_seasons": 3, "publish_min_age": 0}):
        off = load_config({**kw, "dist_dir": tmp_path / "off", **extra}, env={})
        m = load(sb.build_site(off) / "data" / "meta.json")
        assert m["counts"]["withheld"] == 0
        assert m["publish"]["withheld_from_first_season"] is None
    assert m["publish"]["unknown_recent_seasons"] == 0


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
    assert len(pages) == len(list(sample.cfg.web_dir.glob("*.html"))) >= 4
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
    assert cfg.publish_unknown_recent_seasons == 3
    # the demo data have almost no birth years: off there unless asked for
    assert load_config({"sample": True}, env={}).publish_unknown_recent_seasons == 0
    assert load_config({"sample": True, "publish_unknown_recent_seasons": 2},
                       env={}).publish_unknown_recent_seasons == 2
    with pytest.raises(ValueError, match="publish_unknown_recent_seasons"):
        load_config({"publish_unknown_recent_seasons": -1}, env={})
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
    sample_files = sorted((real.data / "fests").iterdir())[-40:]
    assert any(a[-1] for p in sample_files for a in load(p)["athletes"]["rows"])   # anon


def test_real_unknown_birth_years(real: Site) -> None:
    """Without a birth year: published only when the debut is more than three seasons
    back; and nobody published gives away a withheld namesake of the same club."""
    year = int(real.meta["as_of"][:4])
    rows = table(real.search)
    unknown = [r for r in rows if r["by"] is None]
    assert len(unknown) > 1000                      # the older ones stay
    assert all(r["first"] <= year - 3 for r in unknown)
    assert 0 < real.meta["counts"]["withheld_unknown"] < 30
    a = real.athletes[real.athletes["athlete_id"].isin(real.withheld)]
    hidden = {(n.casefold(), c) for n, c in zip(a["full_name"], a["club"])}
    assert not [r["id"] for r in unknown if (r["name"].casefold(), r["club"]) in hidden]


def test_real_withheld_rows_carry_no_rating(real: Site) -> None:
    n_anon = 0
    for p in (real.data / "fests").iterdir():
        for a in table(load(p)["athletes"]):
            if a["anon"]:
                n_anon += 1
                assert (a["id"], a["name"], a["club"], a["tv"], a["before"], a["after"],
                        a["exp"]) == (None,) * 7, p.name
    assert n_anon > 5000


def test_real_festival_files_give_no_contribution_for_withheld_athletes(real: Site) -> None:
    """The leak test of the festival files (Phase 10 task 7b), on every file of the real
    build and derived from the Parquet files, not from the exporter: no bout with a
    withheld athlete on either side carries a contribution - neither on a listed
    athlete's Gang against him nor on his own row -, every such bout is there without
    one, and nothing else in the file is a number of his rating. Counts only."""
    withheld = real.withheld
    b = real.bouts
    hidden_bouts: dict[int, int] = {}
    for fid, a, c in zip(b["fest_id"], b["athlete_a_id"], b["athlete_b_id"]):
        if a in withheld or c in withheld:
            hidden_bouts[int(fid)] = hidden_bouts.get(int(fid), 0) + 1
    assert sum(hidden_bouts.values()) > 30_000
    n_checked = n_listed_athletes_with_hidden = 0
    for p in sorted((real.data / "fests").iterdir()):
        f = load(p)
        assert set(f["athletes"]["cols"]) == set(sb.FEST_ATHLETE_COLS)
        assert f["bouts"]["cols"] == sb.FEST_BOUT_COLS
        rows = f["athletes"]["rows"]
        anon = {i for i, r in enumerate(rows) if r[-1]}
        with_anon = [r for r in f["bouts"]["rows"] if r[1] in anon or r[2] in anon]
        assert len(with_anon) == hidden_bouts.get(f["id"], 0)
        assert all(r[7] is None for r in with_anon)
        assert all(len(r) == 8 for r in f["bouts"]["rows"])
        # a bout without contribution at a rated festival has a withheld side: nothing
        # else is hidden, so "ohne Einzelwert" on the page means exactly that
        if f["status"] != "unrated":
            assert all(r[7] is not None or r[1] in anon or r[2] in anon
                       for r in f["bouts"]["rows"])
        n_checked += len(with_anon)
        n_listed_athletes_with_hidden += len(
            {i for r in with_anon for i in (r[1], r[2]) if i not in anon})
    assert n_checked == sum(hidden_bouts.values())
    assert n_listed_athletes_with_hidden > 10_000


def test_real_bout_files_name_no_withheld_athlete(real: Site) -> None:
    """Every id in every comparison file is a published athlete, no file exists for a
    withheld one, and no withheld name occurs in the files that carry names (`other`)."""
    published = {r[0] for r in real.search["rows"]}
    assert real.withheld and not (real.withheld & published)
    a = real.athletes[real.athletes["athlete_id"].isin(real.withheld)]
    published_names = {r[1] for r in real.search["rows"]}
    hidden_names = set(a["full_name"]) - published_names        # namesakes may be published
    published_name = {r[0]: r[1] for r in real.search["rows"]}
    assert len(hidden_names) > 300
    ids_seen: set[str] = set()
    for p in (real.data / "bouts").iterdir():
        aid = p.stem.removeprefix("bouts_")
        listed, hidden = aid in published, aid in real.withheld
        assert listed and not hidden, p.name            # booleans: no set of ids in the output
        obj = load(p)
        ids_seen |= set(obj["opps"]) | {obj["id"]}
        # the only strings in the file: ids, column names, names of unrated festivals
        strings = {v for fid_rows in obj["fests"] for r in fid_rows[1] for v in r
                   if isinstance(v, str)}
        assert len(strings) == 0, p.name
        # the opponents' names: one per id, each the published name of that id
        wrong = sum(1 for o, n in zip(obj["opps"], obj["names"], strict=True)
                    if published_name.get(o) != n)
        assert wrong == 0, f"{p.name}: {wrong} opponent names are not the published ones"
        named = sum(1 for row in obj["other"]["rows"] if row[1] in hidden_names)
        assert named == 0, f"{p.name} names a withheld athlete"   # the file, not the name
    extra = sorted(ids_seen - published)
    assert len(extra) == 0, f"{len(extra)} ids outside the search index, e.g. {extra[:3]}"
    # the bouts a published athlete had against withheld ones are absent, not anonymised:
    # the tally of two published athletes cannot include a third person
    b = real.bouts
    mixed = b[b["athlete_a_id"].isin(real.withheld) ^ b["athlete_b_id"].isin(real.withheld)]
    assert len(mixed) > 5000
    both = b[b["athlete_a_id"].isin(published) & b["athlete_b_id"].isin(published)]
    n_sides = sum(len(rows) for p in (real.data / "bouts").iterdir()
                  for _, rows in load(p)["fests"])
    assert n_sides == 2 * len(both) < 2 * (len(b) - len(mixed))


def test_real_bout_files_say_nothing_about_hidden_bouts(real: Site) -> None:
    """A contribution reveals the opponent's rating to whoever knows the formula. So a
    bout against a withheld athlete has no row, no contribution, no count and no Gang in
    the comparison files: per athlete and festival the file holds exactly the bouts
    against published opponents, and no value in it is the contribution of a hidden bout
    in disguise (a remainder, a sum, an expected score)."""
    published = {r[0] for r in real.search["rows"]}
    br = pd.read_parquet(real.cfg.processed_dir / "bout_ratings.parquet")
    mine = br[br["athlete_id"].isin(published)]
    hidden = mine[~mine["opponent_id"].isin(published)]
    assert set(hidden["opponent_id"]) <= real.withheld and len(hidden) > 20_000
    n_shown = mine[mine["opponent_id"].isin(published)].groupby(
        ["athlete_id", "fest_id"]).size().to_dict()
    n_hidden = hidden.groupby(["athlete_id", "fest_id"]).size().to_dict()
    only_hidden = set(n_hidden) - set(n_shown)
    assert len(only_hidden) > 20                 # festivals with hidden opponents only
    extra = leaked = 0
    for p in (real.data / "bouts").iterdir():
        obj = load(p)
        assert set(obj) == {"id", "build", "opps", "names", "unc", "cols", "fests", "other"}, p.name
        assert obj["cols"] == sb.BOUT_SIDE_COLS == ["gang", "opp", "res", "g", "go", "flags", "d"]
        aid = obj["id"]
        for fid, rows in obj["fests"]:
            rated = [r for r in rows if r[6] is not None]
            extra += len(rated) != n_shown.get((aid, fid), 0)
            leaked += (aid, fid) in only_hidden and bool(rated)
    assert extra == 0, f"{extra} festivals list more or fewer bouts than the published ones"
    assert leaked == 0
    # a single hidden bout: its value is not in the file under any column (compared with
    # every number of that festival's rows, at the precision of the file)
    single = hidden[hidden.set_index(["athlete_id", "fest_id"]).index.map(n_hidden.get) == 1]
    hits = checked = 0
    for a, f, d in zip(single["athlete_id"].iloc[::25], single["fest_id"].iloc[::25],
                       single["delta"].iloc[::25]):
        rows = next((rows for fid, rows in real.bout_file(a)["fests"] if fid == int(f)), [])
        numbers = [abs(v) for r in rows for v in (r[6],) if v is not None]
        hits += any(abs(abs(float(d)) - v) < 0.05 for v in numbers) and abs(d) > 0.5
        checked += 1
    # a published bout of the same festival can have the same size by chance
    assert checked > 300 and hits < checked * 0.05, (hits, checked)


def test_real_known_weaknesses_are_represented(real: Site) -> None:
    seasons = {s["season"]: s for s in real.seasons["seasons"]}
    # the burn-in is the first season of the data (2004 since Phase 10, task 6; was 2011)
    first = min(seasons)
    assert first == real.cfg.elo_first_ranked_season - 1 and seasons[first]["status"] == "burn_in"
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


def test_twins_carry_what_is_left_to_tell_them_apart(site: Site) -> None:
    """`twins` holds exactly the athletes whose search row equals another one's in every
    distinguishing field, with their number of festivals and bouts (Phase 10)."""
    rows = table(site.search)
    groups: dict[tuple[Any, ...], list[str]] = {}
    for r in rows:
        groups.setdefault((r["name"].casefold(), r["club"], r["by"], r["tv"], r["first"],
                           r["last"]), []).append(r["id"])
    same = {i for ids in groups.values() if len(ids) > 1 for i in ids}
    twins = site.search["twins"]
    assert set(twins) == same
    by_id = site.ar.set_index("athlete_id")
    for aid, (n_fests, n_bouts) in twins.items():
        assert (n_fests, n_bouts) == (by_id.at[aid, "n_festivals"], by_id.at[aid, "n_bouts"])
        # the namesake list of the profile carries the same number
        assert all(n["nf"] == by_id.at[n["id"], "n_festivals"]
                   for n in site.history(aid)["namesakes"])


def test_real_namesakes_can_be_told_apart(real: Site) -> None:
    """Every search row differs from every other one in what the page shows. Two pairs of
    namesakes of the seasons before 2011 share name, Teilverband and seasons and have
    neither club nor birth year - the sheets print nothing else, and they are two people
    each because both stand in the same sheets. For such athletes the row shows the
    number of festivals (`twins`), which is all that is left; the page says so. The
    assertions give counts only, never a name."""
    rows = table(real.search)
    twins = real.search["twins"]
    seen: dict[tuple[Any, ...], int] = {}
    for r in rows:
        key = (r["name"], r["club"], r["by"], r["tv"], r["first"], r["last"],
               twins.get(r["id"], [None])[0])            # as SE.subline shows the row
        seen[key] = seen.get(key, 0) + 1
    assert sum(1 for n in seen.values() if n > 1) == 0
    assert 0 < len(twins) <= 10          # rare: it must not become the normal answer
    assert all(r["flags"] & sb.F_UNCERTAIN for r in rows if r["id"] in twins)
    ranked = [(r["name"], r["club"], r["by"], r["tv"]) for r in table(real.rankings)]
    assert len(ranked) == len(set(ranked))


def test_real_payload_sizes(real: Site) -> None:
    size = lambda n: (real.data / n).stat().st_size  # noqa: E731
    assert size("rankings_latest.json") < 300_000
    assert size("athletes.json") < 900_000
    assert size("seasons.json") < 250_000 and size("festivals.json") < 300_000
    biggest = max(p.stat().st_size for p in (real.data / "history").iterdir())
    assert biggest < 40_000
    # the comparison data: one on-demand file per athlete, a pair loads two of them
    bout_sizes = [p.stat().st_size for p in (real.data / "bouts").iterdir()]
    # (60 MB since Phase 9, was 50: the files also carry the contribution per bout and
    # the opponents' names, so that the Gang tooltip needs no 590 KB search index)
    # Phase 10, history from 2004: 8,830 athletes instead of 6,306 and careers of up to 23
    # seasons. Largest file 71.7 KB (20.5 KB gzip; 252 festivals, 1,481 bouts), all files
    # 63.7 MB. The bounds follow the data (80 KB / 75 MB): a file is fetched only for an
    # athlete selected on the comparison page, and trimming it would mean dropping the
    # opponents' names again or shortening the ids, i.e. a new contract for 3 KB gzip.
    assert max(bout_sizes) < 80_000 and sum(bout_sizes) < 75_000_000
    n_files, n_bytes = sb.dist_stats(real.dist)
    # 19,739 files / 125 MB with the history (was 14,447 / 103 MB); Pages allows 1 GB
    assert n_files < 24_000 and n_bytes < 160_000_000
