"""SchwingElo engine (spec §4.2): one test per rule, then runs on the --sample data and
sanity checks on the real data (skipped when ``data/processed`` is absent)."""

from __future__ import annotations

import datetime as dt
import itertools
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from src import cli
from src.config import Config, DEFAULT_K_FACTORS, load_config
from src.pipeline import elo_engine as ee
from src.pipeline import elo_eval as ev
from src.pipeline import elo_runner as er
from src.pipeline.elo_engine import EloParams, SchwingElo

K = dict(DEFAULT_K_FACTORS)


def params(**kw: object) -> EloParams:
    base: dict[str, object] = dict(k_factors=K, mov_alpha=0.0, update_mode="festival")
    base.update(kw)
    return EloParams(**base)  # type: ignore[arg-type]


_ids = itertools.count()


def bout(a: str, b: str, outcome: str = "WIN_A", *, fest: int = 1, date: str = "2015-06-01",
         gang: int = 1, cat: str = "Regional", ga: float | None = 10.0,
         gb: float | None = 8.75, eligible: bool = True) -> dict[str, object]:
    return {"bout_id": f"{fest}-{gang}-{next(_ids):05d}", "fest_id": fest, "gang_nr": gang,
            "athlete_a_id": a, "athlete_b_id": b, "outcome": outcome, "grade_a": ga,
            "grade_b": gb, "date": date, "category": cat, "elo_eligible": eligible}


