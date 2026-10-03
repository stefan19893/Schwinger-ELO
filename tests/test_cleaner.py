"""Identity-resolution orchestration: resolver contract, baseline, bout remapping.

Offline; tiny synthetic SQLite databases in tmp_path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.db import Festival, connect, upsert_festivals
from src.pipeline import cleaner as cl


# ------------------------------------------------------------------ tiny staging db
def fest(fid: int, date: str, **kw: object) -> Festival:
    return Festival(fest_id=fid, name=f"Fest {fid}", date=date, category="Kantonal",
                    location="X", url=f"https://example.invalid/{fid}", **kw)  # type: ignore[arg-type]


# (raw_id, fest_id, idx, name, birth_year)
RAW = [
    ("100-000", 100, 0, "Muster Hans", "1995"),
    ("100-001", 100, 1, "Beispiel Kurt (SWS)", None),
    ("100-002", 100, 2, "Herger Elias 1", None),   # in-sheet namesakes
    ("100-003", 100, 3, "Herger Elias 2", None),
    ("200-000", 200, 0, "Muster Hans", None),
    ("200-001", 200, 1, "Beispiel Kurt", None),
    ("200-002", 200, 2, "Herger Elias", "2001"),
]
# (bout_id, fest_id, gang, a, b, outcome, grade_a, grade_b, schlussgang, flags)
BOUTS = [
    ("100-1-1", 100, 1, "100-000", "100-001", "WIN_A", 10.0, 8.75, None, ""),
    ("100-2-1", 100, 2, "100-002", "100-003", "DRAW", 9.0, 9.0, 0, ""),
    ("100-3-1", 100, 3, "100-000", "100-002", "WIN_B", None, 9.75, 1, "one_sided"),
    ("200-1-1", 200, 1, "200-000", "200-001", "DRAW", 9.0, 9.0, None, ""),
    ("200-2-1", 200, 2, "200-002", "200-000", "WIN_A", 10.0, None, None, "extra_bout"),
]


def make_db(path: Path, raw: list[tuple] = RAW, bouts: list[tuple] = BOUTS) -> Path:
    conn = connect(path)
    upsert_festivals(conn, [fest(100, "2015-05-01"),
                            fest(200, "2016-06-01", event_flags="team", elo_eligible=False)])
    with conn:
        conn.executemany(
            "INSERT INTO athletes_raw (athlete_raw_id, fest_id, idx, name_raw, name, name_key, "
            "name_base_key, birth_year) VALUES (?,?,?,?,?,?,?,?)",
            [(r, f, i, n, n, n.lower(), n.lower(), y) for r, f, i, n, y in raw])
        conn.executemany(
            "INSERT INTO bouts (bout_id, fest_id, gang_nr, athlete_a_id, athlete_b_id, outcome, "
            "grade_a, grade_b, schlussgang, flags) VALUES (?,?,?,?,?,?,?,?,?,?)", bouts)
    conn.close()
    return path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return make_db(tmp_path / "schwingen.db")


def inputs(db_path: Path, extra: list[pd.DataFrame] | None = None) -> cl.CleanInputs:
    conn = cl.connect_ro(db_path)
    try:
        return cl.load_inputs(conn, extra or [])
    finally:
        conn.close()


# ------------------------------------------------------------------ name helpers
@pytest.mark.parametrize(("raw", "clean"), [
    ("Muster Hans", "Muster Hans"),
    ("Beispiel Kurt (SWS)", "Beispiel Kurt"),
    ("Herger Elias 1", "Herger Elias"),
    ("Koch Silvan 10", "Koch Silvan"),
    ("Kälin Jörg, Einsiedeln", "Kälin Jörg"),
    ("Glarner  Matthias **", "Glarner Matthias"),
])
def test_clean_name(raw: str, clean: str) -> None:
    assert cl.clean_name(raw) == clean


def test_name_key_and_slug() -> None:
    assert cl.name_key("Müller Jörg (1990)") == "müller jörg"
    assert cl.slugify("Müller Jörg") == "muller-jorg"
    assert cl.slugify("Ambühl Joél") == cl.slugify("Ambühl Joel")  # -> needs hash suffix
    assert cl.slugify("???") == "x"


# ------------------------------------------------------------------ loading
def test_load_inputs_joins_festival_and_optional_columns(db: Path) -> None:
    extra = pd.DataFrame({"athlete_raw_id": ["100-000", "200-000"],
                          "club": ["SK Bern", None], "portrait_slug": ["hans-muster", None]})
    extra2 = pd.DataFrame({"athlete_raw_id": ["200-000"], "club": ["SK Thun"]})
    inp = inputs(db, [extra, extra2])
    raw = inp.raw.set_index("athlete_raw_id")
    assert len(raw) == len(RAW)
    for col in (*cl.OPTIONAL_RAW_COLUMNS, *cl.FESTIVAL_JOIN.values(), "fest_year"):
        assert col in raw.columns
    assert raw.loc["100-000", "club"] == "SK Bern"
    assert raw.loc["200-000", "club"] == "SK Thun"  # later frame fills the gap
    assert raw.loc["100-000", "portrait_slug"] == "hans-muster"
    assert pd.isna(raw.loc["100-001", "residence"])
    assert raw.loc["200-000", "fest_year"] == 2016
    assert raw.loc["200-000", "fest_event_flags"] == "team"
    assert len(inp.bouts) == len(BOUTS)
    assert set(inp.festivals["fest_id"]) == {100, 200}


def test_connect_ro_never_writes(db: Path) -> None:
    conn = cl.connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM bouts")
    conn.close()


def test_missing_db_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run `parse` first"):
        cl.connect_ro(tmp_path / "nope.db")


# ------------------------------------------------------------------ baseline resolver
def test_baseline_is_a_resolver() -> None:
    assert isinstance(cl.BaselineResolver(), cl.Resolver)
    assert isinstance(cl.default_resolver(), cl.Resolver)


def test_baseline_merges_across_sheets_but_not_in_sheet_namesakes(db: Path) -> None:
    inp = inputs(db)
    res = cl.BaselineResolver().resolve(cl.ResolverInput(inp.raw, inp.bouts))
    m = dict(zip(res.identity["athlete_raw_id"], res.identity["athlete_id"]))
    assert m["100-000"] == m["200-000"] == "muster-hans"
    assert m["100-001"] == m["200-001"] == "beispiel-kurt"
    # "Herger Elias 1"/"2" on one sheet are two people: never merged with each other
    assert len({m["100-002"], m["100-003"], m["200-002"]}) == 3
    assert m["100-002"] == "herger-elias--100-002"
    ath = res.athletes.set_index("athlete_id")
    assert ath.loc["muster-hans", "full_name"] == "Muster Hans"
    assert ath.loc["muster-hans", "birth_year"] == "1995"
    assert ath.loc["herger-elias--100-002", "confidence"] == 0.0
    assert ath.loc["herger-elias--100-002", "evidence"] == "in_sheet_duplicate"


def test_baseline_slug_clash_gets_hash_suffix(tmp_path: Path) -> None:
    raw = [("100-000", 100, 0, "Ambühl Joel", None), ("200-000", 200, 0, "Ambühl Joél", None)]
    inp = inputs(make_db(tmp_path / "s.db", raw, []))
    res = cl.BaselineResolver().resolve(cl.ResolverInput(inp.raw, inp.bouts))
    ids = sorted(res.identity["athlete_id"])
    assert len(set(ids)) == 2 and all(i.startswith("ambuhl-joel-") for i in ids)
    again = cl.BaselineResolver().resolve(cl.ResolverInput(inp.raw, inp.bouts))
    assert sorted(again.identity["athlete_id"]) == ids  # deterministic


# ------------------------------------------------------------------ remapping
def _ident(pairs: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame({"athlete_raw_id": list(pairs), "athlete_id": list(pairs.values())})


def test_remap_replaces_raw_ids(db: Path) -> None:
    inp = inputs(db)
    ident = _ident({r[0]: f"A{r[0]}" for r in RAW})
    out = cl.remap_bouts(inp.bouts, ident)
    assert len(out.bouts) == len(BOUTS) and out.rejects.empty
    row = out.bouts.set_index("bout_id").loc["100-1-1"]
    assert (row["athlete_a_id"], row["athlete_b_id"]) == ("A100-000", "A100-001")
    assert (row["athlete_a_raw_id"], row["athlete_b_raw_id"]) == ("100-000", "100-001")


def test_remap_rejects_unmapped_and_self_bouts(db: Path) -> None:
    inp = inputs(db)
    ident = _ident({"100-000": "hans", "100-001": "kurt",
                    "100-002": "elias", "100-003": "elias",       # over-merged
                    "200-000": "hans"})                           # 200-001/200-002 unmapped
    out = cl.remap_bouts(inp.bouts, ident)
    reasons = dict(zip(out.rejects["bout_id"], out.rejects["reason"]))
    assert reasons == {"100-2-1": "self_bout", "200-1-1": "unmapped_b", "200-2-1": "unmapped_a"}
    assert list(out.bouts["bout_id"]) == ["100-1-1", "100-3-1"]
    assert out.counts["bouts_in"] == len(out.bouts) + len(out.rejects)  # nothing lost
    assert out.counts["rejected_self_bout"] == 1


def test_remap_unmapped_both() -> None:
    bouts = pd.DataFrame([BOUTS[0]], columns=list(cl.BOUT_COLUMNS))
    out = cl.remap_bouts(bouts, _ident({}))
    assert out.rejects["reason"].tolist() == ["unmapped_both"]


# ------------------------------------------------------------------ contract validation
class FixedResolver:
    def __init__(self, identity: pd.DataFrame, athletes: pd.DataFrame) -> None:
        self.name = "fixed"
        self._res = cl.Resolution(identity, athletes)

    def resolve(self, inp: cl.ResolverInput) -> cl.Resolution:
        return self._res


def _athletes(*ids: str) -> pd.DataFrame:
    return pd.DataFrame({"athlete_id": list(ids), "full_name": [i.title() for i in ids]})


def test_custom_resolver_plugs_in_and_over_merging_is_flagged(db: Path) -> None:
    """A resolver that merges everybody: every bout becomes a self-bout, none silently lost."""
    inp = inputs(db)
    r = FixedResolver(_ident({x[0]: "everyone" for x in RAW}), _athletes("everyone"))
    assert isinstance(r, cl.Resolver)
    result = cl.assemble(inp, r.resolve(cl.ResolverInput(inp.raw, inp.bouts)), r.name)
    assert result.counts["self_bouts"] == len(BOUTS) and result.bouts.empty
    assert result.athletes["n_bouts"].tolist() == [0]
    assert set(result.identity_map["resolver"]) == {"fixed"}


def test_partial_resolution_keeps_unmapped_rows(db: Path) -> None:
    inp = inputs(db)
    res = cl.Resolution(_ident({"100-000": "hans", "200-000": "hans"}), _athletes("hans"))
    result = cl.assemble(inp, res, "partial")
    assert len(result.identity_map) == len(RAW)  # every raw row present
    assert result.counts["raw_unmapped"] == len(RAW) - 2
    assert result.counts["unmapped_bouts"] == len(BOUTS)
    assert len(result.bouts) + len(result.bout_rejects) == len(BOUTS)
    # optional attribute columns are filled with NULL
    assert {"club", "slug", "confidence"} <= set(result.athletes.columns)


@pytest.mark.parametrize(("identity", "athletes", "msg"), [
    ({"athlete_raw_id": ["100-000", "100-000"], "athlete_id": ["a", "b"]}, ["a", "b"], "twice"),
    ({"athlete_raw_id": ["999-000"], "athlete_id": ["a"]}, ["a"], "unknown raw ids"),
    ({"athlete_raw_id": ["100-000"], "athlete_id": ["a"]}, ["b"], "without attributes"),
    ({"athlete_raw_id": ["100-000"], "athlete_id": ["a"]}, ["a", "a"], "unique"),
])
def test_validation_rejects_broken_resolutions(db: Path, identity: dict, athletes: list[str],
                                               msg: str) -> None:
    inp = inputs(db)
    with pytest.raises(ValueError, match=msg):
        cl.validate_resolution(cl.Resolution(pd.DataFrame(identity), _athletes(*athletes)),
                               inp.raw)


def test_orphan_athletes_dropped_with_warning(db: Path, caplog: pytest.LogCaptureFixture) -> None:
    inp = inputs(db)
    res = cl.validate_resolution(
        cl.Resolution(_ident({"100-000": "a"}), _athletes("a", "ghost")), inp.raw)
    assert res.athletes["athlete_id"].tolist() == ["a"]
    assert any("without any raw row" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ assembled frames
def test_assemble_baseline_stats(db: Path) -> None:
    inp = inputs(db)
    result = cl.assemble(inp, cl.BaselineResolver().resolve(
        cl.ResolverInput(inp.raw, inp.bouts)), "baseline")
    a = result.athletes.set_index("athlete_id")
    assert a.loc["muster-hans", "active_years"] == [2015, 2016]
    assert (a.loc["muster-hans", "first_season"], a.loc["muster-hans", "last_season"]) == (2015, 2016)
    assert a.loc["muster-hans", "n_festivals"] == 2
    assert a.loc["muster-hans", "n_bouts"] == 4
    assert result.counts["self_bouts"] == 0
    assert result.counts["bouts_elo_eligible"] == 3  # festival 200 is a team event
    b = result.bouts.set_index("bout_id")
    assert not b.loc["200-1-1", "elo_eligible"] and b.loc["200-1-1", "event_flags"] == "team"
    f = result.festivals.set_index("fest_id")
    assert (f.loc[100, "n_bouts"], f.loc[100, "n_athletes_raw"]) == (3, 4)


# ------------------------------------------------------------------ athlete_evidence
EVIDENCE = [  # (raw id, fest, residence, birth_year, club_key, club, sub, source, pid, slug)
    ("100-000", 100, "Thun", 1994, "thun", "Thun", "BKSV", "code", 7, "hans-muster"),
    ("100-001", 100, "Bulle", 1990, None, None, "SWSV", "festival", None, None),
    ("200-000", 200, "Thun", None, "thun", "Thun", "BKSV", "club", None, None),
]


def add_evidence(db_path: Path) -> None:
    conn = connect(db_path)
    with conn:
        conn.executemany(
            "INSERT INTO athlete_evidence (athlete_raw_id, fest_id, residence, birth_year, "
            "club_key, club, sub_association, sub_assoc_source, portrait_id, portrait_slug) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", EVIDENCE)
    conn.close()


def test_load_evidence_feeds_optional_columns(db: Path) -> None:
    conn = cl.connect_ro(db)
    assert cl.load_evidence(conn) is None  # table empty: parse has not built it
    conn.close()
    add_evidence(db)
    conn = cl.connect_ro(db)
    try:
        ev = cl.load_evidence(conn)
        assert ev is not None and list(ev.columns) == ["athlete_raw_id", *cl.EVIDENCE_COLUMNS]
        raw = cl.load_inputs(conn, [ev]).raw.set_index("athlete_raw_id")
    finally:
        conn.close()
    row = raw.loc["100-000"]
    assert (row["club"], row["club_key"], row["sub_association"], row["sub_assoc_source"],
            row["residence"], row["portrait_slug"], row["portrait_id"]) == (
        "Thun", "thun", "BKSV", "code", "Thun", "hans-muster", 7)
    assert row["birth_year"] == "1995"                     # statistic sheet wins
    assert raw.loc["100-001", "birth_year"] == "1990"      # gap filled, same text form
    assert pd.isna(raw.loc["100-001", "club"]) and raw.loc["100-001", "sub_assoc_source"] == "festival"
    assert pd.isna(raw.loc["100-002", "sub_association"])  # no evidence row


def test_load_evidence_on_a_pre_v6_database(db: Path) -> None:
    conn = connect(db)
    conn.execute("DROP TABLE athlete_evidence")
    conn.commit()
    conn.close()
    conn = cl.connect_ro(db)
    try:
        assert cl.load_evidence(conn) is None
    finally:
        conn.close()


def test_run_clean_writes_club_and_teilverband(db: Path, tmp_path: Path,
                                               caplog: pytest.LogCaptureFixture) -> None:
    import pyarrow.parquet as pq

    out = tmp_path / "processed"
    with caplog.at_level("WARNING"):
        cl.run_clean(db, out)
    assert any("no athlete_evidence" in r.getMessage() for r in caplog.records)
    assert pq.read_table(out / "athletes.parquet").to_pandas()["club"].isna().all()
    add_evidence(db)
    # a caller-supplied frame has priority; the stored evidence fills its gaps
    override = pd.DataFrame({"athlete_raw_id": ["200-000"], "club": ["Thun und Umgebung"]})
    res = cl.run_clean(db, out, resolver=cl.BaselineResolver(), extra_raw=[override])
    athletes = pq.read_table(out / "athletes.parquet").to_pandas().set_index("athlete_id")
    hans = athletes.loc["muster-hans"]
    assert (hans["club"], hans["sub_association"], hans["slug"], hans["birth_year"]) == (
        "Thun und Umgebung", "BKSV", "hans-muster", 1995)
    kurt = athletes.loc["beispiel-kurt"]
    assert kurt["club"] is None and kurt["sub_association"] == "SWSV" and kurt["birth_year"] == 1990
    ident = pq.read_table(out / "identity_map.parquet").to_pandas().set_index("athlete_raw_id")
    assert tuple(ident.loc["100-000", ["club", "sub_association", "residence", "portrait_slug"]]) == (
        "Thun", "BKSV", "Thun", "hans-muster")
    assert ident.loc["100-002", "club"] is None
    assert res.counts["athletes"] == 5  # evidence does not change the baseline identities
