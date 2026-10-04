"""Statistic sheets before 2011 (Phase 10): the positional table reader, unlisted
opponents and the hand-set spellings. Offline: fixtures are the first page of real
sheets as PDFium lines (``tests/fixtures/statistic_old/<fest_id>.json``)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from src.db import Festival
from src.scraper import bouts_parser as bp
from src.scraper import parse_runner as pr
from src.scraper.grid_parser import parse_grid, title_case
from src.scraper.pdf_layout import Line, Row, Word, merge_rows

OLD = Path(__file__).parent / "fixtures" / "statistic_old"


def load(fid: int) -> tuple[Festival, str, list[Row]]:
    doc = json.loads((OLD / f"{fid}.json").read_text(encoding="utf-8"))
    m = doc["fest"]
    fest = Festival(fest_id=m["fest_id"], name=m["name"], date=m["date"],
                    category=m["category"], location=None, eidg_type=m["eidg_type"])
    lines = [Line(0, y, tuple(Word(*w) for w in words)) for y, words in doc["lines"]]
    return fest, doc["text"], merge_rows(lines)


def parse(fid: int) -> bp.FestivalParse:
    fest, text, rows = load(fid)
    return pr.parse_old(fest, text, rows, min_pair_rate=0.5)


def listed(res: bp.FestivalParse) -> list[dict[str, Any]]:
    return [a for a in res.athletes if bp.UNLISTED_FLAG not in str(a["flags"]).split(",")]


def one_sided(res: bp.FestivalParse) -> list[dict[str, Any]]:
    return [b for b in res.bouts if "unlisted_opponent" in str(b["flags"]).split(",")]


def athlete(res: bp.FestivalParse, name: str) -> dict[str, Any]:
    hits = [a for a in res.athletes if a["name"] == name]
    assert len(hits) == 1, name
    return hits[0]


def bouts_of(res: bp.FestivalParse, name: str) -> list[dict[str, Any]]:
    me = athlete(res, name)["athlete_raw_id"]
    return sorted((b for b in res.bouts if me in (b["athlete_a_id"], b["athlete_b_id"])),
                  key=lambda b: b["gang_nr"])


# (fest_id, layout, status, printed athletes, name-only athletes, bouts, one-sided,
#  entries, athletes whose total is the sum of their grades)
EXPECTED = [
    (26756, "grid", "ok", 22, 58, 108, 84, 132, 22),        # 2002: header names in a late run
    (26778, "grid", "partial", 10, 37, 52, 45, 60, 10),     # 2001 "Notenblätter", two abreast
    (26775, "grid", "partial", 14, 59, 80, 69, 98, 12),     # ESAF 2001: total first, "O", 8,75
    (26661, "grid", "partial", 24, 93, 161, 131, 192, 24),  # ESAF 2004: independent columns
    (26663, "grid", "ok", 21, 44, 100, 74, 126, 21),        # capitals, "Pl." rows, start numbers
    (26747, "grid", "ok", 24, 45, 107, 70, 144, 24),        # the same without trailing zeros
    (26679, "grid", "partial", 12, 42, 65, 61, 72, 12),     # "+ 10,00 Name", first name first
    (26482, "grid", "ok", 15, 49, 77, 64, 90, 15),          # status first, residence for namesakes
    (26544, "grid", "ok", 30, 78, 149, 118, 180, 30),       # character codes shifted by 29
    (26529, "grid", "ok", 8, 30, 43, 38, 48, 0),            # "1.) Name, Ort": no totals printed
    (26537, "grid", "ok", 12, 42, 64, 56, 72, 12),          # "58 . 25"
    (26657, "grid", "ok", 33, 79, 165, 132, 198, 33),       # total first, three abreast
    (26638, "grid", "ok", 12, 36, 59, 46, 72, 12),          # header year misprinted
]


@pytest.mark.parametrize("fid,layout,status,n_listed,n_unlisted,n_bouts,n_one,entries,good",
                         EXPECTED)
def test_old_sheet_layouts(fid: int, layout: str, status: str, n_listed: int, n_unlisted: int,
                           n_bouts: int, n_one: int, entries: int, good: int) -> None:
    res = parse(fid)
    assert (res.layout, res.status) == (layout, status)
    assert len(listed(res)) == n_listed
    assert len(res.athletes) - n_listed == n_unlisted
    assert (len(res.bouts), len(one_sided(res)), res.entries_total) == (n_bouts, n_one, entries)
    assert bp.consistent_blocks(res)[0] == good
    # every entry is a bout (two-sided: two entries) or a recorded reject
    used = 2 * (n_bouts - n_one) + n_one
    rejected = sum(1 for r in res.rejects if r.stage == "entry") \
        + 2 * sum(1 for r in res.rejects if r.stage == "bout")
    assert used + rejected == entries
    # schema: outcomes, grades, Gang range, ids
    ids = {a["athlete_raw_id"] for a in res.athletes}
    max_gang = 8 if res.gang_count == 8 else 6
    for b in res.bouts:
        assert b["outcome"] in bp.OUTCOMES and 1 <= b["gang_nr"] <= max_gang
        assert b["athlete_a_id"] in ids and b["athlete_b_id"] in ids
        assert b["athlete_a_id"] != b["athlete_b_id"]
        for g in (b["grade_a"], b["grade_b"]):
            assert g is None or 8.25 <= g <= 10.0
    for b in one_sided(res):
        assert "one_sided" in b["flags"] and b["grade_a"] is not None and b["grade_b"] is None
    for a in res.athletes:
        if a not in listed(res):
            assert (a["rank"], a["points"], a["n_entries"]) == (None, None, 0)


def test_header_names_from_a_separate_text_run() -> None:
    """2002: PDFium emits "1 58.50 2 57.50 3a 57.25" and the three names a page later."""
    _fest, text, rows = load(26756)
    assert "1 58.50 2 57.50 3a 57.25" in text
    assert rows[1].text == "1 Huber Matthäus 58.50 2 Zindel Thomas 57.50 3a Forrer Arnold 57.25"
    res = parse(26756)
    first = athlete(res, "Huber Matthäus")
    assert (first["rank"], first["points"], first["n_entries"]) == ("1", 58.5, 6)
    assert [a["rank"] for a in listed(res)][:6] == ["1", "2", "3a", "3b", "3c", "4a"]
    # a bout between two printed athletes is built from both entries
    b = [b for b in bouts_of(res, "Huber Matthäus")
         if athlete(res, "Vogel Christian")["athlete_raw_id"] in (b["athlete_a_id"],
                                                                  b["athlete_b_id"])]
    assert len(b) == 1 and b[0]["outcome"] == "DRAW" and b[0]["flags"] == ""
    assert (b[0]["grade_a"], b[0]["grade_b"]) == (9.0, 9.0) and b[0]["gang_nr"] == 5


def test_unlisted_opponent_is_one_athlete_per_festival() -> None:
    res = parse(26756)
    # "Dick Christian" is not printed; four printed athletes list him
    ph = athlete(res, "Dick Christian")
    assert ph["flags"] == "unlisted"
    mine = bouts_of(res, "Dick Christian")
    assert len(mine) == 4 and all(b["athlete_b_id"] == ph["athlete_raw_id"] for b in mine)
    assert Counter(b["outcome"] for b in mine) == {"WIN_A": 2, "DRAW": 1, "WIN_B": 1}
    assert all(b["flags"] == "one_sided,unlisted_opponent" for b in mine)


def test_residence_after_comma_and_sign_grade_check() -> None:
    res = parse(26778)
    a = athlete(res, "Grab Martin")
    assert (a["rank"], a["points"], a["place"]) == ("1", 58.75, "Rothenthurm")
    # "- Gehrig Roland 10.00": a draw with a winner's grade and nobody to contradict it
    rej = [r for r in res.rejects if r.reason == "inconsistent_outcome"]
    assert len(rej) == 1 and "sign and grade disagree" in rej[0].detail


def test_esaf_2001_total_first_capital_o_decimal_comma() -> None:
    res = parse(26775)
    assert res.gang_count == 8
    a = athlete(res, "Forrer Arnold")
    assert (a["points"], a["sennen_turner"], a["n_entries"]) == (77.25, "S", 8)
    first = bouts_of(res, "Forrer Arnold")[0]
    assert first["gang_nr"] == 1 and first["grade_a"] is not None
    won = first["outcome"] == ("WIN_A" if first["athlete_a_id"] == a["athlete_raw_id"]
                               else "WIN_B")
    assert not won  # "O Klarer Rolf T 8,75"


def test_esaf_2004_columns_are_independent() -> None:
    """Three columns whose blocks have 8, 6 or 4 Gänge: header and Gang cells share rows."""
    res = parse(26661)
    assert res.gang_count == 8
    assert Counter(a["n_entries"] for a in listed(res)) == {8: 24}
    a = athlete(res, "Abderhalden Jörg")
    assert (a["rank"], a["points"], a["status"], a["sennen_turner"]) == ("1", 77.75, "**", "S")
    # printed "d" at the top of the third column: the letter is completed from the
    # numbered rank with the same total (8.a in the second column), not from "6." beside it
    assert athlete(res, "Oesch Christian")["rank"] == "8d"


def test_capitals_sheet_start_numbers_and_pl_rows() -> None:
    res = parse(26663)
    a = athlete(res, "Strebel Stefan")
    assert (a["rank"], a["points"], a["place"], a["status"]) == ("1", 58.0, "Dintikon", "E")
    assert all(x["name"] != x["name"].upper() for x in res.athletes)   # title-cased
    assert not [r for r in res.rejects if r.stage in ("entry", "bout")]


def test_trailing_zeros_dropped() -> None:
    """"1 OESCH CHRISTIAN 58" / "+E 9.75" / "-K 9" / "oE 8.5"."""
    res = parse(26747)
    a = athlete(res, "Oesch Christian")
    assert (a["points"], a["place"], a["association"]) == (58.0, "Kirchberg", "BKSV")
    assert sorted({g for b in res.bouts for g in (b["grade_a"], b["grade_b"]) if g}) == \
        [8.5, 8.75, 9.0, 9.75, 10.0]


def test_grade_before_name_and_first_name_first() -> None:
    res = parse(26679)
    # header "1. Christian Stucki, 58,75 pts"; the Gang cells say "Stucki Christian"
    a = athlete(res, "Stucki Christian")
    assert (a["rank"], a["points"], a["n_entries"]) == ("1", 58.75, 6)
    assert not [x for x in res.athletes if x["name"] == "Christian Stucki"]


def test_status_first_and_residence_for_namesakes() -> None:
    res = parse(26482)
    a = athlete(res, "Stucki Christian")
    assert (a["rank"], a["points"], a["place"], a["sennen_turner"]) == \
        ("1a", 58.75, "Schnottwil", "S")
    # "+ Bürki Christian Eggiwil 10.00" names the athlete printed as
    # "2 a S KK Bürki Christian Eggiwil ET"
    b = athlete(res, "Bürki Christian (Eggiwil)")
    assert (b["rank"], b["place"], b["name_base_key"]) == ("2a", "Eggiwil", "bürki christian")
    both = [x for x in bouts_of(res, "Stucki Christian")
            if b["athlete_raw_id"] in (x["athlete_a_id"], x["athlete_b_id"])]
    assert len(both) == 1 and "one_sided" not in both[0]["flags"]


def test_shifted_character_codes_are_decoded() -> None:
    _fest, text, _rows = load(26544)
    assert text.startswith("pÅÜäìëëê~åÖäáëíÉ")
    decoded = bp.decode_shifted_text(text)
    assert decoded is not None and decoded.startswith("Schlussrangliste")
    assert bp.decode_shifted_text("Statistische Tabelle\n+ Muster Hans 10.00\n" * 30) is None
    res = parse(26544)
    a = athlete(res, "Laimbacher Philipp")
    assert (a["rank"], a["points"], a["status"]) == ("1", 58.75, "**")
    names = {x["name"] for x in res.athletes}
    assert {"Müller Bruno", "Föhn Franz", "Kälin Roland", "Stadelmann René"} <= names
    assert not [n for n in names if "?" in n]               # umlauts survive


def test_sheet_without_totals() -> None:
    res = parse(26529)
    a = athlete(res, "Sempach Thomas")
    assert (a["rank"], a["points"], a["place"], a["n_entries"]) == \
        ("1", None, "Heimenschwand", 6)
    assert athlete(res, "Graber Willy")["rank"] == "4b"      # "4b.)Graber Willy, Bolligen"


def test_numbers_printed_with_spaces() -> None:
    res = parse(26537)
    a = athlete(res, "Stucki Christian")
    assert (a["rank"], a["points"]) == ("1a", 58.25)


def test_header_checks_of_hand_set_sheets() -> None:
    assert parse(26657).header_check.startswith("unverified")   # "Datum: 12.05.2005"
    assert parse(26638).header_check == "ok"                    # "26. Juni 2002" on the 2005 sheet
    v = bp.verify_header
    head = ["78. Berner Jurassisches Schwingfest, 26. Juni 2002 in Tavannes/BE"]
    name = "Bern-Jurassisches Schwingfest Tavannes 2005"
    assert v(head, "2005-06-26", name)[0] == "mismatch"          # 2011+: unchanged
    assert v(head, "2005-06-26", name, hand_set=True) == ("ok", "date (year misprinted) + name")
    assert v(head, "2005-07-03", name, hand_set=True)[0] == "mismatch"
    assert v(head, "2005-06-26", "Seeländisches Schwingfest Ins 2005", hand_set=True)[0] \
        == "mismatch"
    printed = ["Statistik nach 6 Gängen ZH Kantonales Schwingfest 2005",
               "Datum: 12.05.2005 Seite: 1"]
    assert v(printed, "2005-05-05", "Zürcher Kantonalschwingfest Uetikon 2005")[0] == "mismatch"
    assert v(printed, "2005-05-05", "Zürcher Kantonalschwingfest Uetikon 2005",
             hand_set=True)[0] == "unverified"


# ------------------------------------------------------------------ rows
def _line(y: float, *words: tuple[str, float, float]) -> Line:
    return Line(0, y, tuple(Word(*w) for w in words))


def test_merge_rows_joins_runs_of_one_printed_row() -> None:
    lines = [
        _line(700.0, ("1", 50, 54), ("58.50", 176, 200), ("2", 225, 230), ("57.50", 350, 374)),
        _line(688.0, ("+", 50, 55), ("Muster", 60, 90), ("Hans", 93, 115), ("10.00", 176, 200)),
        _line(698.0, ("Beispiel", 60, 95), ("Urs", 98, 112), ("Probe", 234, 261)),  # descender
    ]
    rows = merge_rows(lines)
    assert [r.text for r in rows] == ["1 Beispiel Urs 58.50 2 Probe 57.50",
                                      "+ Muster Hans 10.00"]


def test_merge_rows_keeps_overprinted_runs_apart() -> None:
    """Two runs printed on top of each other (same y, same x) are two rows, in text order."""
    lines = [
        _line(700.0, ("1.", 50, 58), ("Muster", 62, 95), ("Hans", 98, 120), ("58.25", 176, 200)),
        _line(700.0, ("+", 50, 55), ("Beispiel", 62, 100), ("Urs", 103, 118), ("10.00", 176, 200)),
    ]
    assert [r.text for r in merge_rows(lines)] == ["1. Muster Hans 58.25",
                                                   "+ Beispiel Urs 10.00"]


def test_merge_rows_is_per_page() -> None:
    a = Line(0, 700.0, (Word("a", 10, 20),))
    b = Line(1, 700.0, (Word("b", 30, 40),))
    assert [(r.page, r.text) for r in merge_rows([a, b])] == [(0, "a"), (1, "b")]


def _rows(*texts: str) -> list[Row]:
    """Rows from plain strings; a word's x is its character offset * 6, so equal columns
    in the test strings are equal columns on the page."""
    out = []
    for n, text in enumerate(texts):
        words, pos = [], 0
        for tok in text.split():
            pos = text.index(tok, pos)
            words.append(Word(tok, pos * 6.0, (pos + len(tok)) * 6.0))
            pos += len(tok)
        out.append(Row(0, 800.0 - 12 * n, tuple(words)))
    return out


def test_gang_cells_go_to_the_column_above_them() -> None:
    sheet = parse_grid(_rows(
        "Testfest, 1. Juni 2005 Statistische Tabelle",
        "1 Muster Hans 58.50              2 Beispiel Urs 57.00",
        "+ Probe Karl 10.00               o Muster Hans 8.50",
        "+ Beispiel Urs 10.00",                       # the second column has a hole here
        "+ Zeuge Max 9.75                 + Zeuge Max 9.75",
    ))
    assert sheet.header == ["Testfest, 1. Juni 2005 Statistische Tabelle"]
    assert [(b.rank, b.name_raw, b.points, len(b.entries)) for b in sheet.blocks] == \
        [("1", "Muster Hans", 58.5, 3), ("2", "Beispiel Urs", 57.0, 2)]
    assert [e.opponent for e in sheet.blocks[1].entries] == ["Muster Hans", "Zeuge Max"]


def test_one_athlete_per_row_with_gaenge_abreast() -> None:
    sheet = parse_grid(_rows(
        "1 Muster Hans ** Ort S 59.75 K",
        "  + Probe Karl 10.00   + Zeuge Max 9.75   + Beispiel Urs 10.00",
        "  + Vierter Tom 10.00  + Probe Karl 10.00  + Zeuge Max 10.00",
        "2 a Beispiel Urs * Dorf S 57.25 K",
        "  o Muster Hans 8.50   + Zeuge Max 10.00  + Probe Karl 10.00",
    ))
    first, second = sheet.blocks
    assert (first.name_raw, first.place, first.points, len(first.entries)) == \
        ("Muster Hans S**", "Ort", 59.75, 6)
    assert (second.rank, second.name_raw, second.place, len(second.entries)) == \
        ("2a", "Beispiel Urs S*", "Dorf", 3)


def test_cut_off_header_takes_the_opponents_spelling() -> None:
    sheet = parse_grid(_rows(
        "1 Langername Hans-Pete** 58.50   2 Beispiel Urs Dorfikon 57.00",
        "+ Beispiel Urs 10.00             o Langername Hans-Peter 8.50",
    ))
    assert [bp.clean_name(b.name_raw) for b in sheet.blocks] == \
        ["Langername Hans-Peter", "Beispiel Urs"]
    assert sheet.blocks[1].place == "Dorfikon"
    assert bp.status_of(sheet.blocks[0].name_raw) == "**"


def test_title_case() -> None:
    assert title_case("VON AH BENJI") == "von Ah Benji"
    assert title_case("PORTMANN JEAN-CLAUDE") == "Portmann Jean-Claude"
    assert title_case("Auf der Maur Armin") == "Auf der Maur Armin"


# ------------------------------------------------------------------ pairing rules
def _sheet(*blocks: tuple[str, str, float | None, list[tuple[str, str, float]]]) -> bp.Sheet:
    out = []
    for n, (rank, name, points, entries) in enumerate(blocks):
        b = bp.Block(rank, name, points, n)
        b.entries = [bp.Entry(sym, opp, grade, 10 * n + k)
                     for k, (sym, opp, grade) in enumerate(entries)]
        out.append(b)
    return bp.Sheet(layout="grid", header=[], blocks=out)


def _build(sheet: bp.Sheet, **kw: Any) -> bp.FestivalParse:
    return bp.build_festival(sheet, 99, **kw)


SIX = [("+", "Probe Karl", 10.0), ("+", "Zeuge Max", 9.75), ("+", "Vierter Tom", 10.0),
       ("+", "Fünfter Jan", 10.0), ("+", "Sechster Leo", 10.0)]


def test_unlisted_is_off_by_default() -> None:
    sheet = _sheet(("1", "Muster Hans", 58.5, [("o", "Beispiel Urs", 8.75)] + SIX))
    res = _build(sheet)
    assert not res.bouts and Counter(r.reason for r in res.rejects)["opponent_not_found"] == 6
    assert len(res.athletes) == 1


def test_unlisted_opponents_become_one_sided_bouts() -> None:
    sheet = _sheet(("1", "Muster Hans", 58.5, [("o", "Beispiel Urs", 8.75)] + SIX),
                   ("2", "Zweiter Rolf", 58.5, [("-", "Beispiel Urs", 8.75)] + SIX))
    res = _build(sheet, unlisted=True)
    assert len(res.bouts) == 12 and len(res.athletes) == 2 + 6
    urs = athlete(res, "Beispiel Urs")
    assert urs["flags"] == "unlisted" and urs["athlete_raw_id"] == "99-002"
    got = {(b["athlete_a_id"], b["outcome"], b["grade_a"], b["grade_b"], b["gang_nr"])
           for b in res.bouts if b["athlete_b_id"] == urs["athlete_raw_id"]}
    assert got == {("99-000", "WIN_B", 8.75, None, 1), ("99-001", "DRAW", 8.75, None, 1)}
    assert len({b["bout_id"] for b in res.bouts}) == 12


def test_other_spelling_pairs_only_with_a_mirror_entry() -> None:
    sheet = _sheet(
        ("1", "Pellet Hans-Peter", None, [("+", "Halbheer Urs", 10.0), ("+", "Dritter Tom", 10.0)]),
        ("2", "Halbheer Urs", None, [("o", "Pellet Hanspeter", 8.5)]),
        ("3", "Dritter Tom", None, [("+", "Habheer Urs", 10.0)]),  # Halbheer does not list him
    )
    res = _build(sheet, unlisted=True)
    two_sided = [b for b in res.bouts if "one_sided" not in b["flags"]]
    assert [(b["athlete_a_id"], b["athlete_b_id"], b["outcome"]) for b in two_sided] == \
        [("99-000", "99-001", "WIN_A")]
    # no name-only athlete for a spelling of a printed one
    assert len(res.athletes) == 3
    reasons = Counter(r.reason for r in res.rejects)
    assert reasons["unmatched_entry"] == 2    # Pellet -> Dritter (no mirror), Dritter -> Habheer


def test_sign_and_grade_must_agree_without_a_mirror() -> None:
    sheet = _sheet(("1", "Muster Hans", None, [("-", "Beispiel Urs", 10.0),
                                               ("+", "Probe Karl", 8.5),
                                               ("o", "Zeuge Max", 9.75),
                                               ("+", "Vierter Tom", 9.75)]))
    res = _build(sheet, unlisted=True)
    assert len(res.bouts) == 1
    assert Counter(r.reason for r in res.rejects)["inconsistent_outcome"] == 3


def test_unsound_block_gives_no_one_sided_bouts() -> None:
    """A total that is not the sum of the grades: the Gänge may be another athlete's."""
    sheet = _sheet(("1", "Muster Hans", 57.0, [("o", "Beispiel Urs", 8.75)] + SIX),
                   ("2", "Beispiel Urs", 49.5, [("+", "Muster Hans", 9.75)] + SIX[1:]))
    res = _build(sheet, unlisted=True)
    reasons = Counter(r.reason for r in res.rejects)
    assert reasons["block_unsound"] == 5
    assert [b["flags"] for b in res.bouts if "one_sided" not in b["flags"]] == [""]
    assert all(b["athlete_a_id"] == "99-001" for b in res.bouts if "one_sided" in b["flags"])