def frame(*bouts: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(list(bouts))


def mirrored(df: pd.DataFrame) -> pd.DataFrame:
    """The same bouts with the A / B sides swapped."""
    out = df.copy()
    out["athlete_a_id"], out["athlete_b_id"] = df["athlete_b_id"], df["athlete_a_id"]
    out["grade_a"], out["grade_b"] = df["grade_b"], df["grade_a"]
    out["outcome"] = df["outcome"].map({"WIN_A": "WIN_B", "WIN_B": "WIN_A", "DRAW": "DRAW"})
    return out


def final(df: pd.DataFrame, **kw: object) -> dict[str, float]:
    return SchwingElo(params(**kw)).run(df).ratings


def random_bouts(seed: int = 7, n_athletes: int = 30, n_fests: int = 40) -> pd.DataFrame:
    """A random multi-season schedule: 4 Gänge per festival, random pairings."""
    rng = np.random.default_rng(seed)
    names = [f"athlete-{i:02d}" for i in range(n_athletes)]
    cats = list(K)
    rows = []
    for f in range(n_fests):
        date = dt.date(2012, 1, 15) + dt.timedelta(days=int(f * 45 + rng.integers(0, 20)))
        field = rng.choice(names, size=int(rng.integers(8, n_athletes + 1) // 2 * 2),
                           replace=False)
        for gang in range(1, 5):
            order = rng.permutation(field)
            for a, b in zip(order[::2], order[1::2]):
                outcome = str(rng.choice(["WIN_A", "WIN_B", "DRAW"], p=[0.6, 0.15, 0.25]))
                win, lose = float(rng.choice([9.75, 10.0])), float(rng.choice([8.5, 8.75]))
                ga, gb = {"WIN_A": (win, lose), "WIN_B": (lose, win), "DRAW": (8.75, 8.75)}[outcome]
                rows.append(bout(str(a), str(b), outcome, fest=100 + f,
                                 date=date.isoformat(), gang=gang, cat=cats[f % len(cats)],
                                 ga=ga if rng.random() > 0.05 else None, gb=gb))
    return frame(*rows)


# =========================================================================== task 1: core
def test_expected_score_formula() -> None:
    assert ee.expected_score(1500, 1500) == pytest.approx(0.5)
    assert ee.expected_score(1900, 1500) == pytest.approx(10 / 11)
    assert ee.expected_score(1500, 1900) == pytest.approx(1 / 11)
    assert ee.expected_score(1600, 1500, scale=200) == pytest.approx(1 / (1 + 10 ** -0.5))
    e = ee.expected_score(np.array([1500.0, 1700.0]), np.array([1700.0, 1500.0]))
    assert e.sum() == pytest.approx(1.0)  # E_A + E_B = 1


@pytest.mark.parametrize("outcome, score", [("WIN_A", 1.0), ("DRAW", 0.5), ("WIN_B", 0.0)])
def test_win_draw_loss_update(outcome: str, score: float) -> None:
    elo = SchwingElo(params())
    elo.ratings = {"a": 1600.0, "b": 1500.0}
    e = 1 / (1 + 10 ** (-100 / 400))
    delta = elo.rate_bout("a", "b", outcome, 10.0, 8.75, "Regional")
    assert delta == pytest.approx(16 * (score - e))
    assert elo.ratings["a"] == pytest.approx(1600 + delta)
    assert elo.ratings["b"] == pytest.approx(1500 - delta)  # zero-sum per bout


def test_draw_moves_rating_toward_the_weaker_side() -> None:
    elo = SchwingElo(params())
    elo.ratings = {"fav": 1700.0, "dog": 1500.0}
    assert elo.rate_bout("fav", "dog", "DRAW") < 0  # a gestellter Gang costs the favourite
    assert elo.rating("dog") > 1500


def test_new_athletes_start_at_initial_rating() -> None:
    elo = SchwingElo(params(initial=1400.0))
    assert elo.rating("nobody") == 1400.0
    elo.rate_bout("a", "b", "DRAW")
    assert elo.ratings == {"a": 1400.0, "b": 1400.0}  # equal ratings, draw: no change


@pytest.mark.parametrize("category, k", sorted(DEFAULT_K_FACTORS.items()))
def test_k_factor_by_festival_category(category: str, k: float) -> None:
    r = final(frame(bout("a", "b", "WIN_A", cat=category)))
    assert r["a"] - 1500 == pytest.approx(k * 0.5)
    assert r["b"] - 1500 == pytest.approx(-k * 0.5)


def test_k_factor_values_follow_spec_and_decisions() -> None:
    assert K == {"ESAF": 48, "Bergkranz": 40, "Teilverband": 32, "Kantonal": 24,
                 "Gauverband": 24, "Regional": 16}


@pytest.mark.parametrize("category, k", sorted(DEFAULT_K_FACTORS.items()))
def test_k_scale_multiplies_every_k_factor(category: str, k: float) -> None:
    """One multiplier for all tiers: effective K = k_scale * K[category]."""
    r = final(frame(bout("a", "b", "WIN_A", cat=category)), k_scale=2.0)
    assert r["a"] - 1500 == pytest.approx(2.0 * k * 0.5)
    assert r["b"] - 1500 == pytest.approx(-2.0 * k * 0.5)
    assert params(k_scale=2.0).k(category) == 2.0 * k
    assert params().k(category) == k  # default scale 1: the spec's value


def test_k_scale_two_gives_the_decided_effective_k_factors() -> None:
    p = params(k_scale=2.0)
    assert {c: p.k(c) for c in K} == {"ESAF": 96, "Bergkranz": 80, "Teilverband": 64,
                                      "Kantonal": 48, "Gauverband": 48, "Regional": 32}
    assert p.k_factors == K  # the base values stay as decided


def test_k_scale_applies_to_the_step_by_step_rule_and_with_mov() -> None:
    elo = SchwingElo(params(k_scale=2.0, mov_alpha=0.4, mov_baseline_diff=1.25))
    elo.ratings = {"a": 1600.0, "b": 1500.0}
    e = 1 / (1 + 10 ** (-100 / 400))
    delta = elo.rate_bout("a", "b", "WIN_A", 10.0, 8.5, "Kantonal")
    assert delta == pytest.approx(2.0 * 24 * 1.1 * (1 - e))
    assert sum(elo.ratings.values()) == pytest.approx(3100.0)  # still zero-sum
    draw = SchwingElo(params(k_scale=2.0))
    draw.ratings = {"a": 1600.0, "b": 1500.0}
    assert draw.rate_bout("a", "b", "DRAW") == pytest.approx(2.0 * 16 * (0.5 - e))


@pytest.mark.parametrize("mode", ee.UPDATE_MODES)
def test_k_scale_equals_scaled_k_factors_and_one_is_neutral(mode: str) -> None:
    df = random_bouts()
    kw = dict(update_mode=mode, mov_alpha=1.0, mov_baseline_diff=1.36)
    plain = SchwingElo(params(**kw)).run(df)
    one = SchwingElo(params(k_scale=1.0, **kw)).run(df)
    pd.testing.assert_frame_equal(one.history, plain.history)  # k_scale = 1: unchanged
    scaled = SchwingElo(params(k_scale=2.0, **kw)).run(df)
    doubled = SchwingElo(params(k_factors={c: 2 * k for c, k in K.items()}, **kw)).run(df)
    pd.testing.assert_frame_equal(scaled.history, doubled.history)
    assert (scaled.bouts["k"] == 2 * plain.bouts["k"]).all()  # reported K is the effective one
    change = (scaled.history["rating_after"] - scaled.history["rating_before"]).groupby(
        scaled.history["fest_id"]).sum()
    assert np.allclose(change, 0.0, atol=1e-9)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_k_scale_must_be_positive_and_finite(bad: float) -> None:
    with pytest.raises(ValueError, match="k scale"):
        params(k_scale=bad)


def test_unknown_category_is_an_error() -> None:
    with pytest.raises(ValueError, match="K-factor"):
        final(frame(bout("a", "b", cat="Dorffest")))


@pytest.mark.parametrize("bad, match", [
    (dict(outcome="FORFEIT"), "unknown outcomes"),
    (dict(athlete_b_id="a"), "himself"),
    (dict(category=None), "need both athlete ids"),
])
def test_invalid_bouts_are_rejected(bad: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        final(frame({**bout("a", "b"), **bad}))


def test_duplicate_bout_id_and_missing_columns_are_rejected() -> None:
    row = bout("a", "b")
    with pytest.raises(ValueError, match="duplicate bout_id"):
        final(frame(row, row))
    with pytest.raises(ValueError, match="missing columns"):
        final(frame(row).drop(columns=["gang_nr"]))


def test_ineligible_bouts_are_ignored() -> None:
    r = final(frame(bout("a", "b"), bout("a", "c", "WIN_B", fest=2, eligible=False)))
    assert set(r) == {"a", "b"} and r["a"] == pytest.approx(1508)


@pytest.mark.parametrize("mode", ee.UPDATE_MODES)
def test_zero_sum_and_mean_preserved(mode: str) -> None:
    res = SchwingElo(params(update_mode=mode, mov_alpha=0.4, mov_baseline_diff=1.25)).run(
        random_bouts())
    h = res.history
    per_fest = (h["rating_after"] - h["rating_before"]).groupby(h["fest_id"]).sum()
    assert np.allclose(per_fest, 0.0, atol=1e-9)  # zero-sum per festival (and per bout)
    assert np.mean(list(res.ratings.values())) == pytest.approx(1500.0)


@pytest.mark.parametrize("mode", ee.UPDATE_MODES)
def test_model_is_symmetric_in_a_and_b(mode: str) -> None:
    """A is the better-ranked athlete on the sheet; the rating must not depend on it."""
    df = random_bouts()
    kw = dict(update_mode=mode, mov_alpha=0.4, mov_baseline_diff=1.25)
    one, two = final(df, **kw), final(mirrored(df), **kw)
    assert one.keys() == two.keys()
    assert all(one[a] == pytest.approx(two[a], abs=1e-9) for a in one)
    # swapping only some of the bouts must not matter either
    half = df.copy()
    half.iloc[::2] = mirrored(df).iloc[::2]
    three = final(half, **kw)
    assert all(one[a] == pytest.approx(three[a], abs=1e-9) for a in one)


def test_expected_scores_mirror() -> None:
    df = random_bouts()
    one = SchwingElo(params()).run(df).bouts.set_index("bout_id")
    two = SchwingElo(params()).run(mirrored(df)).bouts.set_index("bout_id").loc[one.index]
    assert np.allclose(one["expected_a"], 1 - two["expected_a"])
    assert np.allclose(one["score_a"], 1 - two["score_a"])


@pytest.mark.parametrize("mode", ee.UPDATE_MODES)
def test_deterministic_and_independent_of_row_order(mode: str) -> None:
    df = random_bouts()
    shuffled = df.sample(frac=1.0, random_state=3)
    one = SchwingElo(params(update_mode=mode)).run(df)
    two = SchwingElo(params(update_mode=mode)).run(shuffled)
    pd.testing.assert_frame_equal(one.history, two.history)  # exact, not approximate
    assert one.ratings == two.ratings


def test_sequential_run_equals_bout_by_bout_reference() -> None:
    """``run`` in sequential mode = the step-by-step rules applied in chronological order."""
    df = random_bouts()
    p = params(update_mode="sequential", mov_alpha=0.4, mov_baseline_diff=1.25)
    ref = SchwingElo(p)
    year = None
    for row in df.sort_values(["date", "fest_id", "gang_nr", "bout_id"]).itertuples():
        y = ee.rating_year(row.date, p.season_start_month)
        if year is not None and y > year:
            ref.apply_season_reversion(y - year)
        year = y
        ref.rate_bout(row.athlete_a_id, row.athlete_b_id, row.outcome,
                      None if pd.isna(row.grade_a) else row.grade_a, row.grade_b, row.category)
    got = SchwingElo(p).run(df).ratings
    assert got.keys() == ref.ratings.keys()
    assert all(got[a] == pytest.approx(ref.ratings[a], abs=1e-8) for a in got)


def test_chronological_order_date_then_fest_then_gang() -> None:
    late = bout("a", "b", "WIN_A", fest=1, date="2015-07-01")
    early = bout("b", "c", "WIN_A", fest=2, date="2015-06-01")
    res = SchwingElo(params()).run(frame(late, early))
    assert res.history["fest_id"].tolist() == [2, 2, 1, 1]
    b_rows = res.history[res.history["athlete_id"] == "b"]
    assert b_rows["rating_before"].tolist() == pytest.approx([1500.0, 1508.0])


def test_festival_mode_uses_pre_festival_ratings() -> None:
    """Both bouts of ``a`` are scored against the ratings from before the festival."""
    df = frame(bout("a", "b", gang=1), bout("a", "c", gang=2))
    assert final(df, update_mode="festival")["a"] == pytest.approx(1516.0)
    seq = final(df, update_mode="sequential")["a"]
    assert seq == pytest.approx(1508 + 16 * (1 - 1 / (1 + 10 ** (-8 / 400))))
    assert seq < 1516.0  # the second win counts a little less once a is rated higher
    assert final(df, update_mode="gang")["a"] == pytest.approx(seq)
    assert final(df, update_mode="phase")["a"] == pytest.approx(1516.0)  # Gänge 1-4 = one block


def test_phase_mode_splits_after_gang_four() -> None:
    df = frame(bout("a", "b", gang=4), bout("a", "c", gang=5))
    assert final(df, update_mode="phase")["a"] == pytest.approx(
        final(df, update_mode="sequential")["a"])
    assert final(df, update_mode="phase", phase_split_gang=5)["a"] == pytest.approx(1516.0)


def test_festival_mode_ignores_gang_numbers() -> None:
    """Gang order is unreliable (~2 % of bouts): the default mode must not depend on it."""
    df = random_bouts()
    noisy = df.copy()
    noisy["gang_nr"] = np.random.default_rng(1).permutation(df["gang_nr"].to_numpy())
    one, two = final(df), final(noisy)
    assert all(one[a] == pytest.approx(two[a], abs=1e-9) for a in one)
    seq_one, seq_two = final(df, update_mode="sequential"), final(noisy, update_mode="sequential")
    assert any(abs(seq_one[a] - seq_two[a]) > 1e-6 for a in seq_one)


def test_history_schema_and_continuity() -> None:
    res = SchwingElo(params()).run(random_bouts())
    h = res.history
    assert list(h.columns) == list(ee.HISTORY_COLUMNS)
    assert not h.duplicated(["athlete_id", "fest_id"]).any()
    assert h.equals(h.sort_values(["date", "fest_id", "athlete_id"], kind="mergesort"))
    assert h["n_bouts"].sum() == 2 * len(res.bouts)
    assert h["score"].sum() == pytest.approx(len(res.bouts))  # every bout hands out 1 point
    assert h["expected"].sum() == pytest.approx(len(res.bouts))
    # the last row of every athlete carries his final rating, except for later reversions
    first = h.groupby("athlete_id").head(1)
    assert (first["rating_before"] == 1500.0).all() and (first["bouts_before"] == 0).all()
    assert first["days_inactive"].isna().all()


def test_empty_input() -> None:
    res = SchwingElo(params()).run(frame(bout("a", "b", eligible=False)))
    assert res.history.empty and res.ratings == {} and res.as_of is None
    assert list(res.history.columns) == list(ee.HISTORY_COLUMNS)


def test_params_validation_and_config() -> None:
    with pytest.raises(ValueError, match="update mode"):
        params(update_mode="random")
    with pytest.raises(ValueError, match="K-factors"):
        params(k_factors={"Regional": 0})
    p = EloParams.from_config(load_config(env={}))
    assert p.k_factors == K and p.initial == 1500 and p.scale == 400
    # decision of 2026-10-03: K x 2 (tier ratios unchanged), delta 0.05
    assert p.k_scale == 2.0 and p.reversion_delta == 0.05
    assert p.k("ESAF") == 96 and p.k("Regional") == 32
    assert p.update_mode == "festival" and p.mov_alpha == 1.0 and p.mov_baseline_diff == 1.36
    assert p.replace(update_mode="gang").update_mode == "gang"
    assert math.isclose(p.inactive_days, 1.5 * 365.25)


# =========================================================================== task 2: MoV
def test_mov_multiplier_formula() -> None:
    lam = ee.mov_multiplier
    assert lam(10.0, 8.75, alpha=0.4, baseline_diff=1.25) == pytest.approx(1.0)
    assert lam(10.0, 8.5, alpha=0.4, baseline_diff=1.25) == pytest.approx(1.1)   # Plattwurf
    assert lam(9.75, 8.75, alpha=0.4, baseline_diff=1.25) == pytest.approx(0.9)  # minimal win
    assert lam(10.0, 8.5, alpha=0.0, baseline_diff=1.25) == 1.0                  # neutral
    out = lam(np.array([10.0, 9.75]), np.array([8.5, 8.75]), 0.4, 1.25)
    assert out == pytest.approx([1.1, 0.9])


def test_mov_multiplier_is_clamped() -> None:
    lam = ee.mov_multiplier
    assert lam(10.0, 8.5, alpha=10.0, baseline_diff=1.25) == 2.0
    assert lam(9.75, 8.75, alpha=10.0, baseline_diff=1.25) == 0.5
    assert lam(8.5, 10.0, alpha=1.0, baseline_diff=1.25) == 0.5  # misprinted grades stay sane
    assert lam(10.0, 8.5, 10.0, 1.25, lambda_min=0.8, lambda_max=1.2) == 1.2
    with pytest.raises(ValueError, match="mov_lambda"):
        params(mov_lambda_min=0.0)
    with pytest.raises(ValueError, match="mov_lambda"):
        params(mov_lambda_max=0.9)


@pytest.mark.parametrize("ga, gb", [(None, 8.5), (10.0, None), (None, None),
                                    (float("nan"), 8.5)])
def test_null_grade_counts_with_outcome_only(ga: float | None, gb: float | None) -> None:
    """extra_bout / grade_missing / one_sided bouts: lambda = 1 (spec §4.2.2)."""
    r = final(frame(bout("a", "b", "WIN_A", ga=ga, gb=gb)), mov_alpha=0.8,
              mov_baseline_diff=1.0)
    assert r["a"] == pytest.approx(1508.0)
    assert ee.mov_multiplier(ga if ga is not None else np.nan,
                             gb if gb is not None else np.nan, 0.8, 1.0) == 1.0


def test_mov_scales_wins() -> None:
    kw = dict(mov_alpha=0.4, mov_baseline_diff=1.25)
    assert final(frame(bout("a", "b", ga=10.0, gb=8.5)), **kw)["a"] == pytest.approx(1508.8)
    assert final(frame(bout("a", "b", ga=9.75, gb=8.75)), **kw)["a"] == pytest.approx(1507.2)
    assert final(frame(bout("a", "b", ga=10.0, gb=8.75)), **kw)["a"] == pytest.approx(1508.0)


def test_mov_uses_the_winners_margin_whichever_side_won() -> None:
    kw = dict(mov_alpha=0.4, mov_baseline_diff=1.25)
    win_b = final(frame(bout("a", "b", "WIN_B", ga=8.5, gb=10.0)), **kw)
    assert win_b["b"] == pytest.approx(1508.8) and win_b["a"] == pytest.approx(1491.2)


@pytest.mark.parametrize("ga, gb", [(8.75, 8.75), (9.0, 8.75), (8.75, 9.0), (9.0, 9.0)])
def test_mov_does_not_apply_to_draws(ga: float, gb: float) -> None:
    elo = SchwingElo(params(mov_alpha=0.8, mov_baseline_diff=0.0))
    elo.ratings = {"a": 1700.0, "b": 1500.0}
    delta = elo.rate_bout("a", "b", "DRAW", ga, gb)
    assert delta == pytest.approx(16 * (0.5 - ee.expected_score(1700, 1500)))
    assert ee.bout_multiplier(0.5, ga, gb, elo.params) == 1.0


def test_mov_keeps_the_bout_zero_sum_and_reference_agrees() -> None:
    elo = SchwingElo(params(mov_alpha=0.6, mov_baseline_diff=1.1))
    elo.ratings = {"a": 1480.0, "b": 1655.0}
    elo.rate_bout("a", "b", "WIN_A", 10.0, 8.5, "ESAF")
    assert sum(elo.ratings.values()) == pytest.approx(1480 + 1655)
    lam = 1 + 0.6 * (1.5 - 1.1)
    assert elo.ratings["a"] == pytest.approx(
        1480 + 48 * lam * (1 - 1 / (1 + 10 ** (175 / 400))))


def test_run_reports_lambda_per_bout() -> None:
    df = frame(bout("a", "b", ga=10.0, gb=8.5), bout("c", "d", "DRAW", ga=9.0, gb=8.75),
               bout("e", "f", "WIN_B", ga=8.75, gb=9.75), bout("g", "h", ga=None, gb=8.5))
    res = SchwingElo(params(mov_alpha=0.4, mov_baseline_diff=1.25)).run(df)
    assert res.bouts["mov_lambda"].tolist() == pytest.approx([1.1, 1.0, 0.9, 1.0])


# =========================================================================== task 3: seasons
def test_revert_to_mean_formula() -> None:
    assert ee.revert_to_mean(1700.0, 0.10, 1500.0) == pytest.approx(1680.0)
    assert ee.revert_to_mean(1300.0, 0.10, 1500.0) == pytest.approx(1320.0)
    assert ee.revert_to_mean(1500.0, 0.10, 1500.0) == pytest.approx(1500.0)
    assert ee.revert_to_mean(1700.0, 0.10, 1500.0, times=2) == pytest.approx(1662.0)
    assert ee.revert_to_mean(1700.0, 0.10, 1500.0, times=0) == pytest.approx(1700.0)


@pytest.mark.parametrize("date, year", [
    ("2015-03-31", 2014), ("2015-04-01", 2015), ("2015-12-31", 2015), ("2016-01-02", 2015),
    ("2016-02-20", 2015), ("2016-04-10", 2016)])
def test_rating_year_starts_in_april(date: str, year: int) -> None:
    assert ee.rating_year(date) == year
    assert ee.rating_year(np.array([date], dtype="datetime64[D]")).tolist() == [year]


def test_rating_year_start_month_is_configurable() -> None:
    assert ee.rating_year("2016-02-20", start_month=1) == 2016


def test_reversion_before_first_festival_of_the_season() -> None:
    df = frame(bout("a", "b", date="2015-06-01", fest=1),
               bout("a", "c", date="2015-09-01", fest=2),      # same season: no reversion
               bout("a", "d", date="2016-02-10", fest=3),      # hall festival before April
               bout("a", "e", date="2016-04-20", fest=4))      # first festival of 2016
    h = SchwingElo(params()).run(df).history
    a = h[h["athlete_id"] == "a"].reset_index(drop=True)
    assert a.loc[1, "rating_before"] == pytest.approx(a.loc[0, "rating_after"])
    assert a.loc[2, "rating_before"] == pytest.approx(a.loc[1, "rating_after"])
    assert a.loc[3, "rating_before"] == pytest.approx(
        a.loc[2, "rating_after"] * 0.9 + 1500 * 0.1)
    assert a["season"].tolist() == [2015, 2015, 2016, 2016]  # `season` = calendar year


def test_reversion_applies_to_inactive_athletes_too() -> None:
    df = frame(bout("a", "b", date="2015-06-01", fest=1),
               bout("c", "d", date="2016-06-01", fest=2))
    res = SchwingElo(params()).run(df)
    assert res.ratings["a"] == pytest.approx(1500 + 8 * 0.9)
    assert res.ratings["b"] == pytest.approx(1500 - 8 * 0.9)
    assert res.ratings["c"] == pytest.approx(1508.0)  # newcomers are not reverted


def test_reversion_once_per_season_boundary_crossed() -> None:
    """2020 had no season: two boundaries lie between autumn 2019 and summer 2021."""
    df = frame(bout("a", "b", date="2019-08-01", fest=1),
               bout("a", "c", "DRAW", date="2021-06-01", fest=2))
    h = SchwingElo(params()).run(df).history
    a = h[h["athlete_id"] == "a"]
    assert a["rating_before"].iloc[1] == pytest.approx(1500 + 8 * 0.9 ** 2)


def test_reversion_parameters() -> None:
    df = frame(bout("a", "b", date="2015-06-01", fest=1),
               bout("c", "d", date="2016-06-01", fest=2))
    assert final(df, reversion_delta=0.0)["a"] == pytest.approx(1508.0)
    assert final(df, reversion_delta=1.0)["a"] == pytest.approx(1500.0)
    assert final(df, reversion_delta=0.5, reversion_mean=1400.0)["a"] == pytest.approx(1454.0)
    late = frame(bout("a", "b", date="2015-08-01", fest=1),
                 bout("c", "d", date="2016-06-01", fest=2))
    assert final(late)["a"] == pytest.approx(1507.2)
    assert final(late, season_start_month=7)["a"] == pytest.approx(1508.0)  # same rating year
    with pytest.raises(ValueError, match="reversion delta"):
        params(reversion_delta=1.5)
    with pytest.raises(ValueError, match="start month"):
        params(season_start_month=13)


def test_reversion_keeps_the_mean_and_shrinks_the_spread() -> None:
    elo = SchwingElo(params())
    elo.ratings = {"a": 1800.0, "b": 1200.0, "c": 1500.0}
    elo.apply_season_reversion()
    assert elo.ratings == pytest.approx({"a": 1770.0, "b": 1230.0, "c": 1500.0})
    elo.apply_season_reversion(times=2)
    assert elo.ratings["a"] == pytest.approx(1500 + 270 * 0.81)


def test_provisional_after_more_than_one_and_a_half_seasons_inactive() -> None:
    limit = int(1.5 * 365.25)  # 547 days
    first = dt.date(2015, 6, 1)
    back_late = (first + dt.timedelta(days=limit + 1)).isoformat()
    back_in_time = (first + dt.timedelta(days=limit)).isoformat()
    df = frame(bout("a", "x", date="2015-06-01", fest=1),
               bout("b", "y", date="2015-06-01", fest=1),
               bout("b", "z", date=back_in_time, fest=2),
               bout("a", "z", date=back_late, fest=3),
               bout("a", "y", date="2017-06-01", fest=4))
    h = SchwingElo(params()).run(df).history.set_index(["athlete_id", "fest_id"])
    assert h.loc[("a", 3), "days_inactive"] == limit + 1
    assert h.loc[("a", 3), "provisional"] and h.loc[("a", 3), "provisional_reason"] == "inactive"
    assert not h.loc[("b", 2), "provisional"]                  # exactly 1.5 seasons: not yet
    assert not h.loc[("a", 4), "provisional"]                  # active again
    assert not h.loc[("a", 1), "provisional"] and pd.isna(h.loc[("a", 1), "days_inactive"])


def test_provisional_until_minimum_number_of_bouts() -> None:
    df = frame(*[bout("a", f"x{i}", date=f"2015-06-{i + 1:02d}", fest=i, gang=1)
                 for i in range(5)])
    h = SchwingElo(params(provisional_min_bouts=3)).run(df).history
    a = h[h["athlete_id"] == "a"]
    assert a["provisional"].tolist() == [True, True, False, False, False]
    assert a["provisional_reason"].tolist() == ["few_bouts", "few_bouts", "", "", ""]
    assert a["bouts_before"].tolist() == [0, 1, 2, 3, 4]
    none = SchwingElo(params(provisional_min_bouts=0)).run(df).history
    assert not none["provisional"].any()


def test_provisional_reasons_combine() -> None:
    df = frame(bout("a", "x", date="2015-06-01", fest=1),
               bout("a", "y", date="2018-06-01", fest=2))
    h = SchwingElo(params(provisional_min_bouts=5)).run(df).history
    assert h[h["athlete_id"] == "a"]["provisional_reason"].tolist() == [
        "few_bouts", "few_bouts,inactive"]


# =========================================================================== task 4: runner / CLI
def athletes_frame(*rows: tuple[str, str]) -> pd.DataFrame:
    return pd.DataFrame({"athlete_id": [r[0] for r in rows],
                         "full_name": [r[0].title() for r in rows],
                         "evidence": [r[1] for r in rows]})


def cfg_with(**kw: object) -> Config:
    return load_config(overrides=kw, env={})


def test_identity_uncertainty_marker() -> None:
    athletes = athletes_frame(("a", "unique_name"), ("b", "namesakes=2;ambiguous_rows=12"),
                              ("c", "namesakes=2;ambiguous_rows=1"), ("d", "not_a_name"),
                              ("e", "unique_name"))
    im = pd.DataFrame({
        "athlete_raw_id": [f"r{i}" for i in range(40)],
        "athlete_id": ["a"] * 10 + ["b"] * 20 + ["c"] * 3 + ["d"] + ["e"] * 6,
        "confidence": [0.9] * 10 + [0.4] * 12 + [0.9] * 8 + [0.3, 0.9, 0.9] + [0.0]
        + [0.4] + [0.85] * 5})
    out = er.identity_uncertainty(athletes, im).set_index("athlete_id")
    assert out["identity_rows"].to_dict() == {"a": 10, "b": 20, "c": 3, "d": 1, "e": 6}
    assert out["identity_low_conf_rows"].to_dict() == {"a": 0, "b": 12, "c": 1, "d": 1, "e": 1}
    assert out.loc["b", "identity_low_conf_share"] == pytest.approx(0.6)
    assert out["identity_uncertain"].to_dict() == {
        "a": False, "b": True, "c": True, "d": True, "e": False}  # c: 1 of 3 rows >= 25 %
    assert out.loc["b", "identity_flags"] == "namesakes=2;ambiguous_rows=12"
    none = er.identity_uncertainty(athletes, None)
    assert not none["identity_uncertain"].any() and (none["identity_rows"] == 0).all()


def test_drop_low_confidence_bouts() -> None:
    bouts = frame(bout("a", "b"), bout("a", "c"), bout("b", "c"))
    bouts["athlete_a_raw_id"] = ["1-a", "1-a", "1-b"]
    bouts["athlete_b_raw_id"] = ["1-b", "1-c", "1-c"]
    im = pd.DataFrame({"athlete_raw_id": ["1-a", "1-b", "1-c"], "confidence": [0.9, 0.4, 0.85]})
    kept = er.drop_low_confidence_bouts(bouts, im)
    assert kept[["athlete_a_id", "athlete_b_id"]].values.tolist() == [["a", "c"]]


def test_ranking_excludes_garbage_unrated_provisional() -> None:
    bouts = frame(
        *[bout("champ", "regular", date=f"2025-0{m}-10", fest=m, gang=g)
          for m in (5, 6) for g in (1, 2, 3)],
        bout("regular", "rookie", date="2025-06-10", fest=6, gang=4),
        bout("x", "regular", "WIN_B", date="2025-06-10", fest=6, gang=5),
        *[bout("retired", "old", date="2015-06-10", fest=20 + g, gang=g) for g in range(1, 7)])
    athletes = athletes_frame(("champ", "unique_name"), ("regular", "unique_name"),
                              ("rookie", "unique_name"), ("x", "not_a_name"),
                              ("retired", "unique_name"), ("old", "unique_name"),
                              ("ghost", "unique_name"))
    cfg = cfg_with(provisional_min_bouts=5, mov_alpha=0.0)
    result, table, seasons = er.compute(bouts, athletes, None, cfg)
    t = table.set_index("athlete_id")
    assert t["ranked"].to_dict() == {"champ": True, "regular": True, "rookie": False,
                                     "x": False, "retired": False, "old": False, "ghost": False}
    assert t.loc["champ", "rank"] == 1 and t.loc["regular", "rank"] == 2
    assert t["rank"].isna().sum() == 5
    assert t.loc["rookie", "provisional_reason"] == "few_bouts"
    assert t.loc["retired", "provisional_reason"] == "inactive"
    assert t.loc["x", "provisional_reason"] == "few_bouts"   # rated, never ranked
    assert pd.isna(t.loc["ghost", "rating"]) and t.loc["ghost", "n_bouts"] == 0
    assert not t.loc["ghost", "provisional"]
    assert t.loc["champ", "rating"] == pytest.approx(result.ratings["champ"])
    assert t.loc["champ", "rating_peak"] == pytest.approx(t.loc["champ", "rating"])
    assert pd.isna(t.loc["rookie", "rating_peak"])            # never past the threshold
    assert t.loc["retired", "days_inactive"] > 3000
    # season table: 2015 ranks the two veterans, 2025 the two regulars
    s = seasons.set_index(["season", "athlete_id"])
    assert s.loc[(2015, "retired"), "rank"] == 1 and s.loc[(2015, "old"), "rank"] == 2
    assert s.loc[(2025, "champ"), "rank"] == 1 and pd.isna(s.loc[(2025, "rookie"), "rank"])
    assert pd.isna(s.loc[(2025, "x"), "rank"])
    assert s.loc[(2025, "champ"), "n_bouts"] == 6 and s.loc[(2025, "champ"), "n_festivals"] == 2


def test_burn_in_seasons_are_rated_but_not_ranked() -> None:
    bouts = frame(*[bout("a", "b", date=f"{y}-06-10", fest=y, gang=1) for y in (2011, 2012)])
    cfg = cfg_with(provisional_min_bouts=0, elo_first_ranked_season=2012, mov_alpha=0.0)
    result, table, seasons = er.compute(bouts, athletes_frame(("a", "x"), ("b", "x")), None, cfg)
    s = seasons.set_index(["season", "athlete_id"])
    assert s.loc[(2011, "a"), "burn_in"] and pd.isna(s.loc[(2011, "a"), "rank"])
    assert s.loc[(2011, "a"), "rating_end"] == pytest.approx(
        1500 + cfg.elo_k_scale * 16 * 0.5)                             # still rated
    assert s.loc[(2012, "a"), "rank"] == 1 and not s.loc[(2012, "a"), "burn_in"]
    assert table.set_index("athlete_id").loc["a", "peak_fest_id"] == 2012  # peak outside burn-in


def test_bouts_with_unknown_athletes_are_an_error() -> None:
    with pytest.raises(ValueError, match="missing from athletes.parquet"):
        er.compute(frame(bout("a", "b")), athletes_frame(("a", "x")), None, cfg_with())


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``python -m src.cli elo --sample`` in a fresh data dir (builds db + Parquet)."""
    data = tmp_path_factory.mktemp("elo-sample") / "data"
    mp = pytest.MonkeyPatch()
    for var in [v for v in os.environ if v.startswith("SCHWINGEN_")]:
        mp.delenv(var)
    try:
        assert cli.main(["elo", "--sample", "--data-dir", str(data)]) == 0
    finally:
        mp.undo()
    return data / "processed"


def test_cli_elo_sample_writes_rating_history(sample_run: Path) -> None:
    ratings = pd.read_parquet(sample_run / "ratings.parquet")
    # exit criterion: athlete, date, fest_id, rating_before, rating_after
    assert list(ratings.columns)[:5] == ["athlete_id", "date", "fest_id", "rating_before",
                                         "rating_after"]
    assert pq.read_schema(sample_run / "ratings.parquet").equals(er.RATINGS_SCHEMA)
    bouts = pd.read_parquet(sample_run / "bouts.parquet")
    bouts = bouts[bouts["elo_eligible"]]
    assert ratings["n_bouts"].sum() == 2 * len(bouts) > 0
    assert set(ratings["fest_id"]) == set(bouts["fest_id"])
    assert set(ratings["athlete_id"]) == set(bouts["athlete_a_id"]) | set(bouts["athlete_b_id"])
    assert not ratings.duplicated(["athlete_id", "fest_id"]).any()
    assert ratings[["rating_before", "rating_after"]].notna().all().all()
    change = (ratings["rating_after"] - ratings["rating_before"]).groupby(ratings["fest_id"]).sum()
    assert np.allclose(change, 0.0, atol=1e-8)
    first = ratings.groupby("athlete_id").head(1)
    assert (first["rating_before"] == 1500.0).all()


def test_cli_elo_sample_athlete_and_season_tables(sample_run: Path) -> None:
    table = pd.read_parquet(sample_run / "athlete_ratings.parquet")
    athletes = pd.read_parquet(sample_run / "athletes.parquet")
    seasons = pd.read_parquet(sample_run / "season_ratings.parquet")
    assert pq.read_schema(sample_run / "athlete_ratings.parquet").equals(
        er.ATHLETE_RATINGS_SCHEMA)
    assert pq.read_schema(sample_run / "season_ratings.parquet").equals(er.SEASON_RATINGS_SCHEMA)
    assert sorted(table["athlete_id"]) == sorted(athletes["athlete_id"])
    ranked = table[table["ranked"]]
    assert len(ranked) > 0 and sorted(ranked["rank"]) == list(range(1, len(ranked) + 1))
    assert (ranked["n_bouts"] > 0).all() and not ranked["provisional"].any()
    assert ranked.sort_values("rank")["rating"].is_monotonic_decreasing
    assert table.loc[table["n_bouts"] == 0, "rating"].isna().all()
    assert set(seasons["season"]) == {2011, 2019, 2024, 2025}
    assert seasons.loc[seasons["season"] == 2011, "burn_in"].all()
    assert not seasons.loc[seasons["season"] == 2011, "ranked"].any()
    assert seasons.loc[seasons["season"] == 2019, "ranked"].any()
    for _, part in seasons[seasons["ranked"]].groupby("season"):
        assert sorted(part["rank"]) == list(range(1, len(part) + 1))
    # a known result of the sample: the ESAF 2019 Schlussgang winner leads that festival
    esaf = pd.read_parquet(sample_run / "ratings.parquet").query("fest_id == 24110")
    best = esaf.assign(gain=esaf["rating_after"] - esaf["rating_before"]).nlargest(15, "gain")
    names = set(athletes.set_index("athlete_id").loc[best["athlete_id"], "full_name"])
    assert "Stucki Christian" in names


def test_cli_elo_is_byte_identical_on_rerun(sample_run: Path) -> None:
    names = ["ratings.parquet", "athlete_ratings.parquet", "season_ratings.parquet"]
    before = {n: (sample_run / n).read_bytes() for n in names}
    assert cli.main(["elo", "--sample", "--data-dir", str(sample_run.parent)]) == 0
    assert {n: (sample_run / n).read_bytes() for n in names} == before


def test_cli_elo_without_clean_outputs_fails(tmp_path: Path,
                                             caplog: pytest.LogCaptureFixture) -> None:
    assert cli.main(["elo", "--data-dir", str(tmp_path / "nothing")]) == 1
    assert "run `clean` first" in caplog.text


def test_sample_mode_lowers_the_provisional_threshold() -> None:
    assert load_config(env={}).provisional_min_bouts == 24
    assert load_config({"sample": True}, env={}).provisional_min_bouts == 6
    assert load_config({"sample": True, "provisional_min_bouts": 12},
                       env={}).provisional_min_bouts == 12
    env = {"SCHWINGEN_SAMPLE": "1", "SCHWINGEN_PROVISIONAL_MIN_BOUTS": "30"}
    assert load_config(env=env).provisional_min_bouts == 30


def test_k_scale_and_delta_configuration() -> None:
    cfg = load_config(env={})
    assert cfg.elo_k_scale == 2.0 and cfg.season_reversion_delta == 0.05
    assert cfg.k_factors == K                       # base values untouched by the scale
    env = {"SCHWINGEN_ELO_K_SCALE": "1.0", "SCHWINGEN_SEASON_REVERSION_DELTA": "0.10"}
    spec = load_config(env=env)
    assert spec.elo_k_scale == 1.0 and spec.season_reversion_delta == 0.10
    assert load_config({"elo_k_scale": 1.5}, env=env).elo_k_scale == 1.5  # override wins
    for bad in ("0", "-2", "nan", "inf"):
        with pytest.raises(ValueError, match="k scale"):
            load_config(env={"SCHWINGEN_ELO_K_SCALE": bad})
    with pytest.raises(ValueError, match="SCHWINGEN_ELO_K_SCALE"):
        load_config(env={"SCHWINGEN_ELO_K_SCALE": "double"})


def test_k_scale_one_reproduces_the_spec_numbers() -> None:
    """Reverting = `elo_k_scale = 1.0` and delta 0.10: the values of the spec's rules."""
    bouts = frame(bout("a", "b", date="2015-06-01", fest=1, cat="Regional"),
                  bout("c", "d", date="2015-06-01", fest=1, cat="Regional"),
                  bout("a", "c", date="2016-06-01", fest=2, cat="ESAF", ga=10.0, gb=8.5))
    athletes = athletes_frame(*[(x, "unique_name") for x in "abcd"])
    spec = cfg_with(elo_k_scale=1.0, season_reversion_delta=0.10, provisional_min_bouts=0)
    r = er.compute(bouts, athletes, None, spec)[0].ratings
    low, high = 1 + 1.0 * (1.25 - 1.36), 1 + 1.0 * (1.5 - 1.36)  # MoV 0.89 / 1.14
    assert r["b"] == pytest.approx(1500 - 16 * low * 0.5 * 0.9)  # K 16, 10 % reversion
    assert r["a"] == pytest.approx(1500 + 16 * low * 0.5 * 0.9 + 48 * high * 0.5)  # ESAF K 48
    now = er.compute(bouts, athletes, None, cfg_with(provisional_min_bouts=0))[0].ratings
    assert now["b"] == pytest.approx(1500 - 32 * low * 0.5 * 0.95)   # K 32, 5 % reversion
    assert now["a"] == pytest.approx(1500 + 32 * low * 0.5 * 0.95 + 96 * high * 0.5)


def test_invalid_elo_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="update mode"):
        load_config(env={"SCHWINGEN_ELO_UPDATE_MODE": "bogus"})
    with pytest.raises(ValueError, match="provisional_min_bouts"):
        load_config(env={"SCHWINGEN_PROVISIONAL_MIN_BOUTS": "-1"})


# =========================================================================== task 5: evaluation

def test_prediction_metrics() -> None:
    d = pd.DataFrame({"score_a": [1.0, 0.0, 0.5, 1.0], "expected_a": [0.5, 0.5, 0.5, 0.5],
                      "season": [2014, 2014, 2022, 2022]})
    m = ev.prediction_metrics(d)
    assert m["n"] == 4 and m["brier"] == pytest.approx(0.1875)
    assert m["log_loss"] == pytest.approx(math.log(2))
    sharp = ev.prediction_metrics(d.assign(expected_a=[0.9, 0.1, 0.5, 0.9]))
    assert sharp["brier"] < m["brier"] and sharp["log_loss"] < m["log_loss"]
    assert math.isnan(ev.prediction_metrics(d.iloc[:0])["brier"])
    assert len(ev.in_seasons(d, ev.TRAIN)) == 2 and len(ev.in_seasons(d, ev.TEST)) == 2


def test_predictions_are_made_before_the_bout() -> None:
    """The expected score of a bout never depends on that bout's own result."""
    df = random_bouts()
    flipped = df.copy()
    last = flipped["date"] == flipped["date"].max()
    flipped.loc[last, "outcome"] = flipped.loc[last, "outcome"].map(
        {"WIN_A": "WIN_B", "WIN_B": "WIN_A", "DRAW": "WIN_A"})
    for mode in ("festival", "phase"):
        one = SchwingElo(params(update_mode=mode)).run(df).bouts.set_index("bout_id")
        two = SchwingElo(params(update_mode=mode)).run(flipped).bouts.set_index("bout_id")
        assert np.allclose(one["expected_a_prefest"], two["expected_a_prefest"])
    one = SchwingElo(params()).run(df).bouts.set_index("bout_id")
    two = SchwingElo(params()).run(flipped).bouts.set_index("bout_id")
    assert np.allclose(one["expected_a"], two["expected_a"])  # festival mode: also `used`


def test_mean_win_margin_and_mov_grid() -> None:
    df = frame(bout("a", "b", ga=10.0, gb=8.5), bout("c", "d", "WIN_B", ga=8.75, gb=9.75),
               bout("e", "f", "DRAW", ga=9.0, gb=8.75), bout("g", "h", ga=None, gb=8.5))
    assert ev.mean_win_margin(df) == pytest.approx(1.25)
    grid = ev.calibrate_mov(random_bouts(), params(), alphas=(0.0, 1.0), baselines=(1.25,))
    assert list(grid["alpha"]) == [0.0, 1.0]
    assert grid.loc[0, "mean_lambda"] == 1.0 and grid.loc[1, "mean_lambda"] != 1.0
    assert {"train_brier", "test_brier", "train_log_loss", "test_log_loss"} <= set(grid.columns)


def test_gang_noise_only_moves_flagged_bouts_and_never_the_festival_mode() -> None:
    df = random_bouts()
    df["flags"] = ""
    df.loc[df.index[::7], "flags"] = "gang_uncertain:2/3,gang_collision"
    noisy = ev.perturb_gang_order(df, seed=1)
    changed = noisy["gang_nr"] != df["gang_nr"]
    assert changed.any() and not changed[df["flags"] == ""].any()
    assert noisy["gang_nr"].between(1, 4).all()
    sens = ev.gang_noise_sensitivity(df, params(), seed=1, top_n=5)
    assert sens.loc["festival", "max_abs_diff"] == 0.0
    assert sens.loc["sequential", "max_abs_diff"] > 0.0
    modes = ev.compare_update_modes(df, params())
    assert list(modes.index) == list(ee.UPDATE_MODES)


def test_calibration_experience_and_drift_tables() -> None:
    res = SchwingElo(params(provisional_min_bouts=8)).run(random_bouts())
    cal = ev.calibration_table(res.bouts)
    assert cal["n"].sum() == len(res.bouts)
    assert ((cal["win"] + cal["draw"] + cal["loss"]).round(9) == 1).all()
    assert (cal["predicted"] >= 0.5).all()
    exp = ev.experience_table(res.bouts)
    assert exp["n"].sum() == len(res.bouts)
    drift = ev.season_drift(res.history, 8)
    assert drift.loc[2012, "newcomers"] == drift.loc[2012, "athletes"]
    assert drift.loc[2012, "mean"] == pytest.approx(1500.0)
    assert set(ev.season_metrics(res.bouts).columns) == {"n", "brier", "log_loss"}
    scan = ev.parameter_scan(random_bouts(), params(), (1, 2), (0.0,))
    assert list(scan.index) == ["K x 1", "K x 2", "delta = 0"]
    # the K scan uses absolute multipliers on the base K-factors, whatever is configured
    again = ev.parameter_scan(random_bouts(), params(k_scale=2.0), (1, 2), ())
    assert again.loc["K x 2", "train_brier"] == pytest.approx(scan.loc["K x 2", "train_brier"])
    assert again.loc["K x 1", "train_brier"] == pytest.approx(scan.loc["K x 1", "train_brier"])
    assert scan.loc["K x 1", "train_brier"] != scan.loc["K x 2", "train_brier"]


def test_scale_grid_and_mode_comparison_by_scale() -> None:
    df = random_bouts()
    athletes = athletes_frame(*[(a, "unique_name") for a in sorted(
        set(df["athlete_a_id"]) | set(df["athlete_b_id"]))])
    cfg = cfg_with(provisional_min_bouts=8, elo_first_ranked_season=2012)
    grid = ev.scale_grid(df, athletes, None, cfg, variants=((1.0, 0.10), (2.0, 0.05)),
                         top_n=5)
    assert list(grid.index) == [(1.0, 0.10), (2.0, 0.05)]
    assert grid.loc[(1.0, 0.10), "top20_overlap"] == min(20, grid.loc[(1.0, 0.10), "ranked"])
    # a doubled K moves the ratings more and spreads the scale
    assert grid["max_fest_change"].is_monotonic_increasing
    assert grid["sd_ranked"].is_monotonic_increasing
    assert grid["top5_mean_abs_change"].is_monotonic_increasing
    # the current configuration is one of the rows
    _, table, _ = er.compute(df, athletes, None, cfg)
    assert grid.loc[(2.0, 0.05), "rank1"] == pytest.approx(
        table.loc[table["ranked"], "rating"].max())
    modes = ev.compare_update_modes_by_scale(
        df, params(mov_alpha=1.0, mov_baseline_diff=1.36), (1.0, 2.0))
    assert list(modes.index) == [(c, m) for c in (1.0, 2.0) for m in ee.UPDATE_MODES]
    fest = modes.xs("festival", level="mode")
    assert np.allclose(fest["train_brier"], fest["train_brier_prefest"])  # same information
    assert fest["train_brier"].nunique() == 2


def test_identity_sensitivity_pass() -> None:
    bouts = frame(*[bout(a, b, date=f"2025-06-{d:02d}", fest=d, gang=1)
                    for d, (a, b) in enumerate([("a", "b"), ("a", "c"), ("b", "c"), ("a", "b")],
                                               start=1)])
    bouts["athlete_a_raw_id"] = [f"{f}-{a}" for f, a in zip(bouts["fest_id"], bouts["athlete_a_id"])]
    bouts["athlete_b_raw_id"] = [f"{f}-{b}" for f, b in zip(bouts["fest_id"], bouts["athlete_b_id"])]
    im = pd.DataFrame({"athlete_raw_id": ["1-a", "1-b", "2-a", "2-c", "3-b", "3-c", "4-a", "4-b"],
                       "athlete_id": ["a", "b", "a", "c", "b", "c", "a", "b"],
                       "confidence": [0.9, 0.9, 0.9, 0.4, 0.9, 0.9, 0.9, 0.9]})
    cfg = cfg_with(provisional_min_bouts=0, mov_alpha=0.0)
    top, summary = ev.identity_sensitivity(
        bouts, athletes_frame(("a", "x"), ("b", "x"), ("c", "x")), im, cfg, top_n=3)
    assert summary["bouts_dropped"] == 1 and summary["ranked"] == 3
    t = top.set_index("athlete_id")
    assert t.loc["a", "n_bouts"] == 3 and t.loc["a", "n_bouts_strict"] == 2
    assert t.loc["a", "rating_strict"] < t.loc["a", "rating"]  # one win less
    assert summary["max_abs_rating_diff"] > 0


def test_cli_elo_evaluate_prints_report(sample_run: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["elo", "--sample", "--evaluate", "--data-dir", str(sample_run.parent)]) == 0
    out = capsys.readouterr().out
    assert "K x 2" in out and "delta=0.05" in out
    for section in ("update modes", "update modes by K scale", "MoV grid", "K scale x delta",
                    "calibration by rating gap", "identity sensitivity",
                    "rating distribution"):
        assert section in out


# =========================================================================== task 6: real data
# Sanity checks on the real history. They need data/processed/*.parquet (generated,
# not committed), so they skip in CI / on a fresh clone; the --sample tests above
# always run.
def real_config() -> Config:
    """Defaults, except that SCHWINGEN_DATA_DIR may point to the real data."""
    return load_config(env={k: v for k, v in os.environ.items() if k == "SCHWINGEN_DATA_DIR"})


class Real:
    def __init__(self) -> None:
        self.cfg = real_config()
        self.bouts, self.athletes, self.identity_map = er.load_inputs(self.cfg.processed_dir)
        self.result, self.table, self.seasons = er.compute(
            self.bouts, self.athletes, self.identity_map, self.cfg)

    def athlete(self, full_name: str) -> str:
        """The id of the namesake with the most rated bouts (ids differ between runs)."""
        rows = self.table[self.table["full_name"] == full_name]
        assert len(rows), f"{full_name} not in athletes.parquet"
        return str(rows.sort_values("n_bouts")["athlete_id"].iloc[-1])

    def season_rank(self, full_name: str, season: int) -> int:
        s = self.seasons
        row = s[(s["athlete_id"] == self.athlete(full_name)) & (s["season"] == season)]
        assert len(row) == 1 and row["ranked"].iloc[0], f"{full_name} not ranked in {season}"
        return int(row["rank"].iloc[0])


@pytest.fixture(scope="module")
def real() -> Real:
    processed = real_config().processed_dir
    if not all((processed / n).is_file() for n in ("bouts.parquet", "athletes.parquet")):
        pytest.skip(f"real data not available ({processed}): run `crawl`, `parse`, `clean`")
    data = Real()
    if len(data.result.bouts) < 100_000 or data.result.bouts["season"].nunique() < 10:
        pytest.skip("data/processed holds only a partial history")
    return data


@pytest.mark.parametrize("name, season, at_most", [
    ("Glarner Matthias", 2016, 5),     # Schwingerkönig 2016
    ("Glarner Matthias", 2013, 10),
    ("Wicki Joel", 2018, 3),
    ("Wicki Joel", 2019, 3),           # Schlussgang ESAF 2019
    ("Wicki Joel", 2022, 3),           # Schwingerkönig 2022
    ("Reichmuth Pirmin", 2019, 8),
    ("Reichmuth Pirmin", 2023, 8),
    ("Forrer Arnold", 2012, 12),       # König 2001, still elite in his late career
    ("Forrer Arnold", 2013, 10),
    ("Sempach Matthias", 2013, 3),     # Schwingerkönig 2013
    ("Sempach Matthias", 2014, 3),     # Kilchberg 2014
    ("Stucki Christian", 2017, 3),     # Unspunnen 2017
    ("Stucki Christian", 2019, 3),     # Schwingerkönig 2019
    ("Giger Samuel", 2021, 3),
    ("Giger Samuel", 2023, 3),         # Unspunnen 2023
    ("Staudenmann Fabian", 2024, 3),
    ("Orlik Armon", 2025, 5),          # Schwingerkönig 2025
])
def test_real_elite_athletes_rank_near_the_top_in_their_peak_seasons(
        real: Real, name: str, season: int, at_most: int) -> None:
    assert real.season_rank(name, season) <= at_most


def test_real_elite_rivals_are_far_above_the_field(real: Real) -> None:
    t = real.table.set_index("athlete_id")
    peaks = real.table.loc[real.table["n_bouts"] >= 24, "rating_peak"]
    for name in ("Glarner Matthias", "Wicki Joel", "Reichmuth Pirmin"):
        assert t.loc[real.athlete(name), "rating_peak"] > peaks.quantile(0.99), name
    # Forrer peaks in 2013, while the top of the scale is still spreading out (the 20
    # best average 2073 then, 2200+ from 2016): top 3 % all-time, top 4 of his season
    assert t.loc[real.athlete("Forrer Arnold"), "rating_peak"] > peaks.quantile(0.97)
    # Forrer's best years were before 2011: the three younger ones peak higher here
    assert t.loc[real.athlete("Forrer Arnold"), "rating_peak"] < \
        t.loc[real.athlete("Wicki Joel"), "rating_peak"]


def test_real_esaf_winners_lead_after_their_festival(real: Real) -> None:
    """After an ESAF the König is one of the three best-rated participants."""
    fests = pd.read_parquet(real.cfg.processed_dir / "festivals.parquet")
    h = real.result.history
    for year, king in [(2013, "Sempach Matthias"), (2016, "Glarner Matthias"),
                       (2019, "Stucki Christian"), (2022, "Wicki Joel"),
                       (2025, "Orlik Armon")]:
        fid = fests[(fests["eidg_type"] == "ESAF") & (fests["year"] == year)]["fest_id"].iloc[0]
        field = h[h["fest_id"] == fid].sort_values("rating_after", ascending=False)
        assert real.athlete(king) in set(field["athlete_id"].head(3)), (year, king)
        row = field[field["athlete_id"] == real.athlete(king)].iloc[0]
        assert row["rating_after"] > row["rating_before"] and row["score"] > row["expected"]


def test_real_ratings_are_zero_sum_and_centred(real: Real) -> None:
    h = real.result.history
    change = (h["rating_after"] - h["rating_before"]).groupby(h["fest_id"]).sum()
    assert np.allclose(change, 0.0, atol=1e-6)
    assert np.mean(list(real.result.ratings.values())) == pytest.approx(1500.0, abs=1e-6)
    assert h[["rating_before", "rating_after"]].notna().all().all()
    assert h["rating_after"].between(800, 2900).all()   # nothing runs away (844 .. 2823)


def test_real_rankings_exclude_garbage_and_provisional_athletes(real: Real) -> None:
    t = real.table
    ranked = t[t["ranked"]]
    assert 1000 < len(ranked) < len(t)
    assert (ranked["n_bouts"] >= real.cfg.provisional_min_bouts).all()
    assert not ranked["identity_flags"].str.contains("not_a_name").any()
    assert (ranked["days_inactive"] <= 548).all()
    assert t.loc[t["n_bouts"] == 0, "rank"].isna().all()
    one_festival = t[t["n_festivals"] == 1]
    assert len(one_festival) > 1000 and not one_festival["ranked"].any()
    top = ranked.nsmallest(50, "rank")
    assert (top["n_bouts"] >= 100).all()        # nobody is at the top on a handful of bouts
    assert not top["identity_uncertain"].any()  # known unreliable identities are not up there
    s = real.seasons
    assert not s.loc[s["season"] < real.cfg.elo_first_ranked_season, "ranked"].any()


def test_real_no_inflation_after_burn_in(real: Real) -> None:
    drift = ev.season_drift(real.result.history, real.cfg.provisional_min_bouts)
    full = drift.loc[[y for y in drift.index if y >= 2016 and y != 2020]]
    # the active athletes sit above 1500 (the mean of all rated athletes, retired ones
    # included, is exactly 1500): 1545 .. 1578 at K x 2, delta 0.05
    assert full["mean"].between(1500, 1600).all()
    assert full["mean"].max() - full["mean"].min() < 40
    assert full["top20_mean"].max() - full["top20_mean"].min() < 120
    assert (drift["newcomer_mean"].loc[2012:] < 1500).all()  # newcomers are below average


def test_real_predictions_beat_a_coin_flip_and_are_monotone(real: Real) -> None:
    d = real.result.bouts
    test = ev.prediction_metrics(ev.in_seasons(d, ev.TEST))
    assert test["brier"] < 0.12 and test["log_loss"] < 0.51   # E = 0.5: 0.20 / 0.693
    cal = ev.calibration_table(d[d["season"] >= 2013])
    assert cal["observed"].is_monotonic_increasing
    assert (cal["observed"] > 0.5).all()


def test_real_mov_baseline_matches_the_data(real: Real) -> None:
    """`mov_baseline_diff` is meant to be K-neutral: the mean multiplier of a win is 1."""
    wins = real.result.bouts.loc[real.result.bouts["score_a"] != 0.5, "mov_lambda"]
    assert wins.mean() == pytest.approx(1.0, abs=0.02)
    assert ev.mean_win_margin(real.bouts) == pytest.approx(real.cfg.mov_baseline_diff, abs=0.02)
    assert wins.between(real.cfg.mov_lambda_min, real.cfg.mov_lambda_max).all()


def test_real_model_is_symmetric_and_deterministic(real: Real) -> None:
    again = SchwingElo(real.result.params).run(real.bouts.sample(frac=1.0, random_state=1))
    pd.testing.assert_frame_equal(again.history, real.result.history)
    swapped = SchwingElo(real.result.params).run(mirrored(real.bouts)).ratings
    assert max(abs(swapped[a] - r) for a, r in real.result.ratings.items()) < 1e-6


def test_real_k_scale_decision(real: Real) -> None:
    """K x 2 / delta 0.05 against the spec's values (`elo_k_scale = 1.0`, delta 0.10):
    better predictions, a less under-confident and wider scale, about twice the
    movement per festival - and nearly the same order of athletes."""
    spec_params = real.result.params.replace(k_scale=1.0, reversion_delta=0.10)
    spec, spec_table, _ = er.compute(real.bouts, real.athletes, real.identity_map, real.cfg,
                                     spec_params)
    assert real.result.params.k_scale == 2.0 and real.result.params.reversion_delta == 0.05

    def brier(result: ee.EloResult) -> float:
        return ev.prediction_metrics(ev.in_seasons(result.bouts, ev.TEST))["brier"]

    def gap_error(result: ee.EloResult) -> float:
        d = result.bouts
        cal = ev.calibration_table(d[d["season"] >= 2013], bins=(200, 300))
        return float((cal["observed"] - cal["predicted"]).iloc[0])

    def moves(result: ee.EloResult) -> pd.Series:
        h = result.history[result.history["bouts_before"] >= 24]
        return (h["rating_after"] - h["rating_before"]).abs()

    assert brier(real.result) < brier(spec) - 0.008          # 0.1126 vs 0.1249
    assert 0 < gap_error(real.result) < 0.06 < gap_error(spec)   # +0.044 vs +0.111
    ranked, spec_ranked = (t[t["ranked"]].sort_values("rank") for t in (real.table, spec_table))
    assert 1.3 < ranked["rating"].std() / spec_ranked["rating"].std() < 1.6   # 295 vs 201
    assert 1.8 < moves(real.result).mean() / moves(spec).mean() < 2.2         # 26.0 vs 12.9
    assert moves(real.result).max() < 450                                     # 398 vs 172
    assert len(set(ranked.head(20)["athlete_id"]) & set(spec_ranked.head(20)["athlete_id"])) >= 17
    assert len(set(ranked.head(100)["athlete_id"])
               & set(spec_ranked.head(100)["athlete_id"])) >= 90
    # the numbers recorded in docs/progress (only comparable on the same data)
    if str(real.result.as_of.date()) == "2026-09-27" and len(real.result.bouts) == 491_597:
        assert brier(spec) == pytest.approx(0.12495, abs=5e-5)
        assert spec_ranked["rating"].iloc[0] == pytest.approx(2408.6, abs=0.1)
        assert spec_ranked["full_name"].iloc[0] == "Staudenmann Fabian"
        assert brier(real.result) == pytest.approx(0.11263, abs=5e-5)
        assert ranked["rating"].iloc[0] == pytest.approx(2703.4, abs=0.1)
        assert ranked["full_name"].iloc[0] == "Giger Samuel"
