"""SchwingElo engine (spec §4.2): one test per rule, then runs on the --sample data and
sanity checks on the real data (skipped when ``data/processed`` is absent)."""

from __future__ import annotations

import datetime as dt
import itertools
import math

import numpy as np
import pandas as pd
import pytest

from src.config import DEFAULT_K_FACTORS, load_config
from src.pipeline import elo_engine as ee
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
