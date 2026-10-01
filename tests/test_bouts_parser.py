"""Statistic-sheet parser tests against saved schlussgang sheets (offline).

Fixtures: ``<fest_id>.txt`` (PDF text extracted with PDFium) and two real PDFs in
``tests/fixtures/statistic/`` or (sheets of the ``--sample`` dataset)
``tests/fixtures/sample/statistic/``, plus ``statistic/festivals.json`` (festival rows).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from src.scraper import bouts_parser as bp
from src.scraper.statistic_pdfs import pdf_to_text
from tests.fixture_paths import STATISTIC as FIX
from tests.fixture_paths import sheet_file
META: dict[str, dict[str, Any]] = json.loads((FIX / "festivals.json").read_text(encoding="utf-8"))
SHARED_SHEET = {"23796": "23674"}  # Oberarth 2022's sheet is stored once (shared URL)


def text(fid: int | str) -> str:
    return sheet_file(f"{SHARED_SHEET.get(str(fid), fid)}.txt").read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def parsed(fid: int) -> bp.FestivalParse:
    m = META[str(fid)]
    return bp.parse_festival(text(fid), fid, m["date"], m["name"],
                             max_gang=8 if m["category"] == "ESAF" else 6)


def reasons(res: bp.FestivalParse) -> Counter[str]:
    return Counter(r.reason for r in res.rejects)


def athlete(res: bp.FestivalParse, name: str) -> dict[str, Any]:
    hits = [a for a in res.athletes if a["name"] == name]
    assert len(hits) == 1, (name, [a["name"] for a in res.athletes if name.split()[0] in a["name"]])
    return hits[0]


def bouts_between(res: bp.FestivalParse, n1: str, n2: str) -> list[dict[str, Any]]:
    ids = {athlete(res, n1)["athlete_raw_id"]: n1, athlete(res, n2)["athlete_raw_id"]: n2}
    return sorted((b for b in res.bouts if {b["athlete_a_id"], b["athlete_b_id"]} == set(ids)),
                  key=lambda b: b["gang_nr"])


def result_for(bout: dict[str, Any], res: bp.FestivalParse, name: str) -> tuple[str, float]:
    """('W'|'L'|'D', grade) from the perspective of ``name``."""
    me = athlete(res, name)["athlete_raw_id"]
    is_a = bout["athlete_a_id"] == me
    grade = bout["grade_a"] if is_a else bout["grade_b"]
    if bout["outcome"] == "DRAW":
        return "D", grade
    won = (bout["outcome"] == "WIN_A") == is_a
    return ("W" if won else "L"), grade


# ------------------------------------------------------------------ whole sheets
@pytest.mark.parametrize("fid", [int(k) for k in META])
def test_every_entry_is_accounted_for(fid: int) -> None:
    """Nothing is dropped silently: entries = 2*bouts + entry rejects + 2*bout rejects."""
    res = parsed(fid)
    if "low_pair_rate" in reasons(res):  # bouts deliberately withdrawn, festival-level reject
        return
    entry = sum(1 for r in res.rejects if r.stage == "entry")
    bout = sum(1 for r in res.rejects if r.stage == "bout")
    assert res.entries_total == bp.paired_entries(res) + entry + 2 * bout


@pytest.mark.parametrize("fid", [int(k) for k in META])
def test_every_bout_like_line_is_accounted_for(fid: int) -> None:
    """No line that looks like a bout entry vanishes: it is an entry of an athlete, a
    'line' reject, or part of a youth section (skipped on purpose)."""
    m = META[str(fid)]
    sheet = bp.parse_sheet(text(fid), int(m["date"][:4]))
    if sheet.layout in ("empty", "garbled"):
        return
    used = {e.line for b in sheet.blocks for e in b.entries} | {b.line for b in sheet.blocks}
    used |= {line for line, _, _ in sheet.suspicious} | sheet.youth_lines
    lines = bp.normalize_text(text(fid))
    lost = [ln for i, ln in enumerate(lines) if LOOKS_LIKE_BOUT.match(ln) and i not in used
            and "gewonnen" not in ln]  # symbol legend "+ gewonnen - gestellt o verloren"
    assert lost == []
    assert not any(LOOKS_LIKE_BOUT.match(ln) for _, ln in sheet.noise)


# deliberately looser than the parser's BOUT_LIKE_RE: symbol + any letter
LOOKS_LIKE_BOUT = re.compile(r"^(?:z\s+)?s?(?:[+\-]\s*|[o0u]\s+)[^\W\d_]{2}")


@pytest.mark.parametrize(("fid", "layout", "status", "athletes", "bouts"), [
    (46055, "standard", "ok", 99, 274),        # normal recent 6-Gang festival
    (24110, "standard", "ok", 276, 909),       # ESAF 2019 (1 extra bout)
    (21055, "standard", "ok", 274, 919),       # ESAF 2025 (1 extra bout)
    (26296, "multicol", "ok", 57, 171),        # 2012 multi-column, youth-mixed
    (26412, "multicol", "partial", 101, 280),  # 2011 multi-column Bergkranz
    (26414, "blocks", "partial", 83, 212),     # 2011 block layout
    (26413, "rang", "partial", 134, 372),      # 2011 Rang: layout
    (26108, "rang", "partial", 64, 177),       # 2013 Rang: variant
    (24013, "standard", "ok", 62, 179),        # Jungaktive, S/T suffixes
    (45965, "standard", "ok", 67, 198),        # Hallenschwinget (2 extra bouts, 0.00)
    (24434, "standard", "ok", 130, 367),       # extra bout 0.00 (Schlussgang loser)
    (22547, "standard", "ok", 50, 149),        # extra bouts 0.25
    (25977, "blocks", "partial", 66, 166),     # truncated sheet, no-grade Schlussgang line
    (26200, "standard", "ok", 34, 102),        # wrapped entry lines
    (23796, "standard", "ok", 70, 210),        # true owner of the shared sheet
])
def test_sheet_totals(fid: int, layout: str, status: str, athletes: int, bouts: int) -> None:
    res = parsed(fid)
    assert (res.layout, res.status, len(res.athletes), len(res.bouts)) == \
        (layout, status, athletes, bouts)


def test_klewenalp_clean_sheet() -> None:
    res = parsed(46055)
    assert res.rejects == [] and res.header_check == "ok"
    # every athlete's grades add up to his printed points
    assert all(a["grade_sum"] == a["points"] for a in res.athletes)
    # Gang 1: Scherrer - Bruhin gestellt ('-' on both sides, 8.75)
    g1 = bouts_between(res, "Scherrer Fabian", "Bruhin Fredi")
    assert [(b["gang_nr"], b["outcome"], b["grade_a"], b["grade_b"]) for b in g1] == \
        [(1, "DRAW", 8.75, 8.75)]
    # Schlussgang: Waser prints 'o Scherrer 8.75' = a plain loss
    sg = bouts_between(res, "Scherrer Fabian", "Waser Christoph")
    assert [(b["gang_nr"],) + result_for(b, res, "Scherrer Fabian") for b in sg] == [(6, "W", 10.0)]
    assert result_for(sg[0], res, "Waser Christoph") == ("L", 8.75)


def test_name_suffixes_are_kept_raw() -> None:
    res = parsed(46055)
    e1, e2 = athlete(res, "Herger Elias 1"), athlete(res, "Herger Elias 2")
    assert e1["athlete_raw_id"] != e2["athlete_raw_id"]
    assert bouts_between(res, "Züger Benjamin", "Herger Elias 2")  # resolved to the right one


def test_esaf_eight_gaenge_and_rematch() -> None:
    res = parsed(24110)
    gaenge = Counter(b["gang_nr"] for b in res.bouts)
    assert max(gaenge) == 8 and gaenge[8] > 0
    sw = bouts_between(res, "Stucki Christian", "Wicki Joel")
    assert [(b["gang_nr"],) + result_for(b, res, "Stucki Christian") for b in sw] == \
        [(5, "D", 9.0), (8, "W", 10.0)]
    assert athlete(res, "Odermatt Adrian (2001)")["birth_year"] == "2001"
    assert athlete(res, "Streiff Dominik")["withdrawn"] is True
    assert reasons(res) == Counter()
    # Bernold Christian's 9th line "o Fellmann Roman ** 0.00" is an extra bout
    (eb,) = bouts_between(res, "Fellmann Roman", "Bernold Christian")
    assert "extra_bout" in eb["flags"] and eb["gang_nr"] == 8  # Fellmann's 8th entry
    assert result_for(eb, res, "Fellmann Roman") == ("W", 10.0)
    assert result_for(eb, res, "Bernold Christian") == ("L", None)
    assert sum("extra_bout" in b["flags"] for b in res.bouts) == 1


def test_esaf_two_day_header() -> None:
    assert bp.verify_header(["Glarnerland+, Mollis, 30.-31.08.2025"], "2025-08-30", "x")[0] == "ok"
    assert parsed(21055).header_check == "ok"


def test_draw_heavy_sheet() -> None:
    res = parsed(37052)
    draws = sum(b["outcome"] == "DRAW" for b in res.bouts) / len(res.bouts)
    assert draws > 0.4


def test_youth_mixed_keeps_only_actives() -> None:
    res = parsed(25799)  # Schattdorf 2014: Aktive, then Kat. B-E
    names = {a["name"] for a in res.athletes}
    assert "Kempf Elias" in names
    assert "Zurfluh Michael" not in names  # Kat. B winner
    res2 = parsed(26296)  # Le Mouret 2012: youth sections first, "Actif" last
    assert res.youth_blocks > 0 and res2.youth_blocks == 96
    assert "Duplan Steve (SWS)" not in {a["name"] for a in res2.athletes}  # 1997 category


def test_z_line_extra_bout_is_kept() -> None:
    """'z - Walker Marcel' (no grade) on Herger's list: extra bout, drawn."""
    res = parsed(25799)
    (b,) = bouts_between(res, "Herger Andreas", "Walker Marcel")
    assert "extra_bout" in b["flags"] and b["outcome"] == "DRAW"
    assert "extra_bout_without_grade" not in reasons(res)


