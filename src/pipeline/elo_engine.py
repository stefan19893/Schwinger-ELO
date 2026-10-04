"""Schwingen-specific ELO engine (spec §4.2).

Model, per bout between athletes A and B at a festival of category ``c``::

    E_A    = 1 / (1 + 10 ** ((R_B - R_A) / scale))          expected score of A
    S_A    = 1 (A wins) | 0.5 (gestellt) | 0 (B wins)
    lambda = clamp(1 + alpha * (grade_winner - grade_loser - baseline_diff))
             for wins with both grades known, else 1         margin of victory
    delta  = k_scale * K[c] * w * lambda * (S_A - E_A)
    R_A   += delta ;  R_B -= delta                           zero-sum per bout

``K[c]`` are the per-category K-factors of the spec, ``k_scale`` one multiplier for all
of them (``elo_k_scale`` in ``src/config.py``): it sets the speed of the ratings, and
with it the spread of the scale, without touching the ratio between the tiers. ``w`` is 1
except for a bout flagged ``unlisted_opponent`` (sheets before 2011: the opponent is not
printed, the bout is known from one side), where it is ``one_sided_weight`` - 1.0 as
configured, i.e. such a bout counts like any other; with 0 it is not rated at all.

The model is symmetric in A / B: swapping the two sides of a bout (and its
outcome and grades) gives exactly the mirrored update. That matters because the
sheets list the better-ranked athlete as A (he wins 70 % of the bouts).

Order of operations (:meth:`SchwingElo.run`):

1. Only ``elo_eligible`` bouts (:func:`rated_bouts`); sorted by ``(date, fest_id, gang_nr, bout_id)``.
2. Before a festival that lies in a later rating year than the previous one
   (rating years start on 1 ``season_start_month``, April), every athlete who
   already has a rating is pulled toward the mean once per boundary crossed::

       R = R * (1 - delta) + mean * delta

3. The festival's bouts are applied according to ``update_mode``:

   ``festival``    every bout uses the ratings from before the festival; an athlete's
                   deltas are summed (the default; independent of the Gang order,
                   which the sources get wrong for ~2 % of the bouts)
   ``phase``       two blocks: Gänge 1..``phase_split_gang`` (Anschwingen / Ausschwingen),
                   then the later Gänge (Ausstich, Schlussgang)
   ``gang``        one block per Gang
   ``sequential``  bout by bout in sheet order (the literal reading of the spec)

4. One history row per athlete and festival: ``rating_before`` (after any mean
   reversion) and ``rating_after``.
5. Every bout's signed change (``delta`` above) is kept as it was applied
   (``EloResult.bouts.delta_a``); :func:`bout_contributions` lists it once per side. Per
   athlete and festival these changes sum to ``rating_after - rating_before``.

The engine knows nothing about files; ``src/pipeline/elo_runner.py`` loads the
Parquet inputs, adds the ranking tables and writes the outputs.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from src.config import Config

OUTCOME_SCORE: dict[str, float] = {"WIN_A": 1.0, "DRAW": 0.5, "WIN_B": 0.0}
UPDATE_MODES: tuple[str, ...] = ("festival", "phase", "gang", "sequential")
DAYS_PER_SEASON = 365.25
# Bout flag of the sheets before 2011: the opponent is not printed on the sheet (he did
# not finish the festival), so the bout is known from the printed athlete's line only.
UNLISTED_FLAG = "unlisted_opponent"

BOUT_COLUMNS: tuple[str, ...] = (
    "bout_id", "fest_id", "gang_nr", "athlete_a_id", "athlete_b_id", "outcome",
    "grade_a", "grade_b", "date", "category")

CONTRIBUTION_COLUMNS: tuple[str, ...] = (
    "bout_id", "fest_id", "date", "gang_nr", "side", "athlete_id", "opponent_id", "score",
    "expected", "k", "mov_lambda", "delta")

HISTORY_COLUMNS: tuple[str, ...] = (
    "athlete_id", "date", "fest_id", "rating_before", "rating_after", "season", "category",
    "n_bouts", "score", "expected", "bouts_before", "days_inactive", "provisional",
    "provisional_reason")


# --------------------------------------------------------------------------- parameters
@dataclass(frozen=True)
class EloParams:
    """All model parameters (defaults = spec §4.2; values live in ``src/config.py``)."""

    initial: float = 1500.0
    scale: float = 400.0
    k_factors: Mapping[str, float] = field(default_factory=dict)
    k_scale: float = 1.0  # multiplier on every K-factor (tier ratios unchanged)
    mov_alpha: float = 0.0
    mov_baseline_diff: float = 0.0
    mov_lambda_min: float = 0.5
    mov_lambda_max: float = 2.0
    season_start_month: int = 4
    reversion_delta: float = 0.10
    reversion_mean: float = 1500.0
    provisional_inactive_seasons: float = 1.5
    provisional_min_bouts: int = 0
    update_mode: str = "festival"
    phase_split_gang: int = 4
    # weight of a bout flagged ``unlisted_opponent`` (multiplies its K-factor); 0 = the
    # bout is not rated at all
    one_sided_weight: float = 1.0

    def __post_init__(self) -> None:
        if not (0 <= self.one_sided_weight <= 1):  # also rejects NaN
            raise ValueError("one-sided bout weight must be within 0..1")
        if self.update_mode not in UPDATE_MODES:
            raise ValueError(f"elo update mode {self.update_mode!r} not in {UPDATE_MODES}")
        if self.scale <= 0:
            raise ValueError("elo scale must be > 0")
        if not 0 <= self.reversion_delta <= 1:
            raise ValueError("season reversion delta must be within 0..1")
        if not 1 <= self.season_start_month <= 12:
            raise ValueError("season start month must be within 1..12")
        if not 0 < self.mov_lambda_min <= 1 <= self.mov_lambda_max:
            raise ValueError("require 0 < mov_lambda_min <= 1 <= mov_lambda_max")
        if any(k <= 0 for k in self.k_factors.values()):
            raise ValueError("K-factors must be > 0")
        if not (self.k_scale > 0 and math.isfinite(self.k_scale)):
            raise ValueError("elo k scale must be a finite number > 0")

    @classmethod
    def from_config(cls, cfg: Config, **overrides: Any) -> EloParams:
        values: dict[str, Any] = dict(
            initial=cfg.elo_initial, scale=cfg.elo_scale, k_factors=dict(cfg.k_factors),
            k_scale=cfg.elo_k_scale,
            mov_alpha=cfg.mov_alpha, mov_baseline_diff=cfg.mov_baseline_diff,
            mov_lambda_min=cfg.mov_lambda_min, mov_lambda_max=cfg.mov_lambda_max,
            season_start_month=cfg.season_start_month,
            reversion_delta=cfg.season_reversion_delta,
            reversion_mean=cfg.season_reversion_mean,
            provisional_inactive_seasons=cfg.provisional_inactive_seasons,
            provisional_min_bouts=cfg.provisional_min_bouts,
            update_mode=cfg.elo_update_mode, phase_split_gang=cfg.elo_phase_split_gang,
            one_sided_weight=cfg.elo_one_sided_weight)
        values.update(overrides)
        return cls(**values)

    def replace(self, **changes: Any) -> EloParams:
        return dataclasses.replace(self, **changes)

    def k(self, category: str) -> float:
        """Effective K-factor of a festival category: ``k_scale * k_factors[category]``."""
        return self.k_scale * k_factor(category, self.k_factors)

    @property
    def inactive_days(self) -> float:
        """Days without a bout after which an athlete counts as inactive."""
        return self.provisional_inactive_seasons * DAYS_PER_SEASON


# --------------------------------------------------------------------------- formulas
def expected_score(r_a: Any, r_b: Any, scale: float = 400.0) -> Any:
    """``E_A = 1 / (1 + 10 ** ((R_B - R_A) / scale))`` (floats or numpy arrays)."""
    return 1.0 / (1.0 + 10.0 ** ((np.asarray(r_b, dtype=float) - r_a) / scale))


def mov_multiplier(grade_winner: Any, grade_loser: Any, alpha: float, baseline_diff: float,
                   lambda_min: float = 0.5, lambda_max: float = 2.0) -> Any:
    """``lambda = 1 + alpha * (grade_winner - grade_loser - baseline_diff)``, clamped to
    ``[lambda_min, lambda_max]``. A missing grade (None / NaN) gives 1: the bout counts
    with its outcome only."""
    diff = np.asarray(grade_winner, dtype=float) - np.asarray(grade_loser, dtype=float)
    lam = np.clip(1.0 + alpha * (diff - baseline_diff), lambda_min, lambda_max)
    lam = np.where(np.isnan(diff), 1.0, lam)
    return float(lam) if lam.ndim == 0 else lam


def bout_multiplier(score_a: Any, grade_a: Any, grade_b: Any, params: EloParams) -> Any:
    """MoV multiplier of a bout: wins only (a gestellter Gang has no winner), using the
    winner's grade minus the loser's grade whichever side won."""
    score_a = np.asarray(score_a, dtype=float)
    ga, gb = np.asarray(grade_a, dtype=float), np.asarray(grade_b, dtype=float)
    lam = mov_multiplier(np.where(score_a >= 0.5, ga, gb), np.where(score_a >= 0.5, gb, ga),
                         params.mov_alpha, params.mov_baseline_diff,
                         params.mov_lambda_min, params.mov_lambda_max)
    lam = np.where(score_a == 0.5, 1.0, lam)
    return float(lam) if lam.ndim == 0 else lam


def revert_to_mean(rating: Any, delta: float, mean: float, times: int = 1) -> Any:
    """``R * (1 - delta) + mean * delta``, applied ``times`` times."""
    keep = (1.0 - delta) ** times
    return rating * keep + mean * (1.0 - keep)


def rating_year(date: Any, start_month: int = 4) -> Any:
    """Index of the rating year a date falls in: the calendar year, minus one before
    ``start_month``. A mean reversion is due whenever this index increases."""
    d = pd.DatetimeIndex(pd.to_datetime(np.atleast_1d(date)))
    out = np.asarray(d.year - (d.month < start_month), dtype=np.int64)
    return int(out[0]) if np.ndim(date) == 0 else out


def k_factor(category: str, k_factors: Mapping[str, float]) -> float:
    try:
        return float(k_factors[category])
    except KeyError:
        raise ValueError(f"no K-factor for festival category {category!r} "
                         f"(known: {', '.join(sorted(k_factors))})") from None


# --------------------------------------------------------------------------- result
@dataclass
class EloResult:
    """Output of :meth:`SchwingElo.run`.

    ``history``  one row per athlete and festival (:data:`HISTORY_COLUMNS`), sorted by
                 ``(date, fest_id, athlete_id)``.
    ``bouts``    one row per rated bout in processing order: the expected score of A the
                 update used (``expected_a``), the one from the pre-festival ratings
                 (``expected_a_prefest``), ``score_a``, ``k`` (effective, i.e. including
                 ``k_scale``), ``mov_lambda``, both
                 pre-festival ratings and career bout counts (for evaluation), and
                 ``delta_a``: the rating change the bout applied to A (B got ``-delta_a``),
                 the very value the update added, not a recomputation.
    ``ratings``  final rating per athlete id (after the reversions due up to the last
                 festival).
    ``as_of``    date of the last rated festival.
    """

    history: pd.DataFrame
    bouts: pd.DataFrame
    ratings: dict[str, float]
    as_of: pd.Timestamp | None
    params: EloParams


# --------------------------------------------------------------------------- engine
class SchwingElo:
    """ELO ratings for Schwingen.

    Two ways to use it: :meth:`rate_bout` / :meth:`apply_season_reversion` update the
    ratings one step at a time (the reference implementation of the rules), and
    :meth:`run` rates a whole bout table and returns the rating history.
    """

    def __init__(self, params: EloParams | None = None) -> None:
        self.params = params or EloParams()
        self.ratings: dict[str, float] = {}

    def rating(self, athlete_id: str) -> float:
        return self.ratings.get(athlete_id, self.params.initial)

    def expected(self, a: str, b: str) -> float:
        return float(expected_score(self.rating(a), self.rating(b), self.params.scale))

    def rate_bout(self, a: str, b: str, outcome: str, grade_a: float | None = None,
                  grade_b: float | None = None, category: str = "Regional") -> float:
        """Apply one bout; returns the rating change of ``a`` (``b`` gets the negative)."""
        if a == b:
            raise ValueError(f"bout of {a!r} against himself")
        p = self.params
        score = OUTCOME_SCORE[outcome]
        lam = bout_multiplier(score, _nan(grade_a), _nan(grade_b), p)
        delta = p.k(category) * lam * (score - self.expected(a, b))
        self.ratings[a] = self.rating(a) + delta
        self.ratings[b] = self.rating(b) - delta
        return delta

    def apply_season_reversion(self, times: int = 1) -> None:
        """Pull every rated athlete toward the mean (start of a rating year)."""
        p = self.params
        for athlete, r in self.ratings.items():
            self.ratings[athlete] = float(
                revert_to_mean(r, p.reversion_delta, p.reversion_mean, times))

    # ------------------------------------------------------------------ batch run
    def run(self, bouts: pd.DataFrame) -> EloResult:
        """Rate all eligible bouts from scratch (see the module docstring for the order
        of operations). Deterministic: the result depends only on the bout rows, not
        on their order in ``bouts``."""
        p = self.params
        df = _prepare(bouts, p.one_sided_weight)
        n = len(df)
        ids, inverse = np.unique(
            np.concatenate([df["athlete_a_id"].to_numpy(dtype=object),
                            df["athlete_b_id"].to_numpy(dtype=object)]).astype(str),
            return_inverse=True)
        ia, ib = inverse[:n], inverse[n:]
        score = df["outcome"].map(OUTCOME_SCORE).to_numpy(dtype=float)
        cats = df["category"].to_numpy(dtype=object)
        k_by_cat = {c: p.k(c) for c in pd.unique(cats)}
        k = np.array([k_by_cat[c] for c in cats], dtype=float)
        if p.one_sided_weight != 1.0:
            k = np.where(unlisted_opponent(df), k * p.one_sided_weight, k)
        lam = np.asarray(bout_multiplier(score, df["grade_a"].to_numpy(dtype=float),
                                         df["grade_b"].to_numpy(dtype=float), p),
                         dtype=float).reshape(n)
        weight = k * lam
        dates = df["date"].to_numpy(dtype="datetime64[D]")
        day = dates.astype(np.int64)
        ryear = rating_year(dates, p.season_start_month) if n else np.zeros(0, np.int64)
        season = dates.astype("datetime64[Y]").astype(np.int64) + 1970
        fest = df["fest_id"].to_numpy(dtype=np.int64)
        gang = df["gang_nr"].to_numpy(dtype=np.int64)

        rating = np.full(len(ids), p.initial, dtype=float)
        seen = np.zeros(len(ids), dtype=bool)
        n_before = np.zeros(len(ids), dtype=np.int64)
        last_day = np.full(len(ids), -1, dtype=np.int64)

        exp_used = np.empty(n)
        exp_pre = np.empty(n)
        delta = np.empty(n)  # per bout: the change applied to A (and, negated, to B)
        r_a_pre, r_b_pre = np.empty(n), np.empty(n)
        nb_a, nb_b = np.empty(n, np.int64), np.empty(n, np.int64)
        rows: list[dict[str, np.ndarray]] = []

        starts = np.flatnonzero(np.r_[True, (fest[1:] != fest[:-1]) | (day[1:] != day[:-1])]) \
            if n else np.zeros(0, np.int64)
        ends = np.r_[starts[1:], n]
        current_year: int | None = None
        for lo, hi in zip(starts.tolist(), ends.tolist()):
            if current_year is not None and ryear[lo] > current_year:
                rating[seen] = revert_to_mean(rating[seen], p.reversion_delta,
                                              p.reversion_mean, int(ryear[lo] - current_year))
            current_year = int(ryear[lo]) if current_year is None \
                else max(current_year, int(ryear[lo]))
            a, b = ia[lo:hi], ib[lo:hi]
            athletes, local = np.unique(np.concatenate([a, b]), return_inverse=True)
            before = rating[athletes].copy()
            r_a_pre[lo:hi], r_b_pre[lo:hi] = rating[a], rating[b]
            nb_a[lo:hi], nb_b[lo:hi] = n_before[a], n_before[b]
            exp_pre[lo:hi] = expected_score(rating[a], rating[b], p.scale)

            if p.update_mode == "sequential":
                _update_sequential(rating, a, b, score[lo:hi], weight[lo:hi], p.scale,
                                   exp_used[lo:hi], delta[lo:hi])
            else:
                for s, e in _blocks(gang[lo:hi], p):
                    ea = expected_score(rating[a[s:e]], rating[b[s:e]], p.scale)
                    exp_used[lo + s:lo + e] = ea
                    d = weight[lo + s:lo + e] * (score[lo + s:lo + e] - ea)
                    np.add.at(rating, a[s:e], d)
                    np.add.at(rating, b[s:e], -d)
                    delta[lo + s:lo + e] = d

            m = len(athletes)
            la, lb = local[:hi - lo], local[hi - lo:]
            count = np.bincount(local, minlength=m)
            got = np.bincount(la, score[lo:hi], m) + np.bincount(lb, 1.0 - score[lo:hi], m)
            exp = np.bincount(la, exp_used[lo:hi], m) + np.bincount(lb, 1.0 - exp_used[lo:hi], m)
            idle = np.where(last_day[athletes] >= 0, day[lo] - last_day[athletes], -1)
            rows.append({
                "athlete": athletes, "day": np.full(m, day[lo]), "fest_id": np.full(m, fest[lo]),
                "rating_before": before, "rating_after": rating[athletes].copy(),
                "season": np.full(m, season[lo]), "category": np.full(m, cats[lo], dtype=object),
                "n_bouts": count, "score": got, "expected": exp,
                "bouts_before": n_before[athletes].copy(), "days_inactive": idle})
            n_before[athletes] += count
            seen[athletes] = True
            last_day[athletes] = day[lo]

        self.ratings = {str(i): float(r) for i, r in zip(ids[seen], rating[seen])}
        history = _history_frame(rows, ids, p)
        detail = pd.DataFrame({
            "bout_id": df["bout_id"].to_numpy(), "fest_id": fest, "gang_nr": gang,
            "date": dates, "season": season, "category": cats,
            "athlete_a_id": df["athlete_a_id"].to_numpy(),
            "athlete_b_id": df["athlete_b_id"].to_numpy(),
            "score_a": score, "expected_a": exp_used, "expected_a_prefest": exp_pre,
            "k": k, "mov_lambda": lam, "rating_a_prefest": r_a_pre, "rating_b_prefest": r_b_pre,
            "bouts_before_a": nb_a, "bouts_before_b": nb_b, "delta_a": delta})
        as_of = pd.Timestamp(dates[-1]) if n else None
        return EloResult(history=history, bouts=detail, ratings=dict(self.ratings),
                         as_of=as_of, params=p)


# --------------------------------------------------------------------------- per bout
def bout_contributions(result: EloResult) -> pd.DataFrame:
    """What every rated bout contributed to each of its two athletes' ratings: two rows
    per bout (:data:`CONTRIBUTION_COLUMNS`), in processing order
    ``(date, fest_id, gang_nr, bout_id)``, side A before side B.

    ``delta`` is the change the engine applied (``EloResult.bouts.delta_a`` and its
    negative), ``score`` / ``expected`` are the athlete's own (they sum to 1 over the two
    sides), ``k`` is the effective K-factor and ``mov_lambda`` the margin multiplier, so
    ``delta = k * mov_lambda * (score - expected)``. Per athlete and festival the
    ``delta`` sum to ``rating_after - rating_before`` of the history (up to float
    addition), in every update mode. ``expected`` is the value the update used: from the
    pre-festival ratings in ``festival`` mode, from the ratings at that point of the
    festival in the other modes - only there is the running sum of ``delta`` in this
    order a rating the engine actually held.
    """
    b = result.bouts
    n = len(b)

    def both(a: Any, other: Any) -> np.ndarray:
        """Interleave: row 2i is side A of bout i, row 2i + 1 its side B."""
        a, other = np.asarray(a), np.asarray(other)
        out = np.empty(2 * n, dtype=a.dtype)
        out[0::2], out[1::2] = a, other
        return out

    def same(col: str) -> np.ndarray:
        return np.repeat(b[col].to_numpy(), 2)

    ida = b["athlete_a_id"].to_numpy(dtype=object)
    idb = b["athlete_b_id"].to_numpy(dtype=object)
    score = b["score_a"].to_numpy(dtype=float)
    exp = b["expected_a"].to_numpy(dtype=float)
    d = b["delta_a"].to_numpy(dtype=float)
    return pd.DataFrame({
        "bout_id": same("bout_id"), "fest_id": same("fest_id"), "date": same("date"),
        "gang_nr": same("gang_nr"),
        "side": np.tile(np.array(["A", "B"], dtype=object), n),
        "athlete_id": both(ida, idb), "opponent_id": both(idb, ida),
        "score": both(score, 1.0 - score), "expected": both(exp, 1.0 - exp),
        "k": same("k"), "mov_lambda": same("mov_lambda"),
        "delta": both(d, -d)})[list(CONTRIBUTION_COLUMNS)]


# --------------------------------------------------------------------------- helpers
def _nan(grade: float | None) -> float:
    return math.nan if grade is None else float(grade)


def unlisted_opponent(bouts: pd.DataFrame) -> np.ndarray:
    """Mask of the bouts flagged :data:`UNLISTED_FLAG` (all False without a ``flags``
    column)."""
    if "flags" not in bouts.columns:
        return np.zeros(len(bouts), dtype=bool)
    return bouts["flags"].fillna("").astype(str).str.split(",").map(
        lambda xs: UNLISTED_FLAG in xs).to_numpy(dtype=bool)


def rated_bouts(bouts: pd.DataFrame, one_sided_weight: float = 1.0) -> pd.DataFrame:
    """The bouts the engine rates: ``elo_eligible`` ones, without the bouts against an
    unlisted opponent when their weight is 0."""
    df = bouts
    if "elo_eligible" in df.columns:
        df = df[df["elo_eligible"].astype(bool)]
    if one_sided_weight == 0:
        df = df[~unlisted_opponent(df)]
    return df


def _prepare(bouts: pd.DataFrame, one_sided_weight: float = 1.0) -> pd.DataFrame:
    """Validate the bout table, keep the rated bouts, sort them chronologically."""
    missing = [c for c in BOUT_COLUMNS if c not in bouts.columns]
    if missing:
        raise ValueError(f"bouts: missing columns {missing}")
    df = rated_bouts(bouts, one_sided_weight)
    df = df.assign(date=pd.to_datetime(df["date"]))
    unknown = sorted(set(df["outcome"]) - set(OUTCOME_SCORE))
    if unknown:
        raise ValueError(f"bouts: unknown outcomes {unknown}")
    if df[["athlete_a_id", "athlete_b_id", "date", "category"]].isna().any().any():
        raise ValueError("bouts: eligible bouts need both athlete ids, a date and a category")
    if (df["athlete_a_id"] == df["athlete_b_id"]).any():
        raise ValueError("bouts: an athlete is paired with himself")
    if df["bout_id"].duplicated().any():
        raise ValueError("bouts: duplicate bout_id")
    return df.sort_values(["date", "fest_id", "gang_nr", "bout_id"],
                          kind="mergesort").reset_index(drop=True)


def _blocks(gang: np.ndarray, p: EloParams) -> list[tuple[int, int]]:
    """Slices of a festival's (Gang-sorted) bouts that are updated simultaneously."""
    n = len(gang)
    if p.update_mode == "festival":
        return [(0, n)]
    key = (gang > p.phase_split_gang).astype(np.int64) if p.update_mode == "phase" else gang
    cuts = np.flatnonzero(key[1:] != key[:-1]) + 1
    return list(zip(np.r_[0, cuts].tolist(), np.r_[cuts, n].tolist()))


