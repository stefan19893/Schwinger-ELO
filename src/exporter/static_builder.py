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
``bouts/bouts_<athlete_id>.json``
    One athlete: his bouts against *published* opponents, from his side, grouped by
    festival (read by the comparison page only, one file per selected athlete). ``opp`` is
    an index into the file's own ``opps`` list of athlete ids; ``names`` holds their names
    in the same order and ``unc`` the indices of the identity-uncertain ones. ``d`` is the
    bout's contribution to this athlete's rating (``bout_ratings.parquet``, one decimal;
    null at a festival that does not count). The engine rates a festival as a whole against
    the pre-festival ratings, so the contributions of a festival are a breakdown of its
    change, not ratings after each Gang. Bouts against anyone who is not exportable are not
    written at all, so the file cannot name a withheld athlete - and it carries nothing
    about those bouts either: no contribution, no count, no Gang. What they add up to is
    what the history file already implies (``after`` - ``before`` minus the listed bouts).

An athlete is *exportable* when he has rated bouts, is not a ``not_a_name`` row and is
not *withheld*. Only name, club, Teilverband and birth year are published (no residence,
birthday, licence number or portrait slug). A festival file lists every participant: an
athlete without rated bouts (he only appears at an unrated festival) by name only, without
id or profile; a ``not_a_name`` row without id and name.

Publication switches (``src/config.py``):