def test_injury_forfeits_are_not_bouts() -> None:
    assert reasons(parsed(26412))["forfeit_injury"] == 2   # "> unfall"
    assert reasons(parsed(26413))["forfeit_injury"] == 2   # 'u' symbol


def test_zero_grade_extra_bouts_are_kept() -> None:
    """0.00 on the surplus (7th) line is an extra bout, not a forfeit."""
    res = parsed(45965)  # Kirchberg 2025: "o Thöni Pius 0.00" is Maurer Sam's 7th line
    assert "forfeit_injury" not in reasons(res)
    (b,) = bouts_between(res, "Maurer Sam", "Thöni Pius")
    assert "extra_bout" in b["flags"]
    assert result_for(b, res, "Thöni Pius") == ("W", 10.0)
    assert result_for(b, res, "Maurer Sam") == ("L", None)
    assert athlete(res, "Maurer Sam")["n_entries"] == 7
    # Schaffhausen 2018: Schlussgang loser Schneider lists "o Bless Michael *** 0.00" 7th
    res = parsed(24434)
    (sg,) = bouts_between(res, "Bless Michael", "Schneider Domenic")
    assert "extra_bout" in sg["flags"] and sg["gang_nr"] == 6
    assert result_for(sg, res, "Bless Michael") == ("W", 10.0)
    assert result_for(sg, res, "Schneider Domenic") == ("L", None)