def _update_sequential(rating: np.ndarray, a: np.ndarray, b: np.ndarray, score: np.ndarray,
                       weight: np.ndarray, scale: float, exp_out: np.ndarray,
                       delta_out: np.ndarray) -> None:
    """Bout-by-bout update in the given order (plain Python: clearer than fast)."""
    for j, (x, y, s, w) in enumerate(zip(a.tolist(), b.tolist(), score.tolist(),
                                         weight.tolist())):
        ea = 1.0 / (1.0 + 10.0 ** ((rating[y] - rating[x]) / scale))
        d = w * (s - ea)
        rating[x] += d
        rating[y] -= d
        exp_out[j] = ea
        delta_out[j] = d


def _history_frame(rows: list[dict[str, np.ndarray]], ids: np.ndarray,
                   p: EloParams) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame({c: [] for c in HISTORY_COLUMNS})
    cat = {key: np.concatenate([r[key] for r in rows]) for key in rows[0]}
    few = cat["bouts_before"] + cat["n_bouts"] < p.provisional_min_bouts
    inactive = cat["days_inactive"] > p.inactive_days
    reason = np.where(few & inactive, "few_bouts,inactive",
                      np.where(few, "few_bouts", np.where(inactive, "inactive", "")))
    history = pd.DataFrame({
        "athlete_id": ids[cat["athlete"]].astype(object),
        "date": cat["day"].astype("datetime64[D]").astype("datetime64[ns]"),
        "fest_id": cat["fest_id"], "rating_before": cat["rating_before"],
        "rating_after": cat["rating_after"], "season": cat["season"],
        "category": cat["category"], "n_bouts": cat["n_bouts"], "score": cat["score"],
        "expected": cat["expected"], "bouts_before": cat["bouts_before"],
        "days_inactive": pd.array(np.where(cat["days_inactive"] < 0, pd.NA,
                                           cat["days_inactive"]), dtype="Int64"),
        "provisional": few | inactive, "provisional_reason": reason.astype(object)})
    return history.sort_values(["date", "fest_id", "athlete_id"],
                               kind="mergesort").reset_index(drop=True)[list(HISTORY_COLUMNS)]
