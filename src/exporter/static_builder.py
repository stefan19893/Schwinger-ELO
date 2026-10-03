"""Copies ``web/`` and writes the JSON slices the pages read into ``dist/data/``.

The site is static: every page is plain HTML + JS that fetches these files with
relative URLs. Contracts (``SCHEMA_VERSION``), all compact JSON, ``null`` for missing
values, dates ``YYYY-MM-DD``, tables as ``{"cols": [...], "rows": [[...], ...]}``:

``meta.json``
    ``as_of`` (last rated festival), counts and the model parameters the pages quote.
``rankings_latest.json``
    Every currently ranked athlete (``athlete_ratings.ranked``), by rank.
``athletes.json``
    Search index: one row per exportable athlete.
``alltime_top200.json``
    The 200 highest peak ratings.
``seasons.json``
    Per season the top ``SEASON_TOP_N`` by season-end rating and the season's peak.
``festivals.json``
    Index of all active festivals.
``fests/fest_<fest_id>.json``
    One festival: participants (rating before / after) and bouts.
``history/history_<athlete_id>.json``
    One athlete: profile, season table, rating history per festival.

An athlete is *exportable* when he has rated bouts and is not a ``not_a_name`` row.
Only name, club, Teilverband and birth year are published (no residence, birthday,
licence number or portrait slug).

The build is deterministic: the same Parquet inputs give byte-identical output (no
timestamps; "as of" is the date of the last rated festival).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import REPO_ROOT, Config

log = logging.getLogger(__name__)

_IGNORED = shutil.ignore_patterns(".gitkeep", "__pycache__", "*.pyc", "*.md")

# Written into every build; its presence marks a directory as safe to wipe.
BUILD_MARKER = ".nojekyll"

SCHEMA_VERSION = 1
DATA_DIR = "data"
ALLTIME_TOP_N = 200
SEASON_TOP_N = 100
# A season place needs this many bouts in that season (two festivals); otherwise a
# single festival is enough for a top-20 place (Phase 4 review).
SEASON_MIN_BOUTS = 12
SAMPLE_SEASON_MIN_BOUTS = 4
# A finished season with fewer rated festivals is "thin": listed without places (2020
# had six hall festivals, a normal season has more than a hundred).
THIN_SEASON_FESTIVALS = 20
SAMPLE_THIN_SEASON_FESTIVALS = 1

NOT_A_NAME = "not_a_name"
INPUTS = ("athletes", "athlete_ratings", "bouts", "festivals", "ratings", "season_ratings")

# athletes.json / alltime flags
F_RANKED, F_FEW_BOUTS, F_INACTIVE, F_UNCERTAIN = 1, 2, 4, 8
# bout flags
B_SCHLUSSGANG, B_EXTRA, B_NO_GRADE, B_GANG_UNCERTAIN = 1, 2, 4, 8
# history row flags
H_PROVISIONAL, H_RETURN = 1, 2

RANKING_COLS = ["rank", "id", "name", "club", "tv", "by", "rating", "peak", "last", "idle",
                "bouts", "unc"]
SEARCH_COLS = ["id", "name", "club", "tv", "by", "first", "last", "rating", "peak", "rank",
               "flags"]
ALLTIME_COLS = ["pos", "id", "name", "club", "tv", "by", "peak", "date", "fest_id", "fest",
                "flags"]
SEASON_COLS = ["pos", "id", "name", "club", "tv", "by", "rating", "peak", "bouts", "unc"]
FESTIVAL_COLS = ["id", "name", "date", "cat", "eidg", "loc", "athletes", "bouts", "status"]
FEST_ATHLETE_COLS = ["id", "name", "club", "tv", "before", "after", "w", "d", "l", "pts", "unc"]
FEST_BOUT_COLS = ["gang", "a", "b", "res", "ga", "gb", "flags"]
HISTORY_COLS = ["date", "fest_id", "fest", "cat", "before", "after", "n", "score", "exp",
                "flags"]
ATHLETE_SEASON_COLS = ["season", "rating", "peak", "bouts", "pos"]

PLACEHOLDER_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Schwinger-ELO</title>
</head>
<body>
  <h1>Schwinger-ELO</h1>
  <p>Platzhalter &mdash; das Verzeichnis <code>web/</code> fehlt.</p>
</body>
</html>
"""