def test_quarter_point_extra_bouts_are_kept() -> None:
    """0.25 lines (e.g. Belfaux 2023 Gapany-Kramer decider) pair with their mirror."""
    res = parsed(22547)
    assert reasons(res) == Counter()
    gk = bouts_between(res, "Gapany Benjamin", "Kramer Lario")
    assert [result_for(b, res, "Gapany Benjamin") for b in gk] == [("L", 8.5), ("W", None)]
    assert ["extra_bout" in b["flags"] for b in gk] == [False, True]
    assert result_for(gk[1], res, "Kramer Lario") == ("L", 8.75)
    (tp,) = bouts_between(res, "Tuscher Esteban", "Perroud Florian")
    assert result_for(tp, res, "Tuscher Esteban") == ("L", None)


def test_extra_bout_vs_genuine_forfeit() -> None:
    """0.00 in a regular Gang stays a forfeit; on a surplus line it is an extra bout."""
    lines = ["1 Alpha Anton 58.00"] + [f"+ Opp{i} Otto 10.00" for i in range(5)] + \
        ["o Beta Bruno 0.00"]                                   # Gang 6: injured
    lines += ["2 Beta Bruno 60.00", "+ Alpha Anton 10.00"] + \
        [f"+ Opp{i} Otto 10.00" for i in range(5)] + ["+ Gamma Gustav 10.00"]
    lines += ["3 Gamma Gustav 50.00"] + [f"o Opp{i} Otto 8.50" for i in range(6)] + \
        ["o Beta Bruno 0.25"]                                   # surplus 7th: extra bout
    for i in range(5):
        lines += [f"{i + 4} Opp{i} Otto 26.00", "o Alpha Anton 8.50", "o Beta Bruno 8.50",
                  "+ Gamma Gustav 9.75"]
    lines += ["9 Opp5 Otto 9.75", "+ Gamma Gustav 9.75"]
    res = bp.build_festival(_sheet(lines), 3)
    assert res.gang_count == 6
    assert reasons(res) == Counter({"forfeit_injury": 1})  # Alpha's 0.00 in Gang 6
    extra = [b for b in res.bouts if "extra_bout" in b["flags"]]
    # Beta's 7th line is graded; Gamma's surplus 0.25 line is the placeholder
    assert [(b["athlete_a_id"], b["athlete_b_id"], b["outcome"], b["grade_a"], b["grade_b"],
             b["gang_nr"]) for b in extra] == [("3-001", "3-002", "WIN_A", 10.0, None, 6)]