``publish_min_age``
    An athlete who is not certainly that old at the data date (``as_of`` year - birth year
    <= the age) is *withheld*: he counts in the ratings, but has no history file, no
    search entry and no rank, appears in no list, and a festival shows him without id,
    name, club, Teilverband and without any rating value (column ``anon`` = 1; record and
    grade sum stay, they are the festival's bouts).
    Ranks are the places among the published athletes (see :func:`_published_ranks`).
``publish_unknown_recent_seasons``
    An athlete without a known birth year is withheld in the same way when his first
    season lies within the last N seasons of the data year - he may be a minor.
``site_noindex``
    Every page gets ``<meta name="robots" content="noindex">`` and ``robots.txt`` is
    written.
``contact_email``
    Passed to the about page through ``meta.json`` (``contact``).

``build`` refuses to write a site without rating data (:class:`EmptyBuildError`) unless
``allow_empty`` is set - a deployment must never publish an empty site by accident.

The build is deterministic: the same Parquet inputs give byte-identical output (no
timestamps; "as of" is the date of the last rated festival).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import re
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
INPUTS = ("athletes", "athlete_ratings", "bout_ratings", "bouts", "festivals", "ratings",
          "season_ratings")

# athletes.json / alltime flags
F_RANKED, F_FEW_BOUTS, F_INACTIVE, F_UNCERTAIN = 1, 2, 4, 8
# bout flags
B_SCHLUSSGANG, B_EXTRA, B_NO_GRADE, B_GANG_UNCERTAIN = 1, 2, 4, 8
# bouts_<id>.json only: the festival does not count for the rating
B_UNRATED = 16
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
FEST_ATHLETE_COLS = ["id", "name", "club", "tv", "before", "after", "w", "d", "l", "pts", "unc",
                     "anon"]
FEST_BOUT_COLS = ["gang", "a", "b", "res", "ga", "gb", "flags"]
HISTORY_COLS = ["date", "fest_id", "fest", "cat", "before", "after", "n", "score", "exp",
                "flags"]
ATHLETE_SEASON_COLS = ["season", "rating", "peak", "bouts", "pos"]
BOUT_SIDE_COLS = ["gang", "opp", "res", "g", "go", "flags", "d"]
OTHER_FEST_COLS = ["id", "name", "date", "cat"]

ROBOTS_META = '<meta name="robots" content="noindex">'
ROBOTS_TXT = """# Schwinger-ELO asks not to be indexed (site_noindex in src/config.py).
# Crawlers read robots.txt only at the root of a host: under a project path such as
# <user>.github.io/Schwinger-ELO/ this file is not consulted, there the robots meta tag
# of the pages is what counts.
User-agent: *
Disallow: /
"""

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
    bout_ratings: pd.DataFrame
    bouts: pd.DataFrame
    festivals: pd.DataFrame
    ratings: pd.DataFrame
    season_ratings: pd.DataFrame


class EmptyBuildError(ValueError):
    """There is no rating data to publish and an empty site was not asked for."""


def missing_inputs(processed_dir: Path) -> list[str]:
    """File names of the Parquet inputs that do not exist in ``processed_dir``."""
    return [f"{name}.parquet" for name in INPUTS
            if not (processed_dir / f"{name}.parquet").is_file()]


def load_inputs(processed_dir: Path, allow_empty: bool = False) -> Inputs | None:
    """Read the Parquet files of ``clean`` and ``elo``.

    Without rating data (a file is missing, or ``ratings`` has no rows) this raises
    :class:`EmptyBuildError`; with ``allow_empty`` it returns ``None`` instead and the
    caller writes a site without content."""
    missing = missing_inputs(processed_dir)
    if missing:
        reason = f"{', '.join(missing)} missing in {processed_dir}"
    else:
        inp = Inputs(**{name: pd.read_parquet(processed_dir / f"{name}.parquet")
                        for name in INPUTS})
        if not inp.ratings.empty:
            # every rated bout has its contribution: otherwise bout_ratings.parquet is from
            # another run than bouts.parquet (an old state, `clean` without `elo`). That is
            # not "no data", so `allow_empty` does not turn it into an empty site.
            rated = inp.bouts.loc[inp.bouts["elo_eligible"].astype(bool), "bout_id"]
            n_missing = int((~rated.isin(inp.bout_ratings["bout_id"])).sum())
            if n_missing:
                raise EmptyBuildError(
                    f"bout_ratings.parquet in {processed_dir} lacks {n_missing} of "
                    f"{len(rated)} rated bouts: it does not belong to this bouts.parquet - "
                    f"run `python -m src.cli elo` first")
            return inp
        reason = f"ratings.parquet in {processed_dir} has no rows"
    if not allow_empty:
        raise EmptyBuildError(
            f"no rating data: {reason} - run `python -m src.cli clean` and `elo` first "
            f"(or `build --allow-empty` for a site without content)")
    log.warning("build: %s - writing a site without data (--allow-empty)", reason)
    return None


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
    nameable: bool = True   # False for `not_a_name` rows: never shown by name
    withheld: bool = False  # not certainly old enough to be published by name
    withheld_no_by: bool = False  # ... because the birth year is unknown and he is new


def min_birth_year_withheld(as_of: str | None, min_age: int) -> int | None:
    """First birth year that is withheld: everyone born in or after it is not certainly
    ``min_age`` years old at the data date. ``None`` = nobody is withheld."""
    if min_age <= 0 or not as_of:
        return None
    return int(as_of[:4]) - min_age


def min_first_season_withheld(as_of: str | None, min_age: int, recent_seasons: int) -> int | None:
    """First debut season that is withheld for athletes *without a birth year*: whoever
    started in or after it may not be ``min_age`` yet. ``None`` = the rule is off."""
    if min_age <= 0 or recent_seasons <= 0 or not as_of:
        return None
    return int(as_of[:4]) - recent_seasons + 1


def _people(inp: Inputs, withhold_from: int | None = None,
            withhold_first_from: int | None = None) -> dict[str, _Person]:
    ar = inp.athlete_ratings.set_index("athlete_id")
    out: dict[str, _Person] = {}
    for row in inp.athletes.itertuples(index=False):
        aid = row.athlete_id
        rated = aid in ar.index and _clean(ar.at[aid, "rating"]) is not None \
            and int(ar.at[aid, "n_bouts"]) > 0
        garbage = NOT_A_NAME in _flag_set(row.evidence, ";|")
        if aid in ar.index:
            garbage = garbage or NOT_A_NAME in _flag_set(ar.at[aid, "identity_flags"], ";|")
        by = _int(row.birth_year)
        first = _int(row.first_season)
        withheld = withhold_from is not None and by is not None and by >= withhold_from
        # no birth year: a recent debutant may be a minor (`not_a_name` rows are never
        # shown by name anyway and stay "Name nicht lesbar")
        no_by = (withhold_first_from is not None and by is None and not garbage
                 and (first is None or first >= withhold_first_from))
        withheld = withheld or no_by
        out[aid] = _Person(
            id=aid, name=_text(row.full_name) or "?", club=_text(row.club),
            tv=_text(row.sub_association), by=by,
            first=first, last=_int(row.last_season),
            exportable=bool(rated and not garbage and not withheld),
            nameable=not garbage and not withheld, withheld=withheld, withheld_no_by=no_by,
            unc=int(bool(ar.at[aid, "identity_uncertain"])) if aid in ar.index else 0)
    return out


def _published_ranks(inp: Inputs, people: dict[str, _Person]) -> dict[str, int]:
    """Rank of every published ranked athlete = his place among the published athletes.

    ``athlete_ratings.rank`` counts everyone. Leaving the withheld athletes' places empty
    would show a list that jumps from 137 to 139 and "Rang 2020" in a list of 1,500, and
    every gap would point at a hidden person; so the rank shown is the engine's rank minus
    the withheld athletes ahead (ties keep sharing a rank). Without withheld athletes the
    engine's ranks come out unchanged. Ranking, search index and profile use this map."""
    ranked = inp.athlete_ratings[inp.athlete_ratings["ranked"]].sort_values(["rank", "athlete_id"])
    out: dict[str, int] = {}
    hidden_ranks: list[int] = []
    for aid, rank in zip(ranked["athlete_id"], ranked["rank"]):
        rank = int(rank)
        p = people[aid]
        if p.withheld:
            hidden_ranks.append(rank)
        elif p.exportable:
            out[aid] = rank - sum(1 for h in hidden_ranks if h < rank)
    return out


# --------------------------------------------------------------------------- slices
def _athlete_flags(row: Any) -> int:
    reasons = _flag_set(row.provisional_reason)
    return (F_RANKED * bool(row.ranked) | F_FEW_BOUTS * ("few_bouts" in reasons)
            | F_INACTIVE * ("inactive" in reasons) | F_UNCERTAIN * bool(row.identity_uncertain))


def _rankings(inp: Inputs, people: dict[str, _Person], ranks: dict[str, int]) -> list[list[Any]]:
    ranked = inp.athlete_ratings[inp.athlete_ratings["ranked"]].sort_values(["rank", "athlete_id"])
    rows = []
    for r in ranked.itertuples(index=False):
        p = people[r.athlete_id]
        if not p.exportable:  # never rank garbage names, athletes without bouts, withheld
            continue
        rows.append([ranks[p.id], p.id, p.name, p.club, p.tv, p.by, _int(r.rating),
                     _int(r.rating_peak), _date(r.last_date), _int(r.days_inactive),
                     _int(r.n_bouts), p.unc])
    return rows


def _search_index(inp: Inputs, people: dict[str, _Person],
                  ranks: dict[str, int]) -> list[list[Any]]:
    rows = []
    for r in inp.athlete_ratings.itertuples(index=False):
        p = people[r.athlete_id]
        if not p.exportable:
            continue
        rows.append([p.id, p.name, p.club, p.tv, p.by, p.first, p.last, _int(r.rating),
                     _int(r.rating_peak), ranks.get(p.id) if r.ranked else None,
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
            best = people[top["athlete_id"]]
            peak = {"id": best.id, "name": best.name, "rating": _int(top["rating_peak"]),
                    "unc": best.unc}
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
        # withheld athletes sort after the named ones of equal points, not by their name
        ids = sorted(w, key=lambda a: (-round(pts[a], 2), people[a].withheld,
                                       "" if people[a].withheld else people[a].name.casefold(),
                                       a))
        index = {a: i for i, a in enumerate(ids)}
        athletes = []
        for a in ids:
            p = people[a]
            # a withheld athlete's row carries no rating value: before / after would chain
            # into his whole rating history across the festival files
            before, after = (None, None) if p.withheld else rated.get((a, fest_id),
                                                                      (None, None))
            # no profile: the name alone (unrated athlete) or nothing (`not_a_name`)
            athletes.append([p.id if p.exportable else None, p.name if p.nameable else None,
                             p.club if p.exportable else None, p.tv if p.exportable else None,
                             _int(before), _int(after), int(w[a]), int(d[a]), int(l[a]),
                             _r2(pts[a]), p.unc if p.exportable else 0, int(p.withheld)])
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
                     as_of: str | None, ranks: dict[str, int],
                     out_dir: Path) -> tuple[int, int, int]:
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
            "rank": ranks.get(aid) if bool(a["ranked"]) else None, "ranked": bool(a["ranked"]),
            "provisional": sorted(_flag_set(a["provisional_reason"])),
            "unc": p.unc,
            "unc_rows": [_int(a["identity_low_conf_rows"]), _int(a["identity_rows"])],
            "namesakes": [
                {"id": q.id, "name": q.name, "club": q.club, "tv": q.tv, "by": q.by,
                 "first": q.first, "last": q.last, "unc": q.unc}
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


_RES_OTHER_SIDE = {0: 0, 1: 2, 2: 1}


def _write_bouts(inp: Inputs, people: dict[str, _Person], fests: pd.DataFrame,
                 out_dir: Path) -> tuple[int, int, int]:
    """One file per exportable athlete with his bouts against exportable opponents;
    returns (files, bytes, bouts written - each bout once).

    A bout is written into both athletes' files, each from its own side (``res`` 1 won,
    0 gestellt, 2 lost; ``g`` own grade, ``go`` the opponent's; ``d`` what the bout
    contributed to the athlete's rating, null when the festival does not count). ``d`` is
    the engine's own per-bout change (``bout_ratings.parquet``), never recomputed here; a
    rated bout without one means the Parquet files are from different runs and fails the
    build. Bouts with a withheld,
    unnamed or unrated athlete on either side are skipped: the comparison page is the only
    reader and it compares published athletes. Every exportable athlete gets a file, also
    without rows, so that a selectable athlete never ends in a 404."""
    meta = {int(r.fest_id): (_date(r.date) or "", _text(r.name) or "?", _text(r.category))
            for r in fests.itertuples(index=False)}
    in_history: dict[str, set[int]] = {}
    for aid, fid in zip(inp.ratings["athlete_id"], inp.ratings["fest_id"]):
        in_history.setdefault(aid, set()).add(int(fid))
    exportable = {a for a, p in people.items() if p.exportable}
    # contribution of every rated bout to side A (side B: its negative, by construction)
    br = inp.bout_ratings
    side_a = br["side"] == "A"
    delta_a = dict(zip(br.loc[side_a, "bout_id"], br.loc[side_a, "delta"].astype(float)))
    # athlete -> fest -> rows [gang, opponent id, res, g, go, flags, d]
    sides: dict[str, dict[int, list[list[Any]]]] = {a: {} for a in exportable}
    n_bouts = 0
    b = inp.bouts
    flag_cache: dict[tuple[Any, bool], int] = {}
    # grades as JSON values (2 decimals, None for a missing one); NaN is not a dict key
    # that can be found again, so missing grades are replaced before the loop
    grade = {g: _r2(g) for g in pd.unique(pd.concat([b["grade_a"], b["grade_b"]]).dropna())}
    grade_a = b["grade_a"].astype(object).where(b["grade_a"].notna(), None)
    grade_b = b["grade_b"].astype(object).where(b["grade_b"].notna(), None)
    for bid, fid, gang, a, c, o, ga, gb, fl, sg, ok in zip(
            b["bout_id"], b["fest_id"], b["gang_nr"], b["athlete_a_id"], b["athlete_b_id"],
            b["outcome"], grade_a, grade_b, b["flags"], b["schlussgang"], b["elo_eligible"]):
        fid = int(fid)
        if a not in exportable or c not in exportable or fid not in meta:
            continue
        res = _RES[o]
        key = (fl, sg is True or sg == 1)       # few distinct values: parse each once
        flags = flag_cache.get(key)
        if flags is None:
            flags = flag_cache[key] = _bout_flags(fl, sg)
        da = db = None
        if not ok:
            flags |= B_UNRATED
        else:
            d = delta_a.get(bid)
            if d is None or not math.isfinite(d):
                raise ValueError(
                    "bout_ratings.parquet has no contribution for a rated bout of "
                    f"festival {fid}: it does not belong to this bouts.parquet - run "
                    "`python -m src.cli elo` again")
            # `+ 0.0`: never "-0.0" in the file
            da, db = round(d, 1) + 0.0, round(-d, 1) + 0.0
        ga, gb = grade.get(ga, ga), grade.get(gb, gb)
        sides[a].setdefault(fid, []).append([int(gang), c, res, ga, gb, flags, da])
        sides[c].setdefault(fid, []).append([int(gang), a, _RES_OTHER_SIDE[res], gb, ga, flags,
                                             db])
        n_bouts += 1
    n_files = n_bytes = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for aid in sorted(sides):
        by_fest = sides[aid]
        opps = sorted({r[1] for rows in by_fest.values() for r in rows})
        index = {o: i for i, o in enumerate(opps)}
        fest_rows = []
        for fid in sorted(by_fest, key=lambda f: (meta[f][0], f)):
            rows = sorted(by_fest[fid], key=lambda r: (r[0], r[1]))
            fest_rows.append([fid, [[r[0], index[r[1]], r[2], r[3], r[4], r[5], r[6]]
                                    for r in rows]])
        known = in_history.get(aid, set())
        other = [[fid, meta[fid][1], meta[fid][0] or None, meta[fid][2]]
                 for fid, _ in fest_rows if fid not in known]
        # the opponents are exportable by construction: their names are published
        obj = {"id": aid, "opps": opps, "names": [people[o].name for o in opps],
               "unc": [i for i, o in enumerate(opps) if people[o].unc],
               "cols": BOUT_SIDE_COLS, "fests": fest_rows,
               "other": {"cols": OTHER_FEST_COLS, "rows": other}}
        # plain Python values only (built above): skip the recursive `_clean` pass, which
        # costs more than everything else here; `allow_nan=False` still guards the output
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
        (out_dir / f"bouts_{aid}.json").write_bytes(data)
        n_bytes += len(data)
        n_files += 1
    return n_files, n_bytes, n_bouts


# --------------------------------------------------------------------------- build
def _publish(cfg: Config, withhold_from: int | None, withhold_first_from: int | None = None,
             release: tuple[int, int] = (0, 0)) -> dict[str, Any]:
    """What the pages say about the publication rules (about page), and what the deploy
    guard needs: ``release_next_year`` is the number of withheld athletes (and of ranked
    ones among them) who become publishable when the data year advances by one."""
    return {"min_age": cfg.publish_min_age, "withheld_from_birth_year": withhold_from,
            "unknown_recent_seasons": cfg.publish_unknown_recent_seasons
            if cfg.publish_min_age > 0 else 0,
            "withheld_from_first_season": withhold_first_from,
            "release_next_year": {"athletes": release[0], "ranked": release[1]},
            "noindex": cfg.site_noindex}


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


def write_data(cfg: Config, dist: Path, inp: Inputs | None) -> dict[str, Any]:
    """Write ``dist/data/**``; returns a summary (file counts and bytes per slice).
    ``inp`` None writes the data files of a site without content (``meta.empty``)."""
    data = dist / DATA_DIR
    min_bouts = SAMPLE_SEASON_MIN_BOUTS if cfg.sample else SEASON_MIN_BOUTS
    thin = SAMPLE_THIN_SEASON_FESTIVALS if cfg.sample else THIN_SEASON_FESTIVALS
    model = _model(cfg, min_bouts, thin)
    sizes: dict[str, Any] = {}
    if inp is None:
        meta = {"schema": SCHEMA_VERSION, "sample": cfg.sample, "empty": True, "as_of": None,
                "first_season": None, "last_season": None,
                "counts": {"athletes": 0, "ranked": 0, "festivals": 0, "festivals_partial": 0,
                           "festivals_missing": 0, "bouts": 0, "history_rows": 0,
                           "withheld": 0, "withheld_ranked": 0, "withheld_unknown": 0,
                           "rated": 0, "birth_year_known": 0},
                "model": model, "publish": _publish(cfg, None),
                "contact": cfg.contact_email or None}
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
        return {"sizes": sizes, "history_files": 0, "fest_files": 0, "bout_files": 0,
                "empty": True}

    as_of = _date(max(inp.ratings["date"].map(_date)))
    withhold_from = min_birth_year_withheld(as_of, cfg.publish_min_age)
    withhold_first_from = min_first_season_withheld(as_of, cfg.publish_min_age,
                                                    cfg.publish_unknown_recent_seasons)
    people = _people(inp, withhold_from, withhold_first_from)
    ranks = _published_ranks(inp, people)
    fest_rows, fests = _festival_index(inp)
    fest_names = {int(i): _text(n) or "?" for i, n in zip(inp.festivals["fest_id"],
                                                         inp.festivals["name"])}
    listed = _season_places(inp, people, min_bouts)
    seasons = _seasons(inp, people, listed, as_of, thin, cfg.elo_first_ranked_season)
    placed = {s["season"] for s in seasons if s["status"] in ("ok", "current")}
    rankings = _rankings(inp, people, ranks)
    search = _search_index(inp, people, ranks)
    ar = inp.athlete_ratings
    rated_ids = set(ar.loc[ar["rating"].notna() & (ar["n_bouts"] > 0), "athlete_id"])
    ranked_ids = set(ar.loc[ar["ranked"], "athlete_id"])
    hidden = [p for p in people.values() if p.withheld and p.id in rated_ids]
    withheld = len(hidden)
    withheld_ranked = sum(1 for a in ranked_ids if people[a].withheld)
    withheld_unknown = sum(1 for p in hidden if p.withheld_no_by)
    # who is released when the data year advances by one (the oldest withheld cohort)
    freed = [p for p in hidden if (p.by == withhold_from if p.by is not None
                                   else p.first == withhold_first_from)]
    release = (len(freed), sum(1 for p in freed if p.id in ranked_ids))
    # everyone with a rating who is a person (published or withheld)
    named = [p for p in people.values() if p.id in rated_ids and (p.nameable or p.withheld)]
    birth_year_known = sum(1 for p in named if p.by is not None)

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
        inp, people, fest_names, listed, placed, cfg, as_of, ranks, data / "history")
    bout_files, bout_bytes, bout_pairs = _write_bouts(inp, people, fests, data / "bouts")
    all_seasons = [s["season"] for s in seasons]
    meta = {"schema": SCHEMA_VERSION, "sample": cfg.sample, "empty": False, "as_of": as_of,
            "first_season": min(all_seasons), "last_season": max(all_seasons),
            "counts": {"athletes": len(search), "ranked": len(rankings),
                       "festivals": sum(1 for r in fest_rows if r[8] != "none"),
                       "festivals_partial": sum(1 for r in fest_rows if r[8] == "partial"),
                       "festivals_missing": sum(1 for r in fest_rows if r[8] == "none"),
                       "bouts": int(inp.bouts["elo_eligible"].astype(bool).sum()),
                       "history_rows": hist_rows,
                       "withheld": withheld, "withheld_ranked": withheld_ranked,
                       "withheld_unknown": withheld_unknown,
                       # inputs of the age filter, watched by the deploy guard
                       "rated": len(named), "birth_year_known": birth_year_known},
            "model": model,
            "publish": _publish(cfg, withhold_from, withhold_first_from, release),
            "contact": cfg.contact_email or None}
    sizes["meta.json"] = _write_json(data / "meta.json", meta)
    return {"sizes": sizes, "history_files": hist_files, "history_bytes": hist_bytes,
            "fest_files": fest_files, "fest_bytes": fest_bytes, "empty": False,
            "bout_files": bout_files, "bout_bytes": bout_bytes, "bout_pairs": bout_pairs,
            "athletes": len(search), "ranked": len(rankings), "withheld": withheld,
            "withheld_ranked": withheld_ranked, "withheld_unknown": withheld_unknown}


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


def build_site(cfg: Config, allow_empty: bool = False) -> Path:
    """(Re)build ``cfg.dist_dir`` and return its path.

    Copies ``web/`` and writes the JSON slices to ``dist/data/``. Only relative URLs
    are used (spec §6.3). An existing ``dist`` is only deleted if it is empty or carries
    the :data:`BUILD_MARKER` of a previous build; otherwise ``ValueError``.

    Without rating data nothing is written and :class:`EmptyBuildError` is raised (a
    previous build stays in place); ``allow_empty`` writes the pages with empty data
    files instead (``meta.empty``).
    """
    inp = load_inputs(cfg.processed_dir, allow_empty=allow_empty)  # before dist is wiped
    dist = _prepare_dist(cfg)
    if cfg.web_dir.is_dir():
        shutil.copytree(cfg.web_dir, dist, ignore=_IGNORED)
    else:
        dist.mkdir(parents=True)
    index = dist / "index.html"
    if not index.exists():
        index.write_text(PLACEHOLDER_HTML, encoding="utf-8")
    apply_indexing(cfg, dist)
    summary = write_data(cfg, dist, inp)
    # GitHub Pages: serve files as-is (no Jekyll processing).
    (dist / BUILD_MARKER).touch()
    build_site.last_summary = summary  # type: ignore[attr-defined]
    return dist


def apply_indexing(cfg: Config, dist: Path) -> int:
    """``site_noindex``: put the robots meta tag into every page of ``dist`` and write
    ``robots.txt``; returns the number of pages tagged. A page without ``<head>`` fails
    the build - it must not be published indexable by accident."""
    if not cfg.site_noindex:
        return 0
    pages = sorted(dist.rglob("*.html"))
    for page in pages:
        html = page.read_text(encoding="utf-8")
        if 'name="robots"' in html:
            continue
        if re.search(r"<head[^>]*>", html) is None:
            raise ValueError(f"site_noindex: {page} has no <head> to carry the robots tag")
        # after the charset declaration (which has to come first), else right after <head>
        head = re.search(r"<meta charset=[^>]*>[ \t]*\n?", html) \
            or re.search(r"<head[^>]*>[ \t]*\n?", html)
        page.write_text(html[:head.end()] + "  " + ROBOTS_META + "\n" + html[head.end():],
                        encoding="utf-8")
    (dist / "robots.txt").write_text(ROBOTS_TXT, encoding="utf-8")
    return len(pages)


def dist_stats(dist: Path) -> tuple[int, int]:
    """(file count, total bytes) of a built site."""
    files = [p for p in dist.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)
