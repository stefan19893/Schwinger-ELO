"""Parquet outputs of ``clean`` (spec §3 / §4.1) in ``data/processed/``.

Files (explicit pyarrow schemas, written atomically: temp file + rename):

* ``bouts.parquet``         spec Bout + flags/schlussgang, raw ids, festival date/
                            category/event_flags/elo_eligible (Phase 4 filters; all
                            bouts are exported). NULL grades are preserved.
* ``athletes.parquet``      spec Athlete + birth_year/slug, active_years (list) and
                            first/last season (calendar year of the festival),
                            n_festivals / n_bouts / n_raw, confidence / evidence.
* ``festivals.parquet``     spec Festival + source metadata, parse status, counts.
* ``identity_map.parquet``  every athletes_raw row -> athlete_id (NULL = unmapped).
* ``bout_rejects.parquet``  bouts not exported, with ``reason``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    from src.pipeline.cleaner import CleanResult

log = logging.getLogger("schwingen.clean")

_S, _I8, _I16, _I32, _I64 = pa.string(), pa.int8(), pa.int16(), pa.int32(), pa.int64()
_F, _B, _D = pa.float64(), pa.bool_(), pa.date32()


def _schema(*cols: tuple[str, pa.DataType, bool]) -> pa.Schema:
    return pa.schema([pa.field(n, t, nullable=nullable) for n, t, nullable in cols])


_BOUT_CORE = (
    ("bout_id", _S, False), ("fest_id", _I64, False), ("gang_nr", _I8, False),
    ("athlete_a_id", _S, True), ("athlete_b_id", _S, True), ("outcome", _S, False),
    ("grade_a", _F, True), ("grade_b", _F, True), ("schlussgang", _B, True),
    ("flags", _S, False), ("athlete_a_raw_id", _S, False), ("athlete_b_raw_id", _S, False))

BOUTS_SCHEMA = _schema(
    *[(n, t, nl and n not in ("athlete_a_id", "athlete_b_id")) for n, t, nl in _BOUT_CORE],
    ("date", _D, False), ("category", _S, True), ("event_flags", _S, False),
    ("elo_eligible", _B, False))

BOUT_REJECTS_SCHEMA = _schema(*_BOUT_CORE, ("reason", _S, False))

ATHLETES_SCHEMA = _schema(
    ("athlete_id", _S, False), ("full_name", _S, False), ("club", _S, True),
    ("sub_association", _S, True), ("birth_year", _I16, True), ("slug", _S, True),
    ("active_years", pa.list_(_I16), False), ("first_season", _I16, False),
    ("last_season", _I16, False), ("n_festivals", _I32, False), ("n_bouts", _I32, False),
    ("n_raw", _I32, False), ("confidence", _F, True), ("evidence", _S, True))

FESTIVALS_SCHEMA = _schema(
    ("fest_id", _I64, False), ("name", _S, False), ("date", _D, False), ("year", _I16, False),
    ("category", _S, True), ("eidg_type", _S, True), ("location", _S, True),
    ("kind", _S, False), ("cancelled", _B, False), ("source_category", _S, True),
    ("association", _S, True), ("esv_id", _I64, True), ("event_type", _S, True),
    ("participant_count", _I32, True), ("url", _S, False), ("statistic_pdf_url", _S, True),
    ("event_flags", _S, False), ("elo_eligible", _B, False), ("parse_status", _S, True),
    ("n_gaenge", _I8, True), ("n_athletes_raw", _I32, False), ("n_bouts", _I32, False))

IDENTITY_SCHEMA = _schema(
    ("athlete_raw_id", _S, False), ("athlete_id", _S, True), ("fest_id", _I64, False),
    ("name_raw", _S, False), ("name", _S, False), ("birth_year", _I16, True),
    ("confidence", _F, True), ("evidence", _S, True), ("resolver", _S, False))

SCHEMAS: dict[str, pa.Schema] = {
    "bouts": BOUTS_SCHEMA, "athletes": ATHLETES_SCHEMA, "festivals": FESTIVALS_SCHEMA,
    "identity_map": IDENTITY_SCHEMA, "bout_rejects": BOUT_REJECTS_SCHEMA,
}


def _to_python(v: object) -> object:
    """pandas/numpy scalars and NaN -> plain Python / None for pa.array."""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return [_to_python(x) for x in v]
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v.item() if hasattr(v, "item") else v


def _column(series: pd.Series, field: pa.Field) -> pa.Array:
    t = field.type
    if pa.types.is_date32(t):
        series = pd.to_datetime(series, format="ISO8601").dt.date
    elif pa.types.is_boolean(t):
        series = series.map(lambda v: None if _to_python(v) is None else bool(v))
    elif pa.types.is_integer(t):
        # text birth years ('1995') and float columns with NaN
        series = pd.to_numeric(series, errors="raise").astype("Int64")
    elif pa.types.is_floating(t):
        series = pd.to_numeric(series, errors="raise").astype("Float64")
    arr = pa.array([_to_python(v) for v in series], type=t)
    if not field.nullable and arr.null_count:
        raise ValueError(f"column {field.name}: {arr.null_count} NULLs in a non-null column")
    return arr


def to_table(df: pd.DataFrame, schema: pa.Schema) -> pa.Table:
    """Exactly ``schema``'s columns, in order, cast explicitly (missing column = error)."""
    missing = [f.name for f in schema if f.name not in df.columns]
    if missing:
        raise KeyError(f"missing columns {missing}")
    return pa.Table.from_arrays([_column(df[f.name], f) for f in schema], schema=schema)


def write_parquet_atomic(table: pa.Table, path: Path) -> None:
    """Write to a temp file in the target directory, then rename over ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # plain file creation (umask permissions, unlike mkstemp's 0600); pid-unique name
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_outputs(result: CleanResult, out_dir: Path) -> dict[str, Path]:
    """Write all clean outputs; returns ``{name: path}``. Tables are converted
    (and validated) before any file is replaced, so a schema error leaves the
    previous outputs untouched."""
    fest = result.festivals.assign(year=result.festivals["date"].str[:4].astype(int))
    frames = {
        "bouts": result.bouts.sort_values(["date", "fest_id", "gang_nr", "bout_id"]),
        "athletes": result.athletes,
        "festivals": fest,
        "identity_map": result.identity_map.sort_values("athlete_raw_id"),
        "bout_rejects": result.bout_rejects.sort_values("bout_id"),
    }
    tables = {name: to_table(df, SCHEMAS[name]) for name, df in frames.items()}
    paths: dict[str, Path] = {}
    for name, table in tables.items():
        path = out_dir / f"{name}.parquet"
        write_parquet_atomic(table, path)
        paths[name] = path
        log.info("clean: wrote %s (%d rows, %.1f KiB)", path, table.num_rows,
                 path.stat().st_size / 1024)
    return paths