def test_missing_grade_line_paired_with_mirror() -> None:
    """Stoos 2013: Schuler's surplus line "0 Laimbacher Philipp S***" has no grade."""
    res = parsed(25977)
    (b,) = bouts_between(res, "Laimbacher Philipp", "Schuler Christian")
    assert "extra_bout" in b["flags"] and b["schlussgang"]
    assert result_for(b, res, "Laimbacher Philipp") == ("W", 10.0)
    assert result_for(b, res, "Schuler Christian") == ("L", None)
    # in a regular Gang a no-grade line with a consistent mirror is a 'grade_missing' bout
    res2 = bp.build_festival(_sheet([
        "1 Alpha Anton 20.00", "+ Beta Bruno 10.00", "+ Gamma Gustav 10.00",
        "2 Beta Bruno 18.50", "o Alpha Anton", "+ Gamma Gustav 10.00",
        "3 Gamma Gustav 17.00", "o Alpha Anton 8.50", "o Beta Bruno 8.50",
    ]), 4)
    (g,) = [x for x in res2.bouts if x["grade_b"] is None]
    assert g["flags"] == "grade_missing" and g["outcome"] == "WIN_A"


def test_wrapped_entry_lines_are_joined() -> None:
    """Kiental 2012: "0 Urfer Simon *" with its grade "8.75" on the next line."""
    res = parsed(26200)
    assert res.status == "ok" and reasons(res) == Counter()
    assert athlete(res, "Urfer Simon")["grade_sum"] == athlete(res, "Urfer Simon")["points"]


def test_block_layout_schlussgang_marker() -> None:
    res = parsed(26414)
    assert sum(b["schlussgang"] is True for b in res.bouts) == 1
    assert {b["schlussgang"] for b in res.bouts} == {True, False}


def test_schlussgang_unknown_without_marker() -> None:
    """Sheets without an explicit marker (all modern ESV sheets): NULL, not False."""
    assert {b["schlussgang"] for b in parsed(24110).bouts} == {None}
    assert {b["schlussgang"] for b in parsed(46055).bouts} == {None}


def test_shared_pdf_is_only_imported_for_its_festival() -> None:
    wrong = parsed(23674)  # Schwarzenberg 2022 points to Oberarth's sheet
    assert wrong.status == "header_mismatch" and wrong.bouts == []
    assert reasons(wrong) == Counter({"header_mismatch": 1})
    assert parsed(23796).status == "ok"


def test_same_name_resolved_by_mirror_entry() -> None:
    res = parsed(24013)  # two "Gisler Silvan" (S and T); opponents print just the name
    gs = [a for a in res.athletes if a["name"] == "Gisler Silvan"]
    assert sorted(a["sennen_turner"] for a in gs) == ["S", "T"]
    assert reasons(res) == Counter({"duplicate_name_in_sheet": 1})  # informational only


def test_notenblatt_layout_assigns_blocks_correctly() -> None:
    res = parsed(25931)  # points(k), entries(k), then "rank name(k) points(k+1)"
    assert res.layout == "notenblatt"
    full = [a for a in res.athletes if a["n_entries"] == 6]
    assert len(full) > 50 and all(a["grade_sum"] == a["points"] for a in full)
    assert athlete(res, "Mahrer Jürg")["rank"] == "1" and athlete(res, "Mahrer Jürg")["points"] == 59.5


def test_glyph_id_sheet_is_decoded() -> None:
    """24038 (2021) embeds a font without Unicode map: PDFium returns glyph ids.
    ASCII decodes exactly (id + 29); its non-ASCII ids are per document -> '?'."""
    res = parsed(24038)
    assert reasons(res)["glyph_ids_decoded_lossy"] == 1
    assert res.status == "ok" and len(res.bouts) == 22
    assert athlete(res, "Streich Sascha")["points"] == 59.75
    assert "Ny?enegger Florian" in {a["name"] for a in res.athletes}  # "ff" glyph unknown


def _encode_mac(s: str) -> str:
    """Inverse of the standard Macintosh glyph order (test helper)."""
    return "".join(c if c in "\n " else chr(ord(c) - 29) if ord(c) < 127
                   else chr(c.encode("mac_roman")[0] - 30) for c in s)


def test_decode_standard_mac_glyph_order() -> None:
    lines = ["Statistische Tabelle", "1", "BöschDaniel", "59.00"] + \
        [f"+ MüllerJosé{i} 9.75" for i in range(12)]
    raw = "\n".join(_encode_mac(ln) for ln in lines)
    assert bp.glyph_id_encoding(raw) == "mac"
    out = bp.decode_glyph_ids(raw).split("\n")
    assert out[2] == "Bösch Daniel" and out[4] == "+ Müller José0 9.75"
    assert bp.glyph_id_encoding("normal text 9.75\n" * 30) is None
    # an unknown glyph order (no grades after decoding) is left alone
    assert bp.glyph_id_encoding("\x01\x02\x03\x04 \x05\x06" * 40) is None


def test_undecodable_sheet_fails_with_reason() -> None:
    res = bp.parse_festival("\x01\x02\x03 \x04\x05" * 40, 1, "2013-04-21", "x")
    assert res.status == "failed" and res.bouts == []