# --------------------------------------------------------------------------- JSON
def _clean(v: Any) -> Any:
    """Plain JSON value: numpy scalars unwrapped, NaN / NA / inf -> None, dates -> ISO."""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, (_dt.date, _dt.datetime, pd.Timestamp)):
        return str(v)[:10]
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_clean(x) for x in v]
    return v


def dumps(obj: Any) -> str:
    """Compact, strict JSON (never emits NaN / Infinity)."""
    return json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _write_json(path: Path, obj: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dumps(obj).encode("utf-8")
    path.write_bytes(data)
    return len(data)


def _int(v: Any) -> int | None:
    v = _clean(v)
    return None if v is None else int(round(v))


def _r1(v: Any) -> float | None:
    v = _clean(v)
    return None if v is None else round(float(v), 1)


def _r2(v: Any) -> float | None:
    v = _clean(v)
    return None if v is None else round(float(v), 2)


def _text(v: Any) -> str | None:
    v = _clean(v)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _date(v: Any) -> str | None:
    v = _clean(v)
    return None if v is None else str(v)[:10]


def _flag_set(v: Any, seps: str = ",") -> set[str]:
    s = _text(v)
    if not s:
        return set()
    for sep in seps[1:]:
        s = s.replace(sep, seps[0])
    return {part.split(":")[0].split("=")[0].strip() for part in s.split(seps[0]) if part.strip()}


# --------------------------------------------------------------------------- inputs
@dataclass
class Inputs:
    athletes: pd.DataFrame
    athlete_ratings: pd.DataFrame
    bouts: pd.DataFrame
    festivals: pd.DataFrame
    ratings: pd.DataFrame
    season_ratings: pd.DataFrame


def load_inputs(processed_dir: Path) -> Inputs | None:
    """Read the Parquet files of ``clean`` and ``elo``; ``None`` when any is missing."""
    paths = {name: processed_dir / f"{name}.parquet" for name in INPUTS}
    missing = [p.name for p in paths.values() if not p.is_file()]
    if missing:
        log.warning("build: %s missing in %s - writing a site without data "
                    "(run `python -m src.cli clean` and `elo` first)",
                    ", ".join(missing), processed_dir)
        return None
    return Inputs(**{name: pd.read_parquet(p) for name, p in paths.items()})


@dataclass
class _Person:
    """What the site may show about one athlete."""
    id: str
    name: str
    club: str | None
    tv: str | None
    by: int | None
    first: int | None
    last: int | None
    exportable: bool
    unc: int


def _people(inp: Inputs) -> dict[str, _Person]:
    ar = inp.athlete_ratings.set_index("athlete_id")
    out: dict[str, _Person] = {}
    for row in inp.athletes.itertuples(index=False):
        aid = row.athlete_id
        rated = aid in ar.index and _clean(ar.at[aid, "rating"]) is not None \
            and int(ar.at[aid, "n_bouts"]) > 0
        garbage = NOT_A_NAME in _flag_set(row.evidence, ";|")
        if aid in ar.index:
            garbage = garbage or NOT_A_NAME in _flag_set(ar.at[aid, "identity_flags"], ";|")
        out[aid] = _Person(
            id=aid, name=_text(row.full_name) or "?", club=_text(row.club),
            tv=_text(row.sub_association), by=_int(row.birth_year),
            first=_int(row.first_season), last=_int(row.last_season),
            exportable=bool(rated and not garbage),
            unc=int(bool(ar.at[aid, "identity_uncertain"])) if aid in ar.index else 0)
    return out


# --------------------------------------------------------------------------- slices
def _athlete_flags(row: Any) -> int:
    reasons = _flag_set(row.provisional_reason)
    return (F_RANKED * bool(row.ranked) | F_FEW_BOUTS * ("few_bouts" in reasons)
            | F_INACTIVE * ("inactive" in reasons) | F_UNCERTAIN * bool(row.identity_uncertain))


def _rankings(inp: Inputs, people: dict[str, _Person]) -> list[list[Any]]:
    ranked = inp.athlete_ratings[inp.athlete_ratings["ranked"]].sort_values(["rank", "athlete_id"])
    rows = []
    for r in ranked.itertuples(index=False):
        p = people[r.athlete_id]
        if not p.exportable:  # never rank garbage names or athletes without bouts
            continue
        rows.append([_int(r.rank), p.id, p.name, p.club, p.tv, p.by, _int(r.rating),
                     _int(r.rating_peak), _date(r.last_date), _int(r.days_inactive),
                     _int(r.n_bouts), p.unc])
    return rows


def _search_index(inp: Inputs, people: dict[str, _Person]) -> list[list[Any]]:
    rows = []
    for r in inp.athlete_ratings.itertuples(index=False):
        p = people[r.athlete_id]
        if not p.exportable:
            continue
        rows.append([p.id, p.name, p.club, p.tv, p.by, p.first, p.last, _int(r.rating),
                     _int(r.rating_peak), _int(r.rank) if r.ranked else None,
                     _athlete_flags(r)])
    rows.sort(key=lambda x: (x[1].casefold(), x[0]))
    return rows


def _alltime(inp: Inputs, people: dict[str, _Person], fest_names: dict[int, str]) -> list[list[Any]]:
    ar = inp.athlete_ratings
    ar = ar[ar["rating_peak"].notna() & ar["athlete_id"].map(lambda a: people[a].exportable)]
    ar = ar.sort_values(["rating_peak", "athlete_id"], ascending=[False, True]).head(ALLTIME_TOP_N)
    rows = []
    for pos, r in enumerate(ar.itertuples(index=False), start=1):
        p = people[r.athlete_id]
        fid = _int(r.peak_fest_id)
        rows.append([pos, p.id, p.name, p.club, p.tv, p.by, _int(r.rating_peak),
                     _date(r.peak_date), fid, fest_names.get(fid) if fid is not None else None,
                     _athlete_flags(r)])
    return rows


def _season_places(inp: Inputs, people: dict[str, _Person], min_bouts: int) -> pd.DataFrame:
    """``season_ratings`` rows that may be listed, with the site's season place ``pos``."""
    sr = inp.season_ratings
    ok = sr["athlete_id"].map(lambda a: people[a].exportable) & ~sr["provisional"] \
        & (sr["n_bouts"] >= min_bouts)
    listed = sr[ok].sort_values(["season", "rating_end", "athlete_id"],
                                ascending=[True, False, True]).copy()
    listed["pos"] = listed.groupby("season").cumcount() + 1
    return listed


def _seasons(inp: Inputs, people: dict[str, _Person], listed: pd.DataFrame,
             as_of: str | None, thin_festivals: int, first_ranked: int) -> list[dict[str, Any]]:
    fests_per_season = inp.ratings.groupby("season")["fest_id"].nunique()
    athletes_per_season = inp.season_ratings.groupby("season").size()
    current = int(as_of[:4]) if as_of else None
    out = []
    for season in sorted((int(s) for s in athletes_per_season.index), reverse=True):
        n_fests = int(fests_per_season.get(season, 0))
        if season < first_ranked:
            status = "burn_in"
        elif season == current:
            status = "current"
        elif n_fests < thin_festivals:
            status = "thin"
        else:
            status = "ok"
        part = listed[listed["season"] == season]
        placed = status in ("ok", "current")
        rows = []
        for r in part.head(SEASON_TOP_N).itertuples(index=False):
            p = people[r.athlete_id]
            rows.append([int(r.pos) if placed else None, p.id, p.name, p.club, p.tv, p.by,
                         _int(r.rating_end), _int(r.rating_peak), _int(r.n_bouts), p.unc])
        peak = None
        if len(part):
            top = part.sort_values(["rating_peak", "athlete_id"], ascending=[False, True]).iloc[0]
            peak = {"id": top["athlete_id"], "name": people[top["athlete_id"]].name,
                    "rating": _int(top["rating_peak"])}
        out.append({"season": season, "status": status, "n_festivals": n_fests,
                    "n_athletes": int(athletes_per_season.get(season, 0)),
                    "n_listed": int(len(part)), "peak": peak, "cols": SEASON_COLS, "rows": rows})
    return out


def _fest_status(row: Any) -> str:
    if int(row.n_bouts) <= 0:
        return "none"
    if not bool(row.elo_eligible):
        return "unrated"
    return "partial" if _text(row.parse_status) not in ("ok",) else "ok"


def _festival_index(inp: Inputs) -> tuple[list[list[Any]], pd.DataFrame]:
    f = inp.festivals
    f = f[(f["kind"] == "active") & ~f["cancelled"].astype(bool)]
    f = f.sort_values(["date", "fest_id"], ascending=[False, False])
    rows = []
    for r in f.itertuples(index=False):
        rows.append([int(r.fest_id), _text(r.name) or "?", _date(r.date), _text(r.category),
                     _text(r.eidg_type), _text(r.location), _int(r.n_athletes_raw),
                     _int(r.n_bouts), _fest_status(r)])
    return rows, f


def _outcome_counts(bouts: pd.DataFrame) -> pd.DataFrame:
    """Wins / draws / losses per athlete over ``bouts`` (index athlete_id, columns w d l)."""
    a = pd.DataFrame({"athlete_id": bouts["athlete_a_id"], "o": bouts["outcome"]})
    b = pd.DataFrame({"athlete_id": bouts["athlete_b_id"],
                      "o": bouts["outcome"].map({"WIN_A": "WIN_B", "WIN_B": "WIN_A",
                                                 "DRAW": "DRAW"})})
    both = pd.concat([a, b], ignore_index=True)
    tab = both.groupby(["athlete_id", "o"]).size().unstack(fill_value=0)
    return pd.DataFrame({"w": tab.get("WIN_A", 0), "d": tab.get("DRAW", 0),
                         "l": tab.get("WIN_B", 0)}, index=tab.index).astype(int)


_RES = {"WIN_A": 1, "DRAW": 0, "WIN_B": 2}


def _bout_flags(flags: Any, schlussgang: Any) -> int:
    fs = _flag_set(flags)
    sg = _clean(schlussgang)
    return (B_SCHLUSSGANG * (sg is True or sg == 1)
            | B_EXTRA * ("extra_bout" in fs)
            | B_NO_GRADE * bool(fs & {"grade_missing", "one_sided"})
            | B_GANG_UNCERTAIN * bool(fs & {"gang_uncertain", "gang_inferred", "gang_collision",
                                            "gang_mismatch"}))


def _write_fests(inp: Inputs, people: dict[str, _Person], fests: pd.DataFrame,
                 out_dir: Path) -> tuple[int, int]:
    """One file per festival with bouts; returns (files, bytes)."""
    rated = {(a, int(f)): (b, c) for a, f, b, c in zip(
        inp.ratings["athlete_id"], inp.ratings["fest_id"],
        inp.ratings["rating_before"], inp.ratings["rating_after"])}
    meta = {int(r.fest_id): r for r in fests.itertuples(index=False)}
    n_files = n_bytes = 0
    for fest_id, part in inp.bouts.groupby("fest_id", sort=True):
        fest_id = int(fest_id)
        if fest_id not in meta:
            continue
        m = meta[fest_id]
        w, d, l, pts = {}, {}, {}, {}
        for a, b, o, ga, gb in zip(part["athlete_a_id"], part["athlete_b_id"], part["outcome"],
                                   part["grade_a"], part["grade_b"]):
            for aid, won, lost, g in ((a, o == "WIN_A", o == "WIN_B", ga),
                                      (b, o == "WIN_B", o == "WIN_A", gb)):
                w[aid] = w.get(aid, 0) + won
                l[aid] = l.get(aid, 0) + lost
                d[aid] = d.get(aid, 0) + (o == "DRAW")
                g = _clean(g)
                pts[aid] = pts.get(aid, 0.0) + (g or 0.0)
        ids = sorted(w, key=lambda a: (-round(pts[a], 2), people[a].name.casefold(), a))
        index = {a: i for i, a in enumerate(ids)}
        athletes = []
        for a in ids:
            p = people[a]
            before, after = rated.get((a, fest_id), (None, None))
            athletes.append([p.id if p.exportable else None, p.name if p.exportable else None,
                             p.club if p.exportable else None, p.tv if p.exportable else None,
                             _int(before), _int(after), int(w[a]), int(d[a]), int(l[a]),
                             _r2(pts[a]), p.unc if p.exportable else 0])
        bouts = sorted(
            [int(g), index[a], index[b], _RES[o], _r2(ga), _r2(gb), _bout_flags(fl, sg)]
            for g, a, b, o, ga, gb, fl, sg in zip(
                part["gang_nr"], part["athlete_a_id"], part["athlete_b_id"], part["outcome"],
                part["grade_a"], part["grade_b"], part["flags"], part["schlussgang"]))
        obj = {"id": fest_id, "name": _text(m.name) or "?", "date": _date(m.date),
               "season": _int(m.year), "category": _text(m.category),
               "eidg_type": _text(m.eidg_type), "location": _text(m.location),
               "url": _text(m.url), "status": _fest_status(m), "n_gaenge": _int(m.n_gaenge),
               "athletes": {"cols": FEST_ATHLETE_COLS, "rows": athletes},
               "bouts": {"cols": FEST_BOUT_COLS, "rows": bouts}}
        n_bytes += _write_json(out_dir / f"fest_{fest_id}.json", obj)
        n_files += 1
    return n_files, n_bytes


def _write_histories(inp: Inputs, people: dict[str, _Person], fest_names: dict[int, str],
                     listed: pd.DataFrame, placed_seasons: set[int], cfg: Config,
                     as_of: str | None, out_dir: Path) -> tuple[int, int, int]:
    """One file per exportable athlete; returns (files, bytes, history rows)."""
    ar = inp.athlete_ratings.set_index("athlete_id")
    record = _outcome_counts(inp.bouts[inp.bouts["elo_eligible"].astype(bool)])
    by_name: dict[str, list[str]] = {}
    for p in people.values():
        if p.exportable:
            by_name.setdefault(p.name.casefold(), []).append(p.id)
    pos = {(a, int(s)): int(p) for a, s, p in zip(listed["athlete_id"], listed["season"],
                                                  listed["pos"])}
    seasons: dict[str, list[list[Any]]] = {}
    for r in inp.season_ratings.sort_values(["athlete_id", "season"]).itertuples(index=False):
        s = int(r.season)
        seasons.setdefault(r.athlete_id, []).append(
            [s, _int(r.rating_end), _int(r.rating_peak), _int(r.n_bouts),
             pos.get((r.athlete_id, s)) if s in placed_seasons else None])
    hist = inp.ratings.sort_values(["athlete_id", "date", "fest_id"], kind="mergesort")
    n_files = n_bytes = n_rows = 0
    for aid, part in hist.groupby("athlete_id", sort=True):
        p = people.get(aid)
        if p is None or not p.exportable:
            continue
        rows = []
        for r in part.itertuples(index=False):
            reasons = _flag_set(r.provisional_reason)
            rows.append([_date(r.date), int(r.fest_id), fest_names.get(int(r.fest_id), "?"),
                         _text(r.category), _r1(r.rating_before), _r1(r.rating_after),
                         _int(r.n_bouts), _r1(r.score), _r2(r.expected),
                         H_PROVISIONAL * bool(r.provisional) | H_RETURN * ("inactive" in reasons)])
        a = ar.loc[aid]
        peak_fest = _int(a["peak_fest_id"])
        rec = record.loc[aid] if aid in record.index else None
        obj = {
            "id": p.id, "name": p.name, "club": p.club, "tv": p.tv, "by": p.by,
            "first": p.first, "last": p.last,
            "bouts": _int(a["n_bouts"]), "festivals": _int(a["n_festivals"]),
            "record": [int(rec["w"]), int(rec["d"]), int(rec["l"])] if rec is not None else None,
            "rating": _r1(a["rating"]), "rating_last": rows[-1][5] if rows else None,
            "last_date": _date(a["last_date"]), "idle": _int(a["days_inactive"]),
            "peak": _r1(a["rating_peak"]), "peak_date": _date(a["peak_date"]),
            "peak_fest": {"id": peak_fest, "name": fest_names.get(peak_fest, "?")}
            if peak_fest is not None else None,
            "rank": _int(a["rank"]) if bool(a["ranked"]) else None, "ranked": bool(a["ranked"]),
            "provisional": sorted(_flag_set(a["provisional_reason"])),
            "unc": p.unc,
            "unc_rows": [_int(a["identity_low_conf_rows"]), _int(a["identity_rows"])],
            "namesakes": [
                {"id": q.id, "name": q.name, "club": q.club, "tv": q.tv, "by": q.by,
                 "first": q.first, "last": q.last}
                for q in (people[i] for i in sorted(by_name.get(p.name.casefold(), [])))
                if q.id != p.id],
            "rev": [cfg.season_reversion_delta, cfg.season_reversion_mean,
                    cfg.season_start_month],
            "as_of": as_of,
            "seasons": {"cols": ATHLETE_SEASON_COLS, "rows": seasons.get(aid, [])},
            "history": {"cols": HISTORY_COLS, "rows": rows},
        }
        n_bytes += _write_json(out_dir / f"history_{aid}.json", obj)
        n_files += 1
        n_rows += len(rows)
    return n_files, n_bytes, n_rows


# --------------------------------------------------------------------------- build
def _model(cfg: Config, min_bouts: int, thin: int) -> dict[str, Any]:
    return {
        "initial": cfg.elo_initial, "mean": cfg.season_reversion_mean,
        "delta": cfg.season_reversion_delta, "k_scale": cfg.elo_k_scale,
        "k": {c: cfg.elo_k_scale * k for c, k in sorted(cfg.k_factors.items())},
        "provisional_min_bouts": cfg.provisional_min_bouts,
        "inactive_days": _r1(cfg.provisional_inactive_seasons * 365.25),
        "first_ranked_season": cfg.elo_first_ranked_season,
        "season_min_bouts": min_bouts, "thin_season_festivals": thin,
    }


def write_data(cfg: Config, dist: Path) -> dict[str, Any]:
    """Write ``dist/data/**``; returns a summary (file counts and bytes per slice)."""
    data = dist / DATA_DIR
    min_bouts = SAMPLE_SEASON_MIN_BOUTS if cfg.sample else SEASON_MIN_BOUTS
    thin = SAMPLE_THIN_SEASON_FESTIVALS if cfg.sample else THIN_SEASON_FESTIVALS
    model = _model(cfg, min_bouts, thin)
    inp = load_inputs(cfg.processed_dir)
    sizes: dict[str, Any] = {}
    if inp is None or inp.ratings.empty:
        meta = {"schema": SCHEMA_VERSION, "sample": cfg.sample, "empty": True, "as_of": None,
                "first_season": None, "last_season": None,
                "counts": {"athletes": 0, "ranked": 0, "festivals": 0, "festivals_partial": 0,
                           "festivals_missing": 0, "bouts": 0, "history_rows": 0},
                "model": model}
        sizes["meta.json"] = _write_json(data / "meta.json", meta)
        sizes["rankings_latest.json"] = _write_json(
            data / "rankings_latest.json", {"as_of": None, "cols": RANKING_COLS, "rows": []})
        sizes["athletes.json"] = _write_json(data / "athletes.json",
                                             {"cols": SEARCH_COLS, "rows": []})
        sizes["alltime_top200.json"] = _write_json(
            data / "alltime_top200.json", {"as_of": None, "cols": ALLTIME_COLS, "rows": []})
        sizes["seasons.json"] = _write_json(data / "seasons.json",
                                            {"min_bouts": min_bouts, "seasons": []})
        sizes["festivals.json"] = _write_json(data / "festivals.json",
                                              {"cols": FESTIVAL_COLS, "rows": []})
        return {"sizes": sizes, "history_files": 0, "fest_files": 0, "empty": True}

    people = _people(inp)
    as_of = _date(max(inp.ratings["date"].map(_date)))
    fest_rows, fests = _festival_index(inp)
    fest_names = {int(i): _text(n) or "?" for i, n in zip(inp.festivals["fest_id"],
                                                         inp.festivals["name"])}
    listed = _season_places(inp, people, min_bouts)
    seasons = _seasons(inp, people, listed, as_of, thin, cfg.elo_first_ranked_season)
    placed = {s["season"] for s in seasons if s["status"] in ("ok", "current")}
    rankings = _rankings(inp, people)
    search = _search_index(inp, people)

    sizes["rankings_latest.json"] = _write_json(
        data / "rankings_latest.json", {"as_of": as_of, "cols": RANKING_COLS, "rows": rankings})
    sizes["athletes.json"] = _write_json(data / "athletes.json",
                                         {"cols": SEARCH_COLS, "rows": search})
    sizes["alltime_top200.json"] = _write_json(
        data / "alltime_top200.json",
        {"as_of": as_of, "cols": ALLTIME_COLS, "rows": _alltime(inp, people, fest_names)})
    sizes["seasons.json"] = _write_json(data / "seasons.json",
                                        {"min_bouts": min_bouts, "seasons": seasons})
    sizes["festivals.json"] = _write_json(data / "festivals.json",
                                          {"cols": FESTIVAL_COLS, "rows": fest_rows})
    fest_files, fest_bytes = _write_fests(inp, people, fests, data / "fests")
    hist_files, hist_bytes, hist_rows = _write_histories(
        inp, people, fest_names, listed, placed, cfg, as_of, data / "history")
    all_seasons = [s["season"] for s in seasons]
    meta = {"schema": SCHEMA_VERSION, "sample": cfg.sample, "empty": False, "as_of": as_of,
            "first_season": min(all_seasons), "last_season": max(all_seasons),
            "counts": {"athletes": len(search), "ranked": len(rankings),
                       "festivals": sum(1 for r in fest_rows if r[8] != "none"),
                       "festivals_partial": sum(1 for r in fest_rows if r[8] == "partial"),
                       "festivals_missing": sum(1 for r in fest_rows if r[8] == "none"),
                       "bouts": int(inp.bouts["elo_eligible"].astype(bool).sum()),
                       "history_rows": hist_rows},
            "model": model}
    sizes["meta.json"] = _write_json(data / "meta.json", meta)
    return {"sizes": sizes, "history_files": hist_files, "history_bytes": hist_bytes,
            "fest_files": fest_files, "fest_bytes": fest_bytes, "empty": False,
            "athletes": len(search), "ranked": len(rankings)}


def _prepare_dist(cfg: Config) -> Path:
    """Wipe a previous build (and only that) and return the dist path."""
    dist = cfg.dist_dir.resolve()
    protected = {cfg.web_dir.resolve(), cfg.data_dir.resolve(), REPO_ROOT, Path.home()}
    if dist in protected or dist in REPO_ROOT.parents:
        raise ValueError(f"refusing to wipe {dist}: not a safe dist directory")
    if dist.exists():
        if not dist.is_dir():
            raise ValueError(f"refusing to wipe {dist}: not a directory")
        if any(dist.iterdir()) and not (dist / BUILD_MARKER).is_file():
            raise ValueError(
                f"refusing to wipe {dist}: not empty and not a previous build "
                f"(no {BUILD_MARKER}); delete it yourself or pick another dist dir"
            )
        shutil.rmtree(dist)
    return dist


def build_site(cfg: Config) -> Path:
    """(Re)build ``cfg.dist_dir`` and return its path.

    Copies ``web/`` and writes the JSON slices to ``dist/data/``. Only relative URLs
    are used (spec §6.3). An existing ``dist`` is only deleted if it is empty or carries
    the :data:`BUILD_MARKER` of a previous build; otherwise ``ValueError``.
    """
    dist = _prepare_dist(cfg)
    if cfg.web_dir.is_dir():
        shutil.copytree(cfg.web_dir, dist, ignore=_IGNORED)
    else:
        dist.mkdir(parents=True)
    index = dist / "index.html"
    if not index.exists():
        index.write_text(PLACEHOLDER_HTML, encoding="utf-8")
    summary = write_data(cfg, dist)
    # GitHub Pages: serve files as-is (no Jekyll processing).
    (dist / BUILD_MARKER).touch()
    build_site.last_summary = summary  # type: ignore[attr-defined]
    return dist


def dist_stats(dist: Path) -> tuple[int, int]:
    """(file count, total bytes) of a built site."""
    files = [p for p in dist.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)