def test_structure_guard_withdraws_a_misread_sheet() -> None:
    names = ["Erster Anton", "Zweiter Bruno", "Dritter Carlo", "Vierter Daniel"]
    blocks = [(str(n + 1), name, 10.0 if n == 0 else 50.0,
               [("+" if n < 2 else "o", names[n ^ 1], 10.0 if n < 2 else 8.5)])
              for n, name in enumerate(names)]
    blocks[0][3][0] = ("+", "Zweiter Bruno", 10.0)
    blocks[1][3][0] = ("o", "Erster Anton", 8.5)
    res = bp.festival_from_sheet(_sheet(*blocks), 99, "2005-06-01", "Testfest", unlisted=True)
    assert res.status == "failed" and not res.bouts
    assert res.rejects[-1].reason == "structure_unreliable"


def test_similar_names() -> None:
    s = bp.similar_names
    assert s("Pellet Hans-Peter", "Pellet Hanspeter")
    assert s("Edi Philipp", "Philipp Edi")
    assert s("Pellet Hans-Pete", "Pellet Hans-Peter")
    assert s("Habheer Urs", "Halbheer Urs") and not s("Habheer Urs", "Halbheer Urs", 0)
    assert s("Bieri Ueil", "Bieri Ueli")
    assert not s("Sempach Thomas", "Sempach Matthias")
    assert not s("Suter Heinz", "Suter Peter")
    assert not s("Abt Urs", "Abt Uli")          # too short for letter tolerance
    assert bp.squash_name("von Ah Benji") == "vonahbenji"