def test_pdf_extraction_matches_text_fixture() -> None:
    for fid in (46055, 45965):
        t = pdf_to_text(sheet_file(f"{fid}.pdf").read_bytes())
        assert bp.normalize_text(t) == bp.normalize_text(text(fid))


def test_parse_from_real_pdf() -> None:
    m = META["45965"]
    res = bp.parse_festival(pdf_to_text(sheet_file("45965.pdf").read_bytes()), 45965,
                            m["date"], m["name"])
    assert len(res.bouts) == 198


def test_frames_have_spec_columns() -> None:
    bouts, athletes, rejects = bp.to_frames([parsed(46055), parsed(24110)])
    assert {"bout_id", "fest_id", "gang_nr", "athlete_a_id", "athlete_b_id", "outcome",
            "grade_a", "grade_b"} <= set(bouts.columns)
    assert bouts["bout_id"].is_unique and len(bouts) == 274 + 909
    assert set(bouts["outcome"]) <= set(bp.OUTCOMES)
    graded = bouts[bouts["grade_a"].notna() & bouts["grade_b"].notna()]
    assert graded["grade_a"].between(8.25, 10).all() and graded["grade_b"].between(8.25, 10).all()
    assert bouts[~bouts.index.isin(graded.index)]["flags"].str.contains("extra_bout").all()
    assert athletes["athlete_raw_id"].is_unique
    assert set(bouts["athlete_a_id"]) | set(bouts["athlete_b_id"]) <= set(athletes["athlete_raw_id"])
    assert list(rejects.columns) == ["fest_id", "stage", "reason", "detail", "line"]


# ------------------------------------------------------------------ units
@pytest.mark.parametrize(("raw", "clean"), [
    ("Stucki Christian ***", "Stucki Christian"),
    ("Remo**", "Remo"),
    ("Gisler Silvan, S", "Gisler Silvan"),
    ("Steiner Chris, S (03)", "Steiner Chris (03)"),
    ("Clopath Beat (Bonaduz) EK", "Clopath Beat (Bonaduz)"),
    ("von Ah Benji T**", "von Ah Benji"),
    ("Glarner Matthias OB", "Glarner Matthias"),
    ("Herger Elias 2", "Herger Elias 2"),
    ("Odermatt Adrian (2001) *", "Odermatt Adrian (2001)"),
])
def test_clean_name(raw: str, clean: str) -> None:
    assert bp.clean_name(raw) == clean


def test_name_keys_and_details() -> None:
    assert bp.name_keys("Odermatt Adrian (2001)") == ("odermatt adrian (2001)", "odermatt adrian")
    assert bp.name_details("Steiner Chris (03)")["birth_year"] == "2003"
    assert bp.name_details("Anderegg Simon (BE)")["association"] == "BE"
    assert bp.name_details("Forrer Arnold, Stein")["place"] == "Stein"


@pytest.mark.parametrize(("line", "year", "expected"), [
    ("Aktive", 2014, "active"), ("Actif", 2012, "active"),
    ("Kat. B; Jg. 99/00", 2014, "youth"), ("Kategorie 2000/01", 2015, "youth"),
    ("Jungschwinger 03-04", 2018, "youth"), ("JS 00/01", 2015, "youth"),
    ("1997", 2012, "youth"), ("1999-2000", 2012, "youth"),
    ("2023", 2023, None),  # wrapped title "…Interlaken" / "2023"
    ("+ Bösch Daniel 10.00", 2016, None),
])
def test_section_of(line: str, year: int, expected: str | None) -> None:
    assert bp.section_of(line, year) == expected


@pytest.mark.parametrize(("header", "date", "expected"), [
    (["Oberarth, 10.04.2022"], "2022-06-16", "mismatch"),
    (["Oberarth, 10.04.2022"], "2022-04-10", "ok"),
    (["Schattdorf, 13. April 2014"], "2014-04-13", "ok"),
    (["Villars-le-Terroir, le 7 juillet 2013"], "2013-07-07", "ok"),
    (["Statistische Tabelle Klewenalp-Schwinget"], "2025-08-02", "ok"),  # name token
    (["Statistische Tabelle"], "2025-08-02", "unverified"),
    # print timestamps are not festival dates (Lueg 2018/2019, Bözingenberg 2015)
    (["Statistische Tabelle nach 6 Gängen", "01.01.2000 - 01:41"], "2019-09-08", "unverified"),
    (["Oberarth, 10.04.2022", "Rigiverband ©2022 Eidgenössischer Schwingerverband "
      "10.04.2022 17:24 Seite 1/3"], "2022-04-10", "ok"),
    (["Thörigen, Reithalle, 10.04.2022", "Herzogenbuchsee ©2022 Eidgenössischer "
      "Schwingerverband 04.09.2022 17:10 Seite 1/2"], "2022-09-04", "mismatch"),
    (["Statistische Tabelle", "15.04.2019"], "2019-04-13", "ok"),  # a few days' tolerance
    # Krummenau 2015: sheet 06.09., schlussgang 13.09. (wrong metadata) stays flagged
    (["Wolzenalp ob Krummenau, 06. September 2015"], "2015-09-13", "mismatch"),
])
def test_verify_header(header: list[str], date: str, expected: str) -> None:
    assert bp.verify_header(header, date, "Klewenalp-Schwinget 2025")[0] == expected


