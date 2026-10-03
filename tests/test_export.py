"""Parquet export of ``clean``: schemas/dtypes, NULL round-trips, atomic writes,
idempotency and ``clean --sample`` end-to-end. Offline."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src import cli
from src.pipeline import cleaner as cl
from src.pipeline import export as ex
from tests.test_cleaner import BOUTS, make_db

FILES = ("bouts", "athletes", "festivals", "identity_map", "bout_rejects")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("SCHWINGEN_"):
            monkeypatch.delenv(key)


@pytest.fixture
def out(tmp_path: Path) -> Path:
    db = make_db(tmp_path / "schwingen.db")
    cl.run_clean(db, tmp_path / "processed")
    return tmp_path / "processed"


def test_all_files_written_with_exact_schema(out: Path) -> None:
    for name in FILES:
        schema = pq.read_schema(out / f"{name}.parquet")
        assert schema.remove_metadata().equals(ex.SCHEMAS[name]), name
    assert not list(out.glob(".*.tmp"))  # no temp leftovers


def test_spec_columns_present() -> None:
    bout = {"bout_id", "fest_id", "gang_nr", "athlete_a_id", "athlete_b_id", "outcome",
            "grade_a", "grade_b", "flags", "schlussgang", "elo_eligible", "event_flags"}
    athlete = {"athlete_id", "full_name", "club", "sub_association", "active_years",
               "first_season", "last_season", "n_festivals", "n_bouts"}
    festival = {"fest_id", "name", "date", "category", "location", "event_flags", "elo_eligible"}
    assert bout <= set(ex.BOUTS_SCHEMA.names)
    assert athlete <= set(ex.ATHLETES_SCHEMA.names)
    assert festival <= set(ex.FESTIVALS_SCHEMA.names)


def test_null_grades_and_schlussgang_round_trip(out: Path) -> None:
    b = pq.read_table(out / "bouts.parquet").to_pandas().set_index("bout_id")
    assert pd.isna(b.loc["100-3-1", "grade_a"]) and b.loc["100-3-1", "grade_b"] == 9.75
    assert b.loc["100-3-1", "flags"] == "one_sided" and b.loc["100-3-1", "schlussgang"] is True
    assert pd.isna(b.loc["200-2-1", "grade_b"]) and b.loc["200-2-1", "flags"] == "extra_bout"
    assert b.loc["100-1-1", "schlussgang"] is None  # unknown stays NULL, not False
    assert b.loc["100-2-1", "schlussgang"] is False
    assert len(b) == len(BOUTS)
    assert b["gang_nr"].dtype == "int8" and b["grade_a"].dtype == "float64"


def test_athletes_and_identity_contents(out: Path) -> None:
    a = pq.read_table(out / "athletes.parquet").to_pandas().set_index("athlete_id")
    assert list(a.loc["muster-hans", "active_years"]) == [2015, 2016]
    assert a.loc["muster-hans", "birth_year"] == 1995
    assert pd.isna(a.loc["beispiel-kurt", "birth_year"])
    i = pq.read_table(out / "identity_map.parquet").to_pandas()
    assert i["athlete_id"].notna().all() and set(i["resolver"]) == {"baseline"}
    f = pq.read_table(out / "festivals.parquet").to_pandas().set_index("fest_id")
    assert f.loc[200, "event_flags"] == "team" and not f.loc[200, "elo_eligible"]
    assert f.loc[100, "year"] == 2015


def test_rejects_written_not_dropped(tmp_path: Path) -> None:
    # a bout whose raw id has no athletes_raw row cannot exist (FK), so force an
    # over-merging resolver instead: rejects must hold every missing bout
    db = make_db(tmp_path / "schwingen.db")

    class Everyone:
        name = "everyone"

        def resolve(self, inp: cl.ResolverInput) -> cl.Resolution:
            ids = inp.raw[["athlete_raw_id"]].assign(athlete_id="x")
            return cl.Resolution(ids, pd.DataFrame({"athlete_id": ["x"], "full_name": ["X"]}))

    res = cl.run_clean(db, tmp_path / "p", resolver=Everyone())
    rej = pq.read_table(tmp_path / "p" / "bout_rejects.parquet").to_pandas()
    assert len(rej) == len(BOUTS) and set(rej["reason"]) == {"self_bout"}
    assert pq.read_table(tmp_path / "p" / "bouts.parquet").num_rows == 0
    assert res.counts["self_bouts"] == len(BOUTS)


def test_idempotent(tmp_path: Path, out: Path) -> None:
    before = {n: (out / f"{n}.parquet").read_bytes() for n in FILES}
    cl.run_clean(tmp_path / "schwingen.db", out)
    after = {n: (out / f"{n}.parquet").read_bytes() for n in FILES}
    assert before == after


def test_atomic_write_keeps_old_file_on_failure(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "t.parquet"
    table = pa.table({"x": [1, 2]})
    ex.write_parquet_atomic(table, path)

    def boom(*a: object, **k: object) -> None:
        Path(a[1]).write_bytes(b"partial")  # type: ignore[arg-type]
        raise OSError("disk full")

    monkeypatch.setattr(ex.pq, "write_table", boom)
    with pytest.raises(OSError):
        ex.write_parquet_atomic(pa.table({"x": [3]}), path)
    assert pq.read_table(path).column("x").to_pylist() == [1, 2]
    assert [p.name for p in tmp_path.iterdir()] == ["t.parquet"]


def test_schema_violation_raises_before_writing(tmp_path: Path) -> None:
    df = pd.DataFrame({"athlete_raw_id": ["1-0"], "athlete_id": [None], "fest_id": [1],
                       "name_raw": [None], "name": ["x"], "birth_year": [None],
                       "confidence": [None], "evidence": [None], "resolver": ["r"]})
    with pytest.raises(ValueError, match="name_raw"):
        ex.to_table(df, ex.IDENTITY_SCHEMA)
    with pytest.raises(KeyError, match="missing columns"):
        ex.to_table(df.drop(columns="resolver"), ex.IDENTITY_SCHEMA)


# ------------------------------------------------------------------ CLI end-to-end
def test_clean_sample_end_to_end(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    data = tmp_path / "data"
    assert cli.main(["clean", "--sample", "--data-dir", str(data)]) == 0  # bootstraps db
    proc = data / "processed"
    for name in FILES:
        assert pq.read_schema(proc / f"{name}.parquet").remove_metadata().equals(ex.SCHEMAS[name])
    bouts = pq.read_table(proc / "bouts.parquet").to_pandas()
    ident = pq.read_table(proc / "identity_map.parquet").to_pandas()
    athletes = pq.read_table(proc / "athletes.parquet").to_pandas()
    assert len(bouts) > 1000 and ident["athlete_id"].notna().all()
    assert set(bouts["athlete_a_id"]) | set(bouts["athlete_b_id"]) <= set(athletes["athlete_id"])
    assert sorted(bouts["fest_id"].unique()) == [24110, 26400, 37052, 45965, 46055]
    assert athletes["n_bouts"].sum() == 2 * len(bouts)
    assert any("self-bouts" in r.getMessage() for r in caplog.records)
    first = {n: (proc / f"{n}.parquet").read_bytes() for n in FILES}
    assert cli.main(["clean", "--sample", "--data-dir", str(data)]) == 0  # db reused
    assert {n: (proc / f"{n}.parquet").read_bytes() for n in FILES} == first


def test_clean_without_db_fails(tmp_path: Path) -> None:
    assert cli.main(["clean", "--data-dir", str(tmp_path / "empty")]) == 1
