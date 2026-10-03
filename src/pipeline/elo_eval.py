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

from src.pipeline.elo_engine import UPDATE_MODES, EloParams, EloResult, SchwingElo
from src.pipeline.elo_runner import compute, drop_low_confidence_bouts

if TYPE_CHECKING:
    from src.config import Config

TRAIN: tuple[int, int] = (2013, 2019)   # seasons used to choose parameters
TEST: tuple[int, int] = (2021, 2026)    # seasons used to check them
GANG_FLAGS: tuple[str, ...] = ("gang_collision", "gang_uncertain", "gang_inferred")
GAP_BINS: tuple[float, ...] = (0, 25, 50, 100, 150, 200, 300, 400, 500, 700, 5000)
EXPERIENCE_BINS: tuple[int, ...] = (0, 1, 6, 12, 18, 24, 36, 60, 120, 100000)


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


def parameter_scan(bouts: pd.DataFrame, params: EloParams, k_scales: Sequence[float],
                   deltas: Sequence[float]) -> pd.DataFrame:
    """Reference only (the spec fixes K and delta): predictive quality when all
    K-factors are scaled, and for other reversion strengths."""
    rows = []
    for c in k_scales:
        p = params.replace(k_factors={k: v * c for k, v in params.k_factors.items()})
        rows.append({"variant": f"K x {c:g}", **_train_test(SchwingElo(p).run(bouts).bouts)})
    for dl in deltas:
        d = SchwingElo(params.replace(reversion_delta=dl)).run(bouts).bouts
        rows.append({"variant": f"delta = {dl:g}", **_train_test(d)})
    return pd.DataFrame(rows).set_index("variant")


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
    """The evidence behind the Phase 4 decisions as plain text (about half a minute on
    the full data)."""
    params = EloParams.from_config(cfg)
    result: EloResult = SchwingElo(params).run(bouts)
    d = result.bouts
    neutral = params.replace(mov_alpha=0.0)
    base = mean_win_margin(bouts)
    out = [
        f"ELO evaluation - mode={params.update_mode}, alpha={params.mov_alpha:g}, "
        f"baseline_diff={params.mov_baseline_diff:g}, delta={params.reversion_delta:g}; "
        f"train seasons {TRAIN}, test seasons {TEST}",
        f"current model: train {prediction_metrics(in_seasons(d, TRAIN))}, "
        f"test {prediction_metrics(in_seasons(d, TEST))}",
        "\n== update modes (no MoV)", _fmt(compare_update_modes(bouts, neutral), 5),
        "\n== sensitivity to the unreliable Gang numbers (rating points)",
        _fmt(gang_noise_sensitivity(bouts, neutral), 3),
        f"\n== MoV grid (mean winner margin = {base:.4f})",
        _fmt(calibrate_mov(bouts, params, (0.0, 0.25, 0.5, 1.0, 1.5, 2.0),
                           (1.0, 1.25, round(base, 2), 1.5)), 5),
        "\n== reference only: scaled K-factors / other reversion strengths",
        _fmt(parameter_scan(bouts, params, (0.5, 1, 1.5, 2, 3, 4, 6),
                            (0.0, 0.05, 0.1, 0.2)), 5),
        "\n== prediction error per season", _fmt(season_metrics(d)),
        "\n== calibration by rating gap (favourite's view), seasons from "
        f"{TRAIN[0]}", _fmt(calibration_table(d[d["season"] >= TRAIN[0]]), 3),
        "\n== prediction error by experience of the less experienced athlete, seasons "
        f"from {TRAIN[0]}", _fmt(experience_table(d[d["season"] >= TRAIN[0]])),
        "\n== rating distribution of the athletes active in a season (season end)",
        _fmt(season_drift(result.history, params.provisional_min_bouts), 1),
    ]
    if identity_map is not None:
        top, summary = identity_sensitivity(bouts, athletes, identity_map, cfg)
        out += ["\n== identity sensitivity: without rows of confidence <= "
                f"{cfg.identity_low_confidence:g}",
                "  " + ", ".join(f"{k}={v:g}" for k, v in summary.items()),
                _fmt(top.head(30)[["full_name", "rating", "rank", "rating_strict", "rank_strict",
                                   "rating_diff", "identity_low_conf_rows"]], 1)]
    return "\n".join(out)