@pytest.mark.parametrize(("sa", "sb", "ga", "gb", "outcome", "flags"), [
    ("+", "o", 10.0, 8.5, "WIN_A", []),
    ("o", "+", 8.75, 9.75, "WIN_B", []),
    ("-", "-", 9.0, 9.0, "DRAW", []),
    ("+", "-", 10.0, 8.5, "WIN_A", ["symbol_conflict_resolved_by_grades"]),
    ("+", "+", 9.0, 9.0, None, []),
])
def test_outcome(sa: str, sb: str, ga: float, gb: float, outcome: str | None,
                 flags: list[str]) -> None:
    assert bp._outcome(sa, sb, ga, gb) == (outcome, flags)


def test_gang_from_complete_list() -> None:
    """A shorter list (missed Gang) under-estimates the Gang; the complete list wins."""
    sheet = bp.parse_sheet("\n".join([
        "1 Alpha Anton 58.00", "+ Beta Bruno 10.00", "+ Gamma Gustav 10.00", "+ Delta Dan 10.00",
        "2 Beta Bruno 26.75", "o Alpha Anton 8.50", "+ Delta Dan 9.25",   # missed Gang 3 is last
        "3 Gamma Gustav 17.75", "o Alpha Anton 8.50", "+ Delta Dan 9.25",  # arrived for Gang 2
        "4 Delta Dan 26.25", "o Gamma Gustav 8.75", "o Beta Bruno 8.75", "o Alpha Anton 8.75",
    ]))
    res = bp.build_festival(sheet, 1)
    g = {(b["athlete_a_id"], b["athlete_b_id"]): b["gang_nr"] for b in res.bouts}
    assert g[("1-000", "1-002")] == 2  # Alpha's complete list says Gang 2 (Gamma's says 1)
    assert "gang_inferred:2/1" in [b for b in res.bouts if b["gang_nr"] == 2
                                   and b["athlete_b_id"] == "1-002"][0]["flags"]


# ------------------------------------------------------------------ validation (task 3)
def _sheet(lines: list[str]) -> bp.Sheet:
    return bp.parse_sheet("\n".join(lines))


def test_grades_outside_range_are_rejected() -> None:
    res = bp.build_festival(_sheet([
        "1 Alpha Anton 20.00", "+ Beta Bruno 10.50", "+ Gamma Gustav 10.00",
        "2 Beta Bruno 8.00", "o Alpha Anton 8.00",
        "3 Gamma Gustav 8.10", "o Alpha Anton 8.10",
    ]), 7)
    assert res.bouts == []
    assert reasons(res)["grade_out_of_range"] == 2


def test_asymmetric_bouts_are_rejected() -> None:
    res = bp.build_festival(_sheet([
        "1 Alpha Anton 20.00", "+ Beta Bruno 10.00", "+ Gamma Gustav 10.00",
        "2 Beta Bruno 10.00", "+ Alpha Anton 10.00",      # both claim the win
        "3 Gamma Gustav 8.50", "o Alpha Anton 8.50",
    ]), 7)
    assert len(res.bouts) == 1
    assert reasons(res)["inconsistent_outcome"] == 1


def test_one_sided_entry_is_rejected() -> None:
    res = bp.build_festival(_sheet([
        "1 Alpha Anton 20.00", "+ Beta Bruno 10.00", "+ Gamma Gustav 10.00",
        "2 Beta Bruno 8.50", "o Alpha Anton 8.50",
        "3 Gamma Gustav 8.50", "o Delta Dan 8.50",         # Gamma never lists Alpha
    ]), 7)
    r = reasons(res)
    assert len(res.bouts) == 1 and r["unmatched_entry"] == 1 and r["opponent_not_found"] == 1


def _round_robin(n: int) -> list[str]:
    """n athletes, everyone fights everyone (n-1 graded entries each)."""
    names = [f"Ath{i} Otto" for i in range(n)]
    lines: list[str] = []
    for i, me in enumerate(names):
        lines.append(f"{i + 1} {me} 50.00")
        for j, opp in enumerate(names):
            if i != j:
                lines.append(f"{'+' if i < j else 'o'} {opp} {'10.00' if i < j else '8.50'}")
    return lines