def test_parse_quality_prefers_the_better_reading() -> None:
    fest, text, rows = load(26756)
    by_text = bp.parse_festival(text, fest.fest_id, fest.date, fest.name, unlisted=True)
    by_grid = bp.festival_from_sheet(parse_grid(rows, fest.year), fest.fest_id, fest.date,
                                     fest.name, unlisted=True)
    assert by_text.status == "failed" and by_grid.status == "ok"
    assert pr.parse_quality(by_grid) > pr.parse_quality(by_text)


def test_sheets_from_2011_on_never_use_the_old_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    fest = Festival(fest_id=1, name="Testfest 2011", date="2011-06-01", category="Kantonal",
                    location=None)
    calls: list[str] = []
    monkeypatch.setattr(pr, "pdf_to_text", lambda content: "Testfest 1. Juni 2011\n")
    monkeypatch.setattr(pr, "pdf_rows", lambda content: calls.append("rows") or [])
    monkeypatch.setattr(pr, "parse_old", lambda *a, **k: calls.append("old"))
    res = pr.parse_pdf(fest, b"", min_pair_rate=0.5)
    assert calls == [] and res.status == "failed"
    old = Festival(fest_id=2, name="Testfest 2010", date="2010-06-01", category="Kantonal",
                   location=None)
    pr.parse_pdf(old, b"", min_pair_rate=0.5)
    assert calls == ["rows", "old"]


