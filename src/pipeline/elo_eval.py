"""Evaluation and calibration of the ELO model (Phase 4 evidence; ``elo --evaluate``).

Nothing here changes the ratings: these functions re-run the engine with other
parameters and measure how well the *pre-bout* expected scores predict the results.
Every prediction is made before its bout is rated, so all numbers are out-of-sample
with respect to the ratings; the parameters themselves are chosen on the earlier
seasons (``TRAIN``) and checked on the later ones (``TEST``).

Scores are ``S in {1, 0.5, 0}`` (about 20 % of the bouts are gestellt), predictions the
two-outcome expected score ``E``:

* Brier score ``mean((S - E)^2)`` — 0.25 * (1 - draw share) = 0.20 for ``E = 0.5``,
* log-loss ``-mean(S ln E + (1 - S) ln(1 - E))`` (a draw counts as half a win and half
  a loss) — 0.693 for ``E = 0.5``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Sequence

import numpy as np
import pandas as pd

from src.pipeline.elo_engine import (OUTCOME_SCORE, UNLISTED_FLAG, UPDATE_MODES, EloParams,
                                     EloResult, SchwingElo, unlisted_opponent)
from src.pipeline.elo_runner import compute, drop_low_confidence_bouts

if TYPE_CHECKING:
    from src.config import Config

TRAIN: tuple[int, int] = (2013, 2019)   # seasons used to choose parameters
TEST: tuple[int, int] = (2021, 2026)    # seasons used to check them
GANG_FLAGS: tuple[str, ...] = ("gang_collision", "gang_uncertain", "gang_inferred")
GAP_BINS: tuple[float, ...] = (0, 25, 50, 100, 150, 200, 300, 400, 500, 700, 5000)
EXPERIENCE_BINS: tuple[int, ...] = (0, 1, 6, 12, 18, 24, 36, 60, 120, 100000)
# Phase 10 (history before 2011): weights tried for the bouts against an unlisted opponent,
# the season windows their effect is read in, and the complete seasons that are cut like
# the old sheets to see which weight recovers the ratings of the complete data.
ONE_SIDED_WEIGHTS: tuple[float, ...] = (1.0, 0.5, 0.25, 0.0)
ONE_SIDED_WINDOWS: tuple[tuple[int, int], ...] = (
    (2005, 2007), (2008, 2010), (2011, 2012), TRAIN, TEST)
CENSOR_SEASONS: tuple[int, int] = (2011, 2016)
# (k_scale, reversion delta): the spec's values first (reference of the top-20 overlap),
# then the variants of the Phase 4 review grid.
SCALE_VARIANTS: tuple[tuple[float, float], ...] = (
    (1.0, 0.10), (1.0, 0.05), (1.5, 0.05), (2.0, 0.05), (2.0, 0.10), (3.0, 0.0))


# --------------------------------------------------------------------------- metrics
def prediction_metrics(detail: pd.DataFrame, column: str = "expected_a") -> dict[str, float]:
    """Brier score and log-loss of ``detail[column]`` against ``score_a``."""
    if detail.empty:
        return {"n": 0, "brier": float("nan"), "log_loss": float("nan")}
    e = detail[column].to_numpy(dtype=float).clip(1e-12, 1 - 1e-12)
    s = detail["score_a"].to_numpy(dtype=float)
    return {"n": len(detail), "brier": float(np.mean((s - e) ** 2)),
            "log_loss": float(-np.mean(s * np.log(e) + (1 - s) * np.log(1 - e)))}


def in_seasons(detail: pd.DataFrame, seasons: tuple[int, int]) -> pd.DataFrame:
    return detail[detail["season"].between(seasons[0], seasons[1])]


def _train_test(detail: pd.DataFrame, column: str = "expected_a") -> dict[str, float]:
    tr, te = (prediction_metrics(in_seasons(detail, s), column) for s in (TRAIN, TEST))
    return {"train_brier": tr["brier"], "train_log_loss": tr["log_loss"],
            "test_brier": te["brier"], "test_log_loss": te["log_loss"]}


# --------------------------------------------------------------------------- comparisons
def compare_update_modes(bouts: pd.DataFrame, params: EloParams) -> pd.DataFrame:
    """Predictive quality of the four update orders. ``*_prefest`` columns score every
    mode on the same task (predict a festival's bouts from the ratings before it), the
    plain ones on the expected scores each mode used itself (the sequential modes have
    seen the earlier Gänge of the festival)."""
    rows = []
    for mode in UPDATE_MODES:
        d = SchwingElo(params.replace(update_mode=mode)).run(bouts).bouts
        own, pre = _train_test(d), _train_test(d, "expected_a_prefest")
        rows.append({"mode": mode, **own, **{f"{k}_prefest": v for k, v in pre.items()}})
    return pd.DataFrame(rows).set_index("mode")


def perturb_gang_order(bouts: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    """A plausible alternative reading of the unreliable Gang numbers: every bout
    flagged ``gang_collision`` / ``gang_uncertain`` / ``gang_inferred`` is moved one Gang
    earlier or later (staying within the festival's Gänge)."""
    out = bouts.copy()
    flagged = out["flags"].fillna("").str.contains("|".join(GANG_FLAGS)).to_numpy()
    rng = np.random.default_rng(seed)
    step = rng.choice([-1, 1], size=len(out))
    top = out.groupby("fest_id")["gang_nr"].transform("max").to_numpy()
    gang = out["gang_nr"].to_numpy().astype(int)
    moved = np.where(gang + step < 1, gang + 1, np.where(gang + step > top, gang - 1, gang + step))
    out["gang_nr"] = np.where(flagged, np.clip(moved, 1, None), gang).astype(out["gang_nr"].dtype)
    return out


def gang_noise_sensitivity(bouts: pd.DataFrame, params: EloParams, seed: int = 0,
                           top_n: int = 100) -> pd.DataFrame:
    """How much the ratings move per update mode when the flagged Gang numbers are
    perturbed (:func:`perturb_gang_order`): difference of ``rating_after`` over all
    history rows and over the rows of the ``top_n`` athletes by final rating."""
    noisy = perturb_gang_order(bouts, seed)
    rows = []
    for mode in UPDATE_MODES:
        p = params.replace(update_mode=mode)
        one, two = SchwingElo(p).run(bouts), SchwingElo(p).run(noisy)
        key = ["athlete_id", "fest_id"]
        m = one.history[key + ["rating_after"]].merge(
            two.history[key + ["rating_after"]], on=key, suffixes=("", "_noisy"))
        diff = (m["rating_after"] - m["rating_after_noisy"]).abs()
        top = set(pd.Series(one.ratings).nlargest(top_n).index)
        rows.append({"mode": mode, "rows_changed": float((diff > 1e-9).mean()),
                     "mean_abs_diff": float(diff.mean()), "p99_abs_diff": float(diff.quantile(.99)),
                     "max_abs_diff": float(diff.max()),
                     "top_mean_abs_diff": float(diff[m["athlete_id"].isin(top)].mean()),
                     "top_max_abs_diff": float(diff[m["athlete_id"].isin(top)].max())})
    return pd.DataFrame(rows).set_index("mode")


def mean_win_margin(bouts: pd.DataFrame) -> float:
    """Mean grade difference winner - loser over the graded wins (the K-neutral
    ``baseline_diff``: with it the average multiplier of a win is 1)."""
    w = bouts[(bouts["outcome"] != "DRAW") & bouts["grade_a"].notna() & bouts["grade_b"].notna()]
    if "elo_eligible" in w.columns:
        w = w[w["elo_eligible"]]
    return float((w["grade_a"] - w["grade_b"]).abs().mean())


def calibrate_mov(bouts: pd.DataFrame, params: EloParams, alphas: Iterable[float],
                  baselines: Iterable[float]) -> pd.DataFrame:
    """Grid over ``alpha`` x ``baseline_diff``: train / test Brier and log-loss, and the
    mean multiplier of the wins (a mean above 1 is a hidden increase of K)."""
    rows = []
    for baseline in baselines:
        for alpha in alphas:
            d = SchwingElo(params.replace(mov_alpha=alpha, mov_baseline_diff=baseline)) \
                .run(bouts).bouts
            wins = d["score_a"] != 0.5
            rows.append({"alpha": alpha, "baseline_diff": baseline,
                         "mean_lambda": float(d.loc[wins, "mov_lambda"].mean()),
                         **_train_test(d)})
    return pd.DataFrame(rows)


def compare_update_modes_by_scale(bouts: pd.DataFrame, params: EloParams,
                                  k_scales: Sequence[float]) -> pd.DataFrame:
    """:func:`compare_update_modes` for several ``k_scale`` values (other parameters as
    configured, MoV included): does the choice of the update order depend on the level
    of K? ``*_prefest`` = all modes on equal information."""
    parts = []
    for c in k_scales:
        t = compare_update_modes(bouts, params.replace(k_scale=c)).reset_index()
        t.insert(0, "k_scale", c)
        parts.append(t[["k_scale", "mode", "train_brier", "test_brier",
                        "train_brier_prefest", "test_brier_prefest"]])
    return pd.concat(parts, ignore_index=True).set_index(["k_scale", "mode"])


def parameter_scan(bouts: pd.DataFrame, params: EloParams, k_scales: Sequence[float],
                   deltas: Sequence[float]) -> pd.DataFrame:
    """One parameter at a time around the configured model: predictive quality for
    other values of ``k_scale`` (absolute multipliers on the spec's K-factors) and for
    other reversion strengths."""
    rows = []
    for c in k_scales:
        p = params.replace(k_scale=c)
        rows.append({"variant": f"K x {c:g}", **_train_test(SchwingElo(p).run(bouts).bouts)})
    for dl in deltas:
        d = SchwingElo(params.replace(reversion_delta=dl)).run(bouts).bouts
        rows.append({"variant": f"delta = {dl:g}", **_train_test(d)})
    return pd.DataFrame(rows).set_index("variant")


def scale_grid(bouts: pd.DataFrame, athletes: pd.DataFrame,
               identity_map: pd.DataFrame | None, cfg: Config,
               variants: Sequence[tuple[float, float]] = SCALE_VARIANTS,
               top_n: int = 100) -> pd.DataFrame:
    """What ``k_scale`` and the reversion ``delta`` do to the leaderboard, per variant:
    test Brier; SD and best rating of the currently ranked athletes; overlap of the
    current top 20 with the first variant's; volatility = largest single-festival
    rating change in the whole history and, for the current top ``top_n``, the mean
    absolute change per festival, plus the mean size of the April drop at the top."""
    rows, reference = [], None
    for k_scale, delta in variants:
        params = EloParams.from_config(cfg, k_scale=k_scale, reversion_delta=delta)
        result, table, _ = compute(bouts, athletes, identity_map, cfg, params)
        ranked = table[table["ranked"]].sort_values("rank")
        top20 = set(ranked.head(20)["athlete_id"])
        reference = top20 if reference is None else reference
        h = result.history
        change = (h["rating_after"] - h["rating_before"]).abs()
        of_top = h["athlete_id"].isin(set(ranked.head(top_n)["athlete_id"]))
        rows.append({
            "k_scale": k_scale, "delta": delta,
            "test_brier": prediction_metrics(in_seasons(result.bouts, TEST))["brier"],
            "ranked": len(ranked), "sd_ranked": float(ranked["rating"].std()),
            "rank1": float(ranked["rating"].max()) if len(ranked) else float("nan"),
            "rank20": float(ranked["rating"].iloc[19]) if len(ranked) >= 20 else float("nan"),
            "top20_overlap": len(top20 & reference),
            "max_fest_change": float(change.max()),
            "p99_fest_change": float(change.quantile(0.99)),
            f"top{top_n}_mean_abs_change": float(change[of_top].mean()),
            "top20_april_drop": float(delta * (ranked.head(20)["rating"].mean()
                                               - params.reversion_mean))})
    return pd.DataFrame(rows).set_index(["k_scale", "delta"])


# --------------------------------------------------------------------------- one-sided bouts
def compare_one_sided_weights(bouts: pd.DataFrame, params: EloParams,
                              weights: Sequence[float] = ONE_SIDED_WEIGHTS,
                              windows: Sequence[tuple[int, int]] = ONE_SIDED_WINDOWS
                              ) -> pd.DataFrame:
    """Brier score of the *two-sided* bouts per season window when the bouts against an
    unlisted opponent (sheets before 2011) count with weight ``w`` (0 = not rated): what
    do the one-sided bouts add to the prediction of the bouts known from both sides?"""
    one_sided = set(bouts.loc[unlisted_opponent(bouts), "bout_id"])
    rows = []
    for w in weights:
        d = SchwingElo(params.replace(one_sided_weight=w)).run(bouts).bouts
        d = d[~d["bout_id"].isin(one_sided)]
        rows.append({"weight": w, **{f"{lo}-{hi}": prediction_metrics(
            in_seasons(d, (lo, hi)))["brier"] for lo, hi in windows}})
    return pd.DataFrame(rows).set_index("weight")


def festival_gang_counts(bouts: pd.DataFrame) -> pd.DataFrame:
    """Per festival and athlete: his number of bouts ``n`` and whether he wrestled the
    whole festival (``finished``: at least as many bouts as the largest number that 10 %
    of the field reach - the festival's Gang count)."""
    long = pd.concat([
        bouts[["fest_id", "athlete_a_id"]].rename(columns={"athlete_a_id": "athlete_id"}),
        bouts[["fest_id", "athlete_b_id"]].rename(columns={"athlete_b_id": "athlete_id"})])
    cnt = long.groupby(["fest_id", "athlete_id"]).size().rename("n").reset_index()

    def gaenge(n: pd.Series) -> int:
        at_least = n.value_counts().sort_index(ascending=False).cumsum()
        return int(at_least[at_least >= 0.10 * len(n)].index.max())

    cnt["finished"] = cnt["n"] >= cnt["fest_id"].map(cnt.groupby("fest_id")["n"].apply(gaenge))
    return cnt


def censor_like_old_sheets(bouts: pd.DataFrame) -> pd.DataFrame:
    """Cut complete festivals down to what a sheet before 2011 prints: only the athletes
    who finished the festival have a block. Bouts among the others disappear, a bout
    between a finisher and a non-finisher keeps its outcome, loses the non-finisher's
    grade and is flagged ``one_sided,unlisted_opponent``."""
    cnt = festival_gang_counts(bouts)
    fin = set(zip(cnt.loc[cnt["finished"], "fest_id"], cnt.loc[cnt["finished"], "athlete_id"]))
    fa = np.fromiter(((f, x) in fin for f, x in zip(bouts["fest_id"], bouts["athlete_a_id"])),
                     dtype=bool, count=len(bouts))
    fb = np.fromiter(((f, x) in fin for f, x in zip(bouts["fest_id"], bouts["athlete_b_id"])),
                     dtype=bool, count=len(bouts))
    out = bouts[fa | fb].copy()
    fa, fb = fa[fa | fb], fb[fa | fb]
    cross = fa ^ fb
    out["flags"] = np.where(cross, f"one_sided,{UNLISTED_FLAG}",
                            out["flags"].fillna("") if "flags" in out.columns else "")
    out["grade_a"] = out["grade_a"].astype(float).where(~(cross & ~fa))
    out["grade_b"] = out["grade_b"].astype(float).where(~(cross & ~fb))
    return out


def censoring_experiment(bouts: pd.DataFrame, params: EloParams,
                         seasons: tuple[int, int] = CENSOR_SEASONS,
                         weights: Sequence[float] = ONE_SIDED_WEIGHTS,
                         check: tuple[int, int] | None = None, top_n: int = 100,
                         min_festivals: int = 5) -> pd.DataFrame:
    """Which weight of the one-sided bouts recovers the ratings of complete sheets?

    The Kranzfeste of ``seasons`` (complete sheets) are cut like the old sheets
    (:func:`censor_like_old_sheets`); bouts before ``seasons`` are left out, later ones
    stay. Reference = the same data uncut. Per weight: Brier score of the (complete)
    seasons ``check`` after the cut ones, and the season-end ratings of the last cut
    season against the reference - RMSE over all athletes, and the mean difference
    (``bias_*``) of the athletes who always finished, of those who mostly did not, and of
    the reference's top ``top_n``. First row (``weight`` NaN) = the reference itself."""
    lo, hi = seasons
    check = check or (hi + 1, hi + 3)
    year = pd.to_datetime(bouts["date"]).dt.year
    eligible = bouts["elo_eligible"].astype(bool) if "elo_eligible" in bouts.columns else True
    b = bouts[(year >= lo) & eligible]
    year = year[b.index]
    old = b[(year <= hi) & (b["category"] != "Regional")]
    later = b[year > hi]
    cut = censor_like_old_sheets(old)

    def season_end(result: EloResult) -> pd.Series:
        h = result.history[result.history["season"] <= hi]
        return h.groupby("athlete_id")["rating_after"].last()

    reference = SchwingElo(params).run(pd.concat([old, later]))
    ref_end = season_end(reference)
    cnt = festival_gang_counts(old)
    share = cnt.groupby("athlete_id")["finished"].agg(["mean", "size"])
    often = share[share["size"] >= min_festivals]
    groups = {"finishers": often[often["mean"] == 1].index,
              "eliminated": often[often["mean"] < 0.5].index,
              f"top{top_n}": ref_end.nlargest(top_n).index}
    scores = OUTCOME_SCORE
    cross = cut[unlisted_opponent(cut)]
    rows = [{"weight": float("nan"), "one_sided_share": len(cross) / max(len(cut), 1),
             "one_sided_draws": float((cross["outcome"].map(scores) == 0.5).mean())
             if len(cross) else float("nan"),
             "check_brier": prediction_metrics(in_seasons(reference.bouts, check))["brier"],
             "sd": float(ref_end.std())}]
    for w in weights:
        r = SchwingElo(params.replace(one_sided_weight=w)).run(pd.concat([cut, later]))
        end = season_end(r)
        diff = (end - ref_end.reindex(end.index)).dropna()
        row = {"weight": w,
               "check_brier": prediction_metrics(in_seasons(r.bouts, check))["brier"],
               "sd": float(end.std()), "rmse": float(np.sqrt((diff ** 2).mean()))}
        for name, ids in groups.items():
            part = diff.reindex(ids).dropna()
            row[f"bias_{name}"] = float(part.mean()) if len(part) else float("nan")
        rows.append(row)
    return pd.DataFrame(rows).set_index("weight")


def cold_start_table(bouts: pd.DataFrame, params: EloParams, starts: Sequence[int],
                     seasons_after: int = 6, min_bouts: int = 24, top_n: int = 100
                     ) -> pd.DataFrame:
    """How long does a cold start (everybody at the initial rating) distort the ratings?

    For every first season in ``starts`` the bouts before it are left out and the
    season-end ratings of its first ``seasons_after`` seasons are compared with those of
    the full history: overlap of the top 20 (among the athletes with ``min_bouts`` bouts
    in the full history), rank correlation over the full history's top ``top_n``, their
    mean rating difference (``shift``: the level of the scale) and the Brier score of
    the season in both runs."""
    year = pd.to_datetime(bouts["date"]).dt.year

    def season_end(result: EloResult, season: int) -> tuple[pd.Series, pd.Series]:
        h = result.history[result.history["season"] == season]
        last = h.groupby("athlete_id").tail(1).set_index("athlete_id")
        return last["rating_after"], last["bouts_before"] + last["n_bouts"]

    def season_brier(result: EloResult, season: int) -> float:
        return prediction_metrics(in_seasons(result.bouts, (season, season)))["brier"]

    full = SchwingElo(params).run(bouts)
    last_season = int(year.max()) if len(year) else 0
    rows = []
    for start in starts:
        late = SchwingElo(params).run(bouts[year >= start])
        for season in range(start, min(start + seasons_after, last_season + 1)):
            ref, n = season_end(full, season)
            ref = ref[n >= min_bouts]
            own = season_end(late, season)[0].reindex(ref.index).dropna()
            ref = ref.reindex(own.index)
            if len(ref) < 2:
                continue
            top = ref.nlargest(top_n).index
            rows.append({
                "start": start, "season": season, "seasons_since_start": season - start + 1,
                "top20_overlap": len(set(ref.nlargest(20).index) & set(own.nlargest(20).index)),
                "rank_corr": float(ref.reindex(top).rank().corr(own.reindex(top).rank())),
                "shift": float((own - ref).reindex(top).mean()),
                "brier": season_brier(late, season), "brier_full": season_brier(full, season)})
    return pd.DataFrame(rows).set_index(["start", "season"]) if rows else pd.DataFrame()


def scale_without_category(bouts: pd.DataFrame, params: EloParams,
                           category: str = "Regional", min_bouts: int = 0) -> pd.DataFrame:
    """Top of the scale per season (mean of the 20 best season-end ratings, SD of the
    athletes active in the season) with all bouts and without the festivals of
    ``category`` (``*_without``): how much of a change of the scale comes with a change
    of what is recorded - Regional festivals exist in the data from 2012 only."""
    full = season_drift(SchwingElo(params).run(bouts).history, min_bouts)
    rest = season_drift(SchwingElo(params).run(bouts[bouts["category"] != category]).history,
                        min_bouts)
    return full[["athletes", "sd", "top20_mean"]].join(
        rest[["athletes", "sd", "top20_mean"]], rsuffix="_without")


# --------------------------------------------------------------------------- diagnostics
def calibration_table(detail: pd.DataFrame, bins: Sequence[float] = GAP_BINS) -> pd.DataFrame:
    """Predicted vs observed score of the higher-rated athlete by pre-festival rating
    gap, with the observed win / draw / loss shares."""
    fav_is_a = detail["expected_a"].to_numpy() >= 0.5
    pred = np.where(fav_is_a, detail["expected_a"], 1 - detail["expected_a"])
    obs = np.where(fav_is_a, detail["score_a"], 1 - detail["score_a"])
    gap = (detail["rating_a_prefest"] - detail["rating_b_prefest"]).abs().to_numpy()
    df = pd.DataFrame({"gap": pd.cut(gap, list(bins), right=False), "predicted": pred,
                       "observed": obs, "win": obs == 1, "draw": obs == 0.5, "loss": obs == 0})
    out = df.groupby("gap", observed=True).agg(
        n=("predicted", "size"), predicted=("predicted", "mean"), observed=("observed", "mean"),
        win=("win", "mean"), draw=("draw", "mean"), loss=("loss", "mean"))
    # rating gap at which the logistic curve would give the observed score
    odds = out["observed"].clip(1e-6, 1 - 1e-6)
    out["implied_gap"] = 400 * np.log10(odds / (1 - odds))
    return out


def experience_table(detail: pd.DataFrame, bins: Sequence[int] = EXPERIENCE_BINS) -> pd.DataFrame:
    """Prediction error by the career bouts of the less experienced athlete before the
    festival (basis for ``provisional_min_bouts``)."""
    least = np.minimum(detail["bouts_before_a"], detail["bouts_before_b"]).to_numpy()
    rows = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        part = detail[(least >= lo) & (least < hi)]
        rows.append({"bouts_before": f"{lo}-{hi - 1}" if hi < 100000 else f"{lo}+",
                     **prediction_metrics(part)})
    return pd.DataFrame(rows).set_index("bouts_before")


def season_metrics(detail: pd.DataFrame) -> pd.DataFrame:
    """Brier / log-loss per calendar year (burn-in evidence)."""
    return pd.DataFrame({int(s): prediction_metrics(part)
                         for s, part in detail.groupby("season")}).T


def season_drift(history: pd.DataFrame, min_bouts: int = 0) -> pd.DataFrame:
    """Inflation / deflation: distribution of the season-end ratings of the athletes
    active in a calendar year, the newcomers' share and where they end the year."""
    last = history.groupby(["athlete_id", "season"], sort=True).tail(1)
    first_season = history.groupby("athlete_id")["season"].min()
    last = last.assign(newcomer=last["season"].to_numpy()
                       == last["athlete_id"].map(first_season).to_numpy(),
                       total=last["bouts_before"] + last["n_bouts"])
    rows = []
    for season, part in last.groupby("season"):
        r = part["rating_after"]
        est = part.loc[part["total"] >= min_bouts, "rating_after"]
        rows.append({"season": int(season), "athletes": len(part), "mean": r.mean(),
                     "sd": r.std(), "median": r.median(), "p90": r.quantile(.9),
                     "p99": r.quantile(.99), "max": r.max(),
                     "top20_mean": r.nlargest(20).mean(),
                     "established_mean": est.mean(),
                     "newcomers": int(part["newcomer"].sum()),
                     "newcomer_mean": part.loc[part["newcomer"], "rating_after"].mean()})
    return pd.DataFrame(rows).set_index("season")


def identity_sensitivity(bouts: pd.DataFrame, athletes: pd.DataFrame,
                         identity_map: pd.DataFrame, cfg: Config,
                         top_n: int = 100) -> tuple[pd.DataFrame, dict[str, float]]:
    """Sensitivity pass: ratings with and without the bouts of low-confidence identity
    rows. Returns the current top ``top_n`` (full run) with both ratings / ranks, and
    summary numbers over all ranked athletes."""
    _, full, _ = compute(bouts, athletes, identity_map, cfg)
    kept = drop_low_confidence_bouts(bouts, identity_map, cfg.identity_low_confidence)
    _, strict, _ = compute(kept, athletes, identity_map, cfg)
    m = full[["athlete_id", "full_name", "rating", "rank", "n_bouts", "identity_uncertain",
              "identity_low_conf_rows"]].merge(
        strict[["athlete_id", "rating", "rank", "n_bouts"]], on="athlete_id",
        suffixes=("", "_strict"))
    m["rating_diff"] = m["rating_strict"] - m["rating"]
    m["rank_diff"] = m["rank_strict"].astype("Float64") - m["rank"].astype("Float64")
    ranked = m[m["rank"].notna()]
    top = ranked.sort_values("rank").head(top_n)
    eligible = bouts[bouts["elo_eligible"]] if "elo_eligible" in bouts.columns else bouts
    kept_eligible = kept[kept["elo_eligible"]] if "elo_eligible" in kept.columns else kept
    summary = {
        "bouts_dropped": float(len(eligible) - len(kept_eligible)),
        "ranked": float(len(ranked)),
        "mean_abs_rating_diff": float(ranked["rating_diff"].abs().mean()),
        "max_abs_rating_diff": float(ranked["rating_diff"].abs().max()),
        "moved_over_10_points": float((ranked["rating_diff"].abs() > 10).sum()),
        f"top{top_n}_mean_abs_rating_diff": float(top["rating_diff"].abs().mean()),
        f"top{top_n}_max_abs_rating_diff": float(top["rating_diff"].abs().max()),
        f"top{top_n}_max_abs_rank_diff": float(
            top["rank_diff"].abs().astype("float64").max()),
        f"top{top_n}_dropped_out": float(top["rank_strict"].isna().sum()),
        "top20_same_set": float(set(top.head(20)["athlete_id"]) == set(
            m[m["rank_strict"].notna()].sort_values("rank_strict").head(20)["athlete_id"])),
    }
    return top.reset_index(drop=True), summary


# --------------------------------------------------------------------------- report
def _fmt(df: pd.DataFrame, digits: int = 4) -> str:
    with pd.option_context("display.width", 250, "display.max_columns", 40,
                           "display.max_rows", 200):
        return df.round(digits).to_string()


def evaluation_report(bouts: pd.DataFrame, athletes: pd.DataFrame,
                      identity_map: pd.DataFrame | None, cfg: Config) -> str:
    """The evidence behind the Phase 4 decisions as plain text (one to two minutes on
    the full data)."""
    params = EloParams.from_config(cfg)
    result: EloResult = SchwingElo(params).run(bouts)
    d = result.bouts
    neutral = params.replace(mov_alpha=0.0)
    base = mean_win_margin(bouts)
    out = [
        f"ELO evaluation - mode={params.update_mode}, K x {params.k_scale:g}, "
        f"alpha={params.mov_alpha:g}, "
        f"baseline_diff={params.mov_baseline_diff:g}, delta={params.reversion_delta:g}; "
        f"train seasons {TRAIN}, test seasons {TEST}",
        f"current model: train {prediction_metrics(in_seasons(d, TRAIN))}, "
        f"test {prediction_metrics(in_seasons(d, TEST))}",
        "\n== update modes (no MoV)", _fmt(compare_update_modes(bouts, neutral), 5),
        "\n== update modes by K scale (MoV on; *_prefest = equal information)",
        _fmt(compare_update_modes_by_scale(bouts, params, (1.0, 2.0, 3.0)), 5),
        "\n== sensitivity to the unreliable Gang numbers (rating points)",
        _fmt(gang_noise_sensitivity(bouts, neutral), 3),
        f"\n== MoV grid (mean winner margin = {base:.4f})",
        _fmt(calibrate_mov(bouts, params, (0.0, 0.25, 0.5, 1.0, 1.5, 2.0),
                           (1.0, 1.25, round(base, 2), 1.5)), 5),
        "\n== one parameter at a time: K scale (x spec K-factors) / reversion delta",
        _fmt(parameter_scan(bouts, params, (0.5, 1, 1.5, 2, 3, 4, 6),
                            (0.0, 0.05, 0.1, 0.2)), 5),
        "\n== K scale x delta: prediction, spread and volatility (first row = spec)",
        _fmt(scale_grid(bouts, athletes, identity_map, cfg), 4),
        "\n== prediction error per season", _fmt(season_metrics(d)),
        "\n== calibration by rating gap (favourite's view), seasons from "
        f"{TRAIN[0]}", _fmt(calibration_table(d[d["season"] >= TRAIN[0]]), 3),
        "\n== prediction error by experience of the less experienced athlete, seasons "
        f"from {TRAIN[0]}", _fmt(experience_table(d[d["season"] >= TRAIN[0]])),
        "\n== rating distribution of the athletes active in a season (season end)",
        _fmt(season_drift(result.history, params.provisional_min_bouts), 1),
    ]
    first = int(d["season"].min()) if len(d) else 0
    starts = [y for y in (first + 1, first + 2, first + 4, first + 7)
              if y <= int(d["season"].max()) - 1] if len(d) else []
    if starts:
        out += ["\n== cold start: a first season later than the data's, against the full "
                "history (shift = rating difference of the full history's top 100)",
                _fmt(cold_start_table(bouts, params, starts), 4)]
    out += ["\n== scale per season with and without the Regional festivals (recorded from "
            "2012)", _fmt(scale_without_category(bouts, params), 1)]
    if unlisted_opponent(bouts).any():
        out += ["\n== one-sided bouts (opponent not printed): Brier of the two-sided bouts "
                "by weight", _fmt(compare_one_sided_weights(bouts, params), 5)]
        seasons = d["season"]
        if seasons.min() <= CENSOR_SEASONS[0] and seasons.max() > CENSOR_SEASONS[1]:
            out += [f"\n== complete Kranzfest sheets of {CENSOR_SEASONS} cut like the old "
                    "sheets: ratings at the end of the cut seasons against the uncut data "
                    "(first row = uncut)",
                    _fmt(censoring_experiment(bouts, params), 4)]
    if identity_map is not None:
        top, summary = identity_sensitivity(bouts, athletes, identity_map, cfg)
        out += ["\n== identity sensitivity: without rows of confidence <= "
                f"{cfg.identity_low_confidence:g}",
                "  " + ", ".join(f"{k}={v:g}" for k, v in summary.items()),
                _fmt(top.head(30)[["full_name", "rating", "rank", "rating_strict", "rank_strict",
                                   "rating_diff", "identity_low_conf_rows"]], 1)]
    return "\n".join(out)