def test_gang_count_limit() -> None:
    lines = _round_robin(8)  # 7 graded entries per athlete
    res = bp.build_festival(_sheet(lines), 7, max_gang=6)
    assert res.gang_count == 6 and max(b["gang_nr"] for b in res.bouts) == 6
    assert reasons(res)["gang_out_of_range"] > 0
    res8 = bp.build_festival(_sheet(lines), 7, max_gang=8)
    assert res8.gang_count == 7 and len(res8.bouts) == 28 and not res8.rejects


@pytest.mark.parametrize(("lengths", "max_gang", "expected"), [
    ([6] * 40 + [4] * 40 + [7], 6, 6),   # one athlete with an extra bout
    ([5] * 60 + [6] * 2, 6, 5),          # 5-Gang festival (Abendschwinget)
    ([8] * 140 + [6] * 130 + [9], 8, 8), # ESAF
    ([6] * 50 + [10] * 6, 6, 6),         # misparsed lists never exceed the category max
    ([], 6, 6),                          # no evidence -> category max
])
def test_festival_gang_count(lengths: list[int], max_gang: int, expected: int) -> None:
    blocks = [bp.Block("1", f"A{i} B", None, 0,
                       entries=[bp.Entry("+", "X Y", 10.0, 0) for _ in range(n)])
              for i, n in enumerate(lengths)]
    assert bp.festival_gang_count(blocks, max_gang) == expected


@pytest.mark.parametrize(("category", "eidg", "n"), [
    ("ESAF", "ESAF", 8), ("ESAF", "Kilchberg", 6), ("ESAF", "Unspunnen", 6),
    ("Bergkranz", None, 6), ("Regional", None, 6),
])
def test_max_gaenge(category: str, eidg: str | None, n: int) -> None:
    assert bp.max_gaenge(category, eidg) == n


def test_low_pair_rate_withdraws_bouts() -> None:
    sheet = _sheet([
        "1 Alpha Anton 38.50", "+ Beta Bruno 10.00", "o X1 Y 8.50", "o X2 Y 8.50", "o X3 Y 8.50",
        "2 Beta Bruno 8.50", "o Alpha Anton 8.50",
    ])
    res = bp.validate_festival(bp.build_festival(sheet, 7), min_pair_rate=0.5)
    assert res.status == "failed" and res.bouts == []
    assert reasons(res)["low_pair_rate"] == 1
    res2 = bp.validate_festival(bp.build_festival(sheet, 7), min_pair_rate=0.2)
    assert len(res2.bouts) == 1


def test_gang_collision_is_flagged() -> None:
    res = bp.FestivalParse(fest_id=1, layout="standard", status="ok", entries_total=4, bouts=[
        {"athlete_a_id": "a", "athlete_b_id": "b", "gang_nr": 1, "flags": ""},
        {"athlete_a_id": "a", "athlete_b_id": "c", "gang_nr": 1, "flags": "gang_inferred:1/2"},
    ])
    bp.validate_festival(res, min_pair_rate=0.0)
    assert [b["flags"] for b in res.bouts] == ["gang_collision", "gang_inferred:1/2,gang_collision"]


def test_points_mismatch_flag() -> None:
    res = parsed(46055)
    assert not any(a["points_mismatch"] for a in res.athletes)
    bad = bp.parse_festival("\n".join([
        "Statistische Tabelle Test 2025", "01.01.2025",
        "1 Alpha Anton 25.00", "+ Beta Bruno 10.00", "2 Beta Bruno 8.50", "o Alpha Anton 8.50",
    ]), 9, "2025-01-01", "Test 2025")
    assert [a["points_mismatch"] for a in bad.athletes] == [True, False]


# ------------------------------------------------------------------ post-review follow-ups
def _six_gang_sheet(winner_last: str = "+ Loser Lukas 10.00", winner_rank: str = "1",
                    loser_lists_winner: bool = False) -> list[str]:
    """Winner (6 Gänge, last = Schlussgang vs Loser) and Loser (6 Gänge, complete list
    that omits the Schlussgang, as some sheets print it); 10 fillers make it 6-Gang."""
    fill = [f"Fill{i} Otto" for i in range(10)]
    lines = [f"{winner_rank} Winner Willi 59.50"] + [f"+ {f} 10.00" for f in fill[:5]] + \
        [winner_last]
    lines += ["2 Loser Lukas 57.00"] + [f"+ {f} 9.50" for f in fill[5:10]] + \
        (["o Winner Willi 8.75"] if loser_lists_winner else ["+ Fill0 Otto 9.50"])
    for i, f in enumerate(fill):
        opp = ["o Winner Willi 8.50"] if i < 5 else []
        opp += ["o Loser Lukas 8.50"] if i >= 5 or (i == 0 and not loser_lists_winner) else []
        lines += [f"{i + 3} {f} 40.00"] + opp + [f"+ {g} 9.00" for g in fill if g != f][:6 - len(opp)]
    return lines