def test_table_reading_is_kept_on_a_near_tie() -> None:
    better = pr.text_reads_better
    assert not better((145, 824, 414), (142, 820, 414))   # about the same: keep the table
    assert better((145, 824, 414), (120, 700, 380))       # clearly more proven athletes
    assert better((142, 880, 440), (142, 820, 414))       # clearly more two-sided entries
    assert better((10, 40, 20), (0, 0, 0))                # the table reading found nothing
    assert better((0, 0, 0), (0, 0, 0))                   # both failed: the text diagnosis


def test_two_digit_birth_year_suffix() -> None:
    sheet = parse_grid(_rows(
        "1 Muster Hans S Dorf ML 58.50",
        "+ Jung Peter 91 10.00",
        "2 Jung Peter 91 S Ort ML 57.00",
        "o Muster Hans 8.50",
    ))
    assert [b.name_raw for b in sheet.blocks] == ["Muster Hans, S", "Jung Peter (91), S"]
    assert sheet.blocks[0].entries[0].opponent == "Jung Peter (91)"
    res = _build(sheet, unlisted=True)
    young = athlete(res, "Jung Peter (91)")
    assert (young["birth_year"], young["place"], young["association"]) == ("1991", "Ort", "ML")
    assert len(res.bouts) == 1 and res.bouts[0]["flags"] == ""
