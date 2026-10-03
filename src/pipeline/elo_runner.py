"""``python -m src.cli elo``: Parquet in, ratings out (spec §4.2 / §5).

Reads ``bouts.parquet``, ``athletes.parquet`` and ``identity_map.parquet`` from
``data/processed/`` (written by ``clean``) and writes three files next to them:

* ``ratings.parquet``         the full rating history — one row per athlete and
                              festival: ``athlete_id, date, fest_id, rating_before,
                              rating_after`` plus ``season`` (calendar year), ``category``,
                              ``n_bouts``, ``score``, ``expected``, ``bouts_before``,
                              ``days_inactive``, ``provisional``, ``provisional_reason``.
                              ``rating_before`` already contains the April mean reversion,
                              so it can differ from the previous row's ``rating_after``.
* ``athlete_ratings.parquet`` one row per athlete of ``athletes.parquet``: current rating
                              (as of the last rated festival), peak, activity, the
                              provisional status, the identity-uncertainty marker and the
                              current rank.
* ``season_ratings.parquet``  one row per athlete and calendar year he fought in: rating
                              at the end of the year, rank among the ranked athletes.

Who is ranked (``ranked`` / ``rank``; everybody is *rated*):

* not a ``not_a_name`` athlete (garbage rows of the sheets) and at least one rated bout,
* at least ``provisional_min_bouts`` career bouts (``few_bouts``),
* current ranking only: a bout within the last 1.5 seasons (``inactive``),
* season rankings only: not a burn-in season (before ``elo_first_ranked_season``).

``athlete_id`` values are only valid within one run of ``clean``: never join these
files to the athletes of another run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pyarrow as pa

from src.pipeline.elo_engine import EloParams, EloResult, SchwingElo
from src.pipeline.export import write_parquet_atomic

if TYPE_CHECKING:
    from src.config import Config

log = logging.getLogger("schwingen.elo")

NOT_A_NAME = "not_a_name"

_S, _F, _B, _D = pa.string(), pa.float64(), pa.bool_(), pa.date32()
_I16, _I32, _I64 = pa.int16(), pa.int32(), pa.int64()


def _schema(*cols: tuple[str, pa.DataType, bool]) -> pa.Schema:
    return pa.schema([pa.field(n, t, nullable=nullable) for n, t, nullable in cols])


RATINGS_SCHEMA = _schema(
    ("athlete_id", _S, False), ("date", _D, False), ("fest_id", _I64, False),
    ("rating_before", _F, False), ("rating_after", _F, False), ("season", _I16, False),
    ("category", _S, False), ("n_bouts", _I16, False), ("score", _F, False),
    ("expected", _F, False), ("bouts_before", _I32, False), ("days_inactive", _I32, True),
    ("provisional", _B, False), ("provisional_reason", _S, False))

ATHLETE_RATINGS_SCHEMA = _schema(
    ("athlete_id", _S, False), ("full_name", _S, False), ("rating", _F, True),
    ("rating_peak", _F, True), ("peak_date", _D, True), ("peak_fest_id", _I64, True),
    ("n_bouts", _I32, False), ("n_festivals", _I32, False), ("first_date", _D, True),
    ("last_date", _D, True), ("days_inactive", _I32, True), ("provisional", _B, False),
    ("provisional_reason", _S, False), ("identity_rows", _I32, False),
    ("identity_low_conf_rows", _I32, False), ("identity_low_conf_share", _F, False),
    ("identity_uncertain", _B, False), ("identity_flags", _S, False),
    ("ranked", _B, False), ("rank", _I32, True))

SEASON_RATINGS_SCHEMA = _schema(
    ("athlete_id", _S, False), ("season", _I16, False), ("rating_end", _F, False),
    ("rating_peak", _F, False), ("n_bouts", _I32, False), ("n_festivals", _I32, False),
    ("bouts_total", _I32, False), ("provisional", _B, False), ("burn_in", _B, False),
    ("ranked", _B, False), ("rank", _I32, True))

SCHEMAS: dict[str, pa.Schema] = {
    "ratings": RATINGS_SCHEMA, "athlete_ratings": ATHLETE_RATINGS_SCHEMA,
    "season_ratings": SEASON_RATINGS_SCHEMA}


@dataclass
class EloOutputs:
    result: EloResult
    ratings: pd.DataFrame
    athletes: pd.DataFrame
    seasons: pd.DataFrame
    paths: dict[str, Path]


# --------------------------------------------------------------------------- identity
def identity_uncertainty(athletes: pd.DataFrame, identity_map: pd.DataFrame | None,
                         low_confidence: float = 0.4, min_rows: int = 10,
                         min_share: float = 0.25) -> pd.DataFrame:
    """Per athlete: number of sheet rows, how many of them were assigned with
    ``confidence <= low_confidence`` (undecidable between namesakes, unbridged career
    gap, name-only among namesakes), and the resolver's notes. ``identity_uncertain``
    marks athletes whose rating rests on such rows to a relevant degree
    (``>= min_rows`` rows or ``>= min_share`` of the rows)."""
    out = pd.DataFrame({"athlete_id": athletes["athlete_id"].to_numpy()})
    if identity_map is not None and len(identity_map):
        im = identity_map.dropna(subset=["athlete_id"])
        rows = im.groupby("athlete_id").size()
        low = im[im["confidence"].fillna(0.0) <= low_confidence].groupby("athlete_id").size()
        out["identity_rows"] = out["athlete_id"].map(rows).fillna(0).astype(int)
        out["identity_low_conf_rows"] = out["athlete_id"].map(low).fillna(0).astype(int)
    else:
        out["identity_rows"] = 0
        out["identity_low_conf_rows"] = 0
    share = out["identity_low_conf_rows"] / out["identity_rows"].where(out["identity_rows"] > 0, 1)
    out["identity_low_conf_share"] = share.astype(float)
    out["identity_uncertain"] = (out["identity_low_conf_rows"] >= min_rows) | \
        ((share >= min_share) & (out["identity_low_conf_rows"] > 0))
    notes = athletes["evidence"] if "evidence" in athletes.columns else None
    out["identity_flags"] = "" if notes is None else notes.fillna("").astype(str).to_numpy()
    return out


def drop_low_confidence_bouts(bouts: pd.DataFrame, identity_map: pd.DataFrame,
                              low_confidence: float = 0.4) -> pd.DataFrame:
    """Sensitivity pass: the bouts without those where either side's sheet row was
    assigned to its athlete with ``confidence <= low_confidence``."""
    low = set(identity_map.loc[identity_map["confidence"].fillna(0.0) <= low_confidence,
                               "athlete_raw_id"])
    keep = ~(bouts["athlete_a_raw_id"].isin(low) | bouts["athlete_b_raw_id"].isin(low))
    return bouts[keep]


# --------------------------------------------------------------------------- tables
def _rank(rating: pd.Series, ranked: pd.Series, ids: pd.Series) -> pd.Series:
    """1 = best among the ranked rows; ties broken by athlete id (deterministic)."""
    order = pd.DataFrame({"r": -rating, "id": ids})[ranked].sort_values(["r", "id"]).index
    rank = pd.Series(pd.array([pd.NA] * len(rating), dtype="Int64"), index=rating.index)
    rank.loc[order] = np.arange(1, len(order) + 1)
    return rank


def athlete_table(result: EloResult, athletes: pd.DataFrame,
                  identity: pd.DataFrame, first_ranked_season: int = 0) -> pd.DataFrame:
    """Current standing of every athlete of ``athletes`` (unrated ones included)."""
    p, h = result.params, result.history
    out = identity.merge(athletes[["athlete_id", "full_name"]], on="athlete_id", how="left",
                         validate="one_to_one")
    grp = h.groupby("athlete_id", sort=False)
    out["rating"] = out["athlete_id"].map(result.ratings)
    out["n_bouts"] = out["athlete_id"].map(grp["n_bouts"].sum()).fillna(0).astype(int)
    out["n_festivals"] = out["athlete_id"].map(grp.size()).fillna(0).astype(int)
    out["first_date"] = out["athlete_id"].map(grp["date"].min())
    out["last_date"] = out["athlete_id"].map(grp["date"].max())
    # peak: best rating after a festival once the athlete is no longer `few_bouts`
    # and outside the burn-in seasons
    solid = h[(h["bouts_before"] + h["n_bouts"] >= p.provisional_min_bouts)
              & (h["season"] >= first_ranked_season)]
    peak = solid.loc[solid.groupby("athlete_id", sort=False)["rating_after"].idxmax()] \
        .set_index("athlete_id") if len(solid) else solid.set_index("athlete_id")
    out["rating_peak"] = out["athlete_id"].map(peak["rating_after"])
    out["peak_date"] = out["athlete_id"].map(peak["date"])
    out["peak_fest_id"] = out["athlete_id"].map(peak["fest_id"]).astype("Int64")
    idle = (result.as_of - out["last_date"]).dt.days if result.as_of is not None \
        else pd.Series(np.nan, index=out.index)
    out["days_inactive"] = idle.astype("Int64")
    few = (out["n_bouts"] < p.provisional_min_bouts) & (out["n_bouts"] > 0)
    inactive = (idle > p.inactive_days).fillna(False)
    out["provisional"] = few | inactive
    out["provisional_reason"] = np.where(
        few & inactive, "few_bouts,inactive",
        np.where(few, "few_bouts", np.where(inactive, "inactive", "")))
    not_a_name = out["identity_flags"].str.split(r"[;|]").map(lambda xs: NOT_A_NAME in xs)
    out["ranked"] = (out["n_bouts"] > 0) & ~not_a_name & ~out["provisional"]
    out["rank"] = _rank(out["rating"], out["ranked"], out["athlete_id"])
    cols = [f.name for f in ATHLETE_RATINGS_SCHEMA]
    return out.sort_values("athlete_id", kind="mergesort").reset_index(drop=True)[cols]


def season_table(result: EloResult, athlete_ratings: pd.DataFrame,
                 first_ranked_season: int = 0) -> pd.DataFrame:
    """Season-end standings: one row per athlete and calendar year with a bout."""
    p, h = result.params, result.history
    if h.empty:
        return pd.DataFrame({f.name: [] for f in SEASON_RATINGS_SCHEMA})
    grp = h.groupby(["athlete_id", "season"], sort=True)
    last = grp.tail(1).set_index(["athlete_id", "season"])
    out = pd.DataFrame({
        "rating_end": last["rating_after"], "rating_peak": grp["rating_after"].max(),
        "n_bouts": grp["n_bouts"].sum(), "n_festivals": grp.size(),
        "bouts_total": last["bouts_before"] + last["n_bouts"]}).reset_index()
    out["provisional"] = out["bouts_total"] < p.provisional_min_bouts
    out["burn_in"] = out["season"] < first_ranked_season
    not_a_name = set(athlete_ratings.loc[athlete_ratings["identity_flags"].str.split(r"[;|]")
                                         .map(lambda xs: NOT_A_NAME in xs), "athlete_id"])
    out["ranked"] = ~out["provisional"] & ~out["burn_in"] & ~out["athlete_id"].isin(not_a_name)
    out["rank"] = pd.array([pd.NA] * len(out), dtype="Int64")
    for _, idx in out.groupby("season").groups.items():
        part = out.loc[idx]
        out.loc[idx, "rank"] = _rank(part["rating_end"], part["ranked"], part["athlete_id"])
    cols = [f.name for f in SEASON_RATINGS_SCHEMA]
    return out.sort_values(["season", "athlete_id"], kind="mergesort").reset_index(drop=True)[cols]


def _to_table(df: pd.DataFrame, schema: pa.Schema) -> pa.Table:
    """Explicit schema, no pandas metadata: identical inputs give identical files."""
    cols = {}
    for f in schema:
        s = df[f.name]
        if pa.types.is_date32(f.type):
            s = pd.to_datetime(s).dt.date
        cols[f.name] = pa.array(s, type=f.type, from_pandas=True)
        if not f.nullable and cols[f.name].null_count:
            raise ValueError(f"column {f.name}: NULL in a non-nullable column")
    return pa.table(cols, schema=schema)


# --------------------------------------------------------------------------- run
def load_inputs(processed_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame | None]:
    """``(bouts, athletes, identity_map or None)`` as written by ``clean``."""
    bouts = pd.read_parquet(processed_dir / "bouts.parquet")
    athletes = pd.read_parquet(processed_dir / "athletes.parquet")
    im_path = processed_dir / "identity_map.parquet"
    identity_map = pd.read_parquet(im_path) if im_path.is_file() else None
    return bouts, athletes, identity_map


def compute(bouts: pd.DataFrame, athletes: pd.DataFrame, identity_map: pd.DataFrame | None,
            cfg: Config, params: EloParams | None = None
            ) -> tuple[EloResult, pd.DataFrame, pd.DataFrame]:
    """Run the engine and build the athlete and season tables (no file access)."""
    params = params or EloParams.from_config(cfg)
    result = SchwingElo(params).run(bouts)
    known = set(athletes["athlete_id"])
    unknown = sorted(set(result.ratings) - known)
    if unknown:
        raise ValueError(f"bouts reference {len(unknown)} athletes missing from "
                         f"athletes.parquet (e.g. {unknown[:3]}) - re-run `clean`")
    identity = identity_uncertainty(
        athletes, identity_map, cfg.identity_low_confidence,
        cfg.identity_uncertain_min_rows, cfg.identity_uncertain_min_share)
    table = athlete_table(result, athletes, identity, cfg.elo_first_ranked_season)
    seasons = season_table(result, table, cfg.elo_first_ranked_season)
    return result, table, seasons


def run_elo(cfg: Config) -> EloOutputs:
    """Compute all ratings from ``cfg.processed_dir`` and write the three outputs."""
    bouts, athletes, identity_map = load_inputs(cfg.processed_dir)
    result, table, seasons = compute(bouts, athletes, identity_map, cfg)
    frames = {"ratings": result.history, "athlete_ratings": table, "season_ratings": seasons}
    tables = {name: _to_table(df, SCHEMAS[name]) for name, df in frames.items()}
    paths: dict[str, Path] = {}
    for name, tab in tables.items():  # converted (validated) before any file is replaced
        paths[name] = cfg.processed_dir / f"{name}.parquet"
        write_parquet_atomic(tab, paths[name])
        log.info("elo: wrote %s (%d rows, %.1f KiB)", paths[name], tab.num_rows,
                 paths[name].stat().st_size / 1024)
    return EloOutputs(result=result, ratings=result.history, athletes=table, seasons=seasons,
                      paths=paths)


def top_table(table: pd.DataFrame, n: int = 10) -> list[str]:
    """The current top ``n`` as log lines."""
    top = table[table["ranked"]].sort_values("rank").head(n)
    return [f"{int(r.rank):>3}  {r.rating:7.1f}  {r.full_name}"
            f"{'  [identity uncertain]' if r.identity_uncertain else ''}"
            for r in top.itertuples()]