def test_one_sided_schlussgang_kept_from_winner_entry() -> None:
    """User decision 2026-10-01: the loser's line is omitted entirely -> bout from the
    winner's final-Gang entry, loser grade NULL, flag one_sided."""
    res = bp.build_festival(_sheet(_six_gang_sheet()), 5)
    assert res.gang_count == 6
    (b,) = [x for x in res.bouts if "one_sided" in x["flags"]]
    assert (b["athlete_a_id"], b["athlete_b_id"], b["outcome"], b["grade_a"], b["grade_b"],
            b["gang_nr"]) == ("5-000", "5-001", "WIN_A", 10.0, None, 6)
    entry = sum(1 for r in res.rejects if r.stage == "entry")
    bout = sum(1 for r in res.rejects if r.stage == "bout")
    assert res.entries_total == bp.paired_entries(res) + entry + 2 * bout


@pytest.mark.parametrize("kw", [
    {"winner_rank": "3"},                              # not the festival winner
    {"winner_last": "o Loser Lukas 8.75"},             # not a win
    {"winner_last": "+ Nobody Known 10.00"},           # opponent does not resolve
])
def test_one_sided_rule_stays_narrow(kw: dict[str, str]) -> None:
    res = bp.build_festival(_sheet(_six_gang_sheet(**kw)), 5)
    assert not any("one_sided" in b["flags"] for b in res.bouts)


def test_one_sided_needs_final_gang() -> None:
    lines = _six_gang_sheet()
    lines[1], lines[6] = lines[6], lines[1]  # the unmatched win is now Gang 1
    res = bp.build_festival(_sheet(lines), 5)
    assert not any("one_sided" in b["flags"] for b in res.bouts)
    assert reasons(res)["unmatched_entry"] >= 1


def test_schlussgang_null_when_marked_bout_not_imported() -> None:
    lines = _six_gang_sheet(winner_last="s+ Ghost Gustav 10.00")  # marker, no such athlete
    res = bp.build_festival(_sheet(lines), 5)
    assert res.bouts and {b["schlussgang"] for b in res.bouts} == {None}
    res2 = bp.build_festival(_sheet(_six_gang_sheet(winner_last="s+ Loser Lukas 10.00",
                                                    loser_lists_winner=True)), 5)
    assert sorted(b["schlussgang"] for b in res2.bouts).count(True) == 1


def test_entries_overflow_is_not_an_extra_bout() -> None:
    """Merged blocks (more than Gänge + 1 entries): no extra_bout, athlete flagged."""
    lines = _six_gang_sheet(loser_lists_winner=True)
    i = lines.index("2 Loser Lukas 57.00")
    lines[i:i] = ["+ Fill1 Otto 0.00", "+ Fill2 Otto 9.00"]  # Winner now has 8 entries
    j = lines.index("4 Fill1 Otto 40.00")
    lines.insert(j + 1, "o Winner Willi 8.50")                # mirror of the 0.00 line
    res = bp.build_festival(_sheet(lines), 5)
    assert "entries_overflow" in athlete(res, "Winner Willi")["flags"]
    assert not any("extra_bout" in b["flags"] for b in res.bouts)
    assert reasons(res)["entries_overflow"] == 1


def test_interim_sheet_adds_only_missing_athletes() -> None:
    """ESAF 2013: athletes only on the interim sheet are merged; no bout twice."""
    final = ["Statistische Tabelle", "1 Alpha Anton 20.00", "+ Beta Bruno 10.00",
             "+ Gamma Gustav 10.00", "2 Beta Bruno 18.50", "o Alpha Anton 8.50",
             "+ Gamma Gustav 10.00"]
    interim = ["Statistische Tabelle nach 2 Gängen", "1 Alpha Anton 20.00",
               "+ Beta Bruno 10.00", "+ Gamma Gustav 10.00",
               "3 Gamma Gustav 17.00", "o Alpha Anton 8.50", "o Beta Bruno 8.50"]
    before = bp.parse_festival("\n".join(final), 9, "2013-09-01", "x")
    after = bp.parse_festival("\n".join(final), 9, "2013-09-01", "x",
                              interim_text="\n".join(interim))
    assert (len(before.bouts), len(after.bouts)) == (1, 3)
    assert reasons(after)["interim_sheet_merged"] == 1
    gamma = athlete(after, "Gamma Gustav")
    assert gamma["flags"] == "interim_sheet" and gamma["rank"] is None
    assert len({frozenset((b["athlete_a_id"], b["athlete_b_id"])) for b in after.bouts}) == 3


def test_glued_letter_rank_is_split() -> None:
    assert bp.normalize_text("12zaBieri Marcel * 36.75") == ["12za Bieri Marcel * 36.75"]
    assert bp.normalize_text("+ Bieri Marcel 9.75") == ["+ Bieri Marcel 9.75"]

