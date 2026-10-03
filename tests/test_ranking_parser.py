"""Schlussrangliste parser against real schlussgang PDFs / positioned-line excerpts
(tests/fixtures/ranking/). Offline only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.scraper.pdf_layout import Line, Word, pdf_lines, split_cells
from src.scraper.ranking_parser import (RankingEntry, RankingParse, Reject, _split_name,
                                        _strip_residence_tail, _strip_status, normalise_result,
                                        parse_ranking_lines, parse_ranking_pdf)
from src.scraper.ranking_runner import lines_from_json
from tests.fixture_paths import ranking_file


def pdf(fest_id: int, max_gaenge: int = 6) -> RankingParse:
    return parse_ranking_pdf(ranking_file(f"{fest_id}.pdf").read_bytes(), max_gaenge=max_gaenge)


def lines(fest_id: int) -> list[Line]:
    return lines_from_json(ranking_file(f"lines_{fest_id}.json").read_text(encoding="utf-8"))


def by_name(res: RankingParse, name: str) -> RankingEntry:
    hits = [e for e in res.entries if e.name == name]
    assert len(hits) == 1, [e.name for e in res.entries]
    return hits[0]


# ----------------------------------------------------------------- real PDFs
def test_esv_2026_without_codes() -> None:
    """Kilchberg 2026: final ESV list, no cantonal code column, no status."""
    res = pdf(46147)
    assert res.layout == "esv"
    assert len(res.entries) == 60 and res.rejects == []
    e = res.entries[0]
    assert (e.rank, e.points, e.result_str, e.name, e.sennen_turner, e.stars, e.residence,
            e.assoc_code, e.club_raw) == ("1", 58.5, "++o+++", "Giger Samuel", "S", "***",
                                         "Märstetten", None, "Ottenberg")
    assert by_name(res, "Bieri Marcel").sennen_turner == "T"
    assert by_name(res, "Kramer Lario").club_raw == "Kerzers/Chiètres"
    assert [e.rank for e in res.entries[3:6]] == ["4a", "4b", "4c"]
    # withdrawn athletes: short result string, low points, still listed
    last = res.entries[-1]
    assert (last.rank, last.points, last.result_str) == ("26", 25.5, "ooo")


def test_esv_2021_with_codes_schlussgang_and_injuries() -> None:
    """Charrat 2021: code column (VD/FR/...), 'S' Schlussgang marker glued to the
    points ('58.50S+++-++'), injured athletes with status 'Unfall'."""
    res = pdf(23846)
    assert res.layout == "esv" and len(res.entries) == 77 and res.rejects == []
    first = res.entries[0]
    assert (first.points, first.schlussgang, first.result_str) == (58.5, True, "+++-++")
    assert (first.residence, first.assoc_code, first.club_raw, first.status) == (
        "Ollon VD", "VD", "Aigle", "Kranz")
    assert by_name(res, "Gottofrey Marc").schlussgang
    assert sum(e.schlussgang for e in res.entries) == 2
    hofer = by_name(res, "Hofer Sven")
    assert (hofer.assoc_code, hofer.club_raw) == ("FR", "Kerzers/Chiètres")
    injured = by_name(res, "Ambresin Cyril")
    assert (injured.points, injured.result_str, injured.status) == (9.0, "-", "Unfall")


def test_ligatures_are_not_split() -> None:
    """'fl' / 'ff' ligature glyphs share one box: no gap inside the word."""
    res = pdf(23846)
    names = {e.name for e in res.entries}
    assert "Schläfli Hugo" in names and "Baeriswyl Christoph" in names
    assert by_name(res, "Baeriswyl Christoph").residence == "Plaffeien"


def test_plain_layout_with_merged_rows() -> None:
    """Riaz 2013: no header; PDFium joins a row that ends with '-' (a gestellter
    last Gang, read as a hyphenation mark U+FFFE) with the next row - they are split
    again (x jumps back to the left, different baseline) and the '-' is kept."""
    res = pdf(25912)
    assert res.layout == "plain"
    assert len(res.entries) == 58 and res.rejects == []
    assert [e.rank for e in res.entries[12:19]] == ["6a", "6b", "6c", "6d", "6e", "6f", "6g"]
    assert by_name(res, "Schmid Köbi").result_str == "++-+o-"
    assert by_name(res, "Schelbert Michael").residence == "Am Mythen"
    assert res.entries[0].schlussgang


def test_other_header_layout() -> None:
    """Niklausschwinget Pratteln 2012: 'Rang Name Wohnort Verband Klub Total Resultate'."""
    res = pdf(26115)
    assert res.layout == "header" and len(res.entries) == 21
    e = res.entries[0]
    assert (e.name, e.residence, e.assoc_code, e.club_raw, e.points, e.result_str) == (
        "Huber Cédric", "Pratteln", "BL", "Pratteln", 58.5, "+++++o")


# ----------------------------------------------------------------- line excerpts
def test_esaf_codes_glued_to_club_and_lone_result() -> None:
    res = parse_ranking_lines(lines(24110), max_gaenge=8)
    assert res.layout == "esv"
    stucki = res.entries[0]
    assert (stucki.rank, stucki.points, stucki.result_str, stucki.schlussgang) == (
        "1a", 77.5, "++++--++", True)
    assert (stucki.assoc_code, stucki.club_raw) == ("SL", "Unteres Seeland")
    mathis = by_name(res, "Mathis Marcel")  # 'ONWNidwalden' printed without a gap
    assert (mathis.residence, mathis.assoc_code, mathis.club_raw) == ("Büren NW", "ONW", "Nidwalden")
    brodard = by_name(res, "Brodard Augustin")  # injured after one loss: result 'o'
    assert (brodard.rank, brodard.points, brodard.result_str, brodard.status) == (
        "46", 8.5, "o", "Unfall")


def test_bernese_list_birth_year_club_number_gau() -> None:
    res = parse_ranking_lines(lines(26403))
    e = res.entries[0]
    assert (e.rank, e.points, e.schlussgang, e.name, e.birth_year, e.residence, e.club_nr,
            e.assoc_code, e.result_str) == ("1", 59.5, True, "Sempach Matthias", 1986,
                                            "Alchenstorf", 181, "OA", "++++++")
    guest = by_name(res, "von Ah Benji")  # guest: no birth year, code GA
    assert (guest.rank, guest.birth_year, guest.assoc_code, guest.club_nr) == ("5a", None, "GA", 4)
    assert by_name(res, "Glarner Matthias").rank == "5b"


def test_two_column_page() -> None:
    res = parse_ranking_lines(lines(25106))
    strebel = by_name(res, "Strebel Joel")
    assert (strebel.rank, strebel.points, strebel.result_str, strebel.assoc_code) == (
        "24", 25.5, "ooo", "NWSV")
    left = by_name(res, "Schuler Christian")
    assert (left.residence, left.assoc_code, left.result_str) == ("Rothenthurm", "SZ", "o+++++")
    assert by_name(res, "Gassmann Fabian").points == 8.75
    assert res.rejects == []


def test_stars_in_separate_cell_and_continuation_rank() -> None:
    res = parse_ranking_lines(lines(26247))
    gisler = by_name(res, "Gisler Bruno")  # 'Gisler Bruno' | 'S***'
    assert (gisler.sennen_turner, gisler.stars, gisler.residence, gisler.assoc_code) == (
        "S", "***", "Rumisberg", "NWS")
    schuler = by_name(res, "Schuler Christian")  # '2. b' + '*' marker, inherits points
    assert (schuler.rank, schuler.points) == ("2b", 57.5)
    assert res.entries[0].stars == "****"


def test_residence_after_comma_in_name_cell() -> None:
    res = parse_ranking_lines(lines(25321))
    e = res.entries[0]
    assert (e.name, e.sennen_turner, e.stars, e.residence, e.assoc_code) == (
        "Räbmatter Patrick", "S", "**", "Uerkheim", "AG")
    assert by_name(res, "Bieri Christoph").rank == "2b"
    assert by_name(res, "Bieri Christoph").points == 58.0


def test_points_printed_with_a_gap() -> None:
    res = parse_ranking_lines(lines(21056))
    althaus = by_name(res, "Althaus Thiben")  # '34 1 7.00 oo'
    assert (althaus.rank, althaus.points, althaus.result_str) == ("34", 17.0, "oo")


# ----------------------------------------------------------------- helpers
@pytest.mark.parametrize("raw, expected", [
    ("Wicki Joel, S **", ("Wicki Joel", "S", "**", None)),
    ("Gisler Bruno S***", ("Gisler Bruno", "S", "***", None)),
    ("Döbeli Lukas (2000), S *", ("Döbeli Lukas", "S", "*", 2000)),
    ("Bruhin Fredi, T", ("Bruhin Fredi", "T", None, None)),
    ("Studer Benno **", ("Studer Benno", None, "**", None)),
    ("Clopath Beat (EK)", ("Clopath Beat", None, None, None)),
    ("von Ah Benji T**", ("von Ah Benji", "T", "**", None)),
])
def test_split_name(raw: str, expected: tuple[object, ...]) -> None:
    assert _split_name(raw) == expected


def test_normalise_result() -> None:
    assert normalise_result("0+O-+o") == "o+o-+o"


@pytest.mark.parametrize("club, expected", [
    ("Zofingen m.Kranz", ("Zofingen", "m.Kranz")),
    ("Dorneck-Thierstein-LaufentalKranz", ("Dorneck-Thierstein-Laufental", "Kranz")),
    ("nicht im Ausstich", ("", "nicht im Ausstich")),
    ("1. Teilverbandskranz", ("", "1. Teilverbandskranz")),
    ("Kranzberg", ("Kranzberg", None)),
    ("Entlebuch", ("Entlebuch", None)),
])
def test_strip_status(club: str, expected: tuple[str, str | None]) -> None:
    assert _strip_status(club) == expected


def test_strip_residence_tail() -> None:
    assert _strip_residence_tail("Stein S EK Kranz") == ("Stein", "S", "EK Kranz")
    assert _strip_residence_tail("Wangen SZ") == ("Wangen SZ", None, None)


def _line(y: float, *words: tuple[str, float]) -> Line:
    return Line(0, y, tuple(Word(t, x, x + 6 * len(t)) for t, x in words))


def test_repeated_pages_are_deduplicated_and_counted() -> None:
    row = [("1", 40), ("58.50", 80), ("+++-++", 120), ("Wicki", 160), ("Joel,", 190),
           ("S", 222), ("**", 232), ("Sörenberg", 300)]
    res = parse_ranking_lines([_line(700, *row), _line(500, *row)])
    assert len(res.entries) == 1
    assert res.rejects == [Reject("repeated_rows_removed", "1 identical rows")]


def test_data_looking_line_without_name_is_rejected() -> None:
    res = parse_ranking_lines([_line(700, ("3", 40), ("57.00", 80), ("+++-++", 120))])
    assert res.entries == []
    assert [r.reason for r in res.rejects] == ["no_name"]


def test_pdf_lines_and_cells() -> None:
    ls = pdf_lines(ranking_file("46147.pdf").read_bytes())
    header = next(ln for ln in ls if "Wohnort" in ln.text)
    cells = [" ".join(w.text for w in c) for c in split_cells(header.words, 5)]
    assert any("Wohnort" in c for c in cells) and any("Schwingklub" in c for c in cells)


# ----------------------------------------------------------------- v2 layouts
def test_bernese_plain_surname_and_first_name_columns() -> None:
    """Hallenschwinget Oberdiessbach 2012: surname | first name | Kranz letters |
    residence | 'SK club' | points | spaced results | 'S'; guests as 'Gast VD';
    points wrapped as '57.0' + '0'."""
    res = parse_ranking_lines(lines(26372))
    assert res.layout == "plain" and len(res.entries) == 9 and res.rejects == []
    e = res.entries[0]
    assert (e.rank, e.points, e.result_str, e.schlussgang, e.name, e.residence, e.club_raw,
            e.status) == ("1a", 59.0, "-+++++", True, "Wenger Kilian", "Thun",
                          "SK Niedersimmental", "EK")
    guest = by_name(res, "Gottofrey Marc")
    assert (guest.residence, guest.assoc_code, guest.club_raw) == ("Waadtland", "VD", None)
    wrapped = by_name(res, "Marti Stefan")
    assert (wrapped.points, wrapped.result_str, wrapped.schlussgang, wrapped.club_raw) == (
        57.0, "-++++o", True, "SK Schwarzenburg")


def test_nwsv_header_layout_rows_emitted_in_two_fragments() -> None:
    """Aargauer Kantonales 2011: PDFium emits 'rank name residence club' and
    'Verband total results status' of one row separately; pdf_layout merges them."""
    res = parse_ranking_lines(lines(26416))
    assert res.layout == "header" and len(res.entries) == 7 and res.rejects == []
    e = res.entries[0]
    assert (e.name, e.stars, e.residence, e.assoc_code, e.club_raw, e.points, e.result_str,
            e.status) == ("Thürig Mario", "***", "Möriken", "AG", "Lenzburg", 58.25, "+++++-",
                          "m.Kranz")
    assert by_name(res, "Clopath Beat").assoc_code == "GST"  # guest from another Teilverband


@pytest.mark.parametrize("club, expected", [
    ("Genevoise Accident", ("Genevoise", "Accident")),
    ("Montagnes Neuchâtel 0 Accident", ("Montagnes Neuchâtel", "0 Accident")),
    ("Haute-Sarine nouveau curonne", ("Haute-Sarine", "nouveau curonne")),
    ("LenzburgAuszeichnung", ("Lenzburg", "Auszeichnung")),
    ("Unfall 5. Gang", ("", "Unfall 5. Gang")),
])
def test_strip_status_french_and_award_phrases(club: str, expected: tuple[str, str]) -> None:
    assert _strip_status(club) == expected


class _FakeTextPage:
    """Minimal PdfTextPage: characters with (left, bottom, right, top) boxes; loose
    boxes share one bottom per font (here: per fragment)."""

    def __init__(self, chars: list[tuple[str, float, float, float]]) -> None:
        self.chars = chars  # (char, left, tight bottom, loose bottom)

    def count_chars(self) -> int:
        return len(self.chars)

    def get_text_range(self, index: int, count: int) -> str:
        return "".join(c[0] for c in self.chars[index:index + count])

    def get_charbox(self, i: int, loose: bool = False) -> tuple[float, float, float, float]:
        ch, left, tight, loose_bottom = self.chars[i]
        return (left, loose_bottom if loose else tight, left + 5.0, tight + 8.0)


def _chars(text: str, x: float, tight: float, loose: float) -> list[tuple[str, float, float, float]]:
    return [(ch, x + 6.0 * k, tight, loose) for k, ch in enumerate(text)]


def test_page_lines_merges_same_baseline_fragments_and_splits_other_rows() -> None:
    from src.scraper.pdf_layout import _page_lines

    # one printed row emitted as two fragments (x jumps back), slightly different fonts
    row = _chars("1 Meier", 40, 700.0, 698.0) + _chars(" AG", 300, 700.5, 696.5) \
        + _chars(" Hans", 100, 700.0, 698.0)
    # a row ending in '-' (PDFium: U+FFFE) glued to the next printed row, 12 pt lower
    glued = _chars("a Suter +o￾", 40, 600.0, 598.0) + _chars(" b Kern ++o", 40, 588.0, 586.0)
    page = _FakeTextPage(row + [("\n", 0, 0, 0)] + glued)
    got = [ln.text for ln in _page_lines(page, 0)]  # type: ignore[arg-type]
    assert got == ["1 Meier Hans AG", "a Suter +o-", "b Kern ++o"]


def test_plain_cells_birth_year_remark_and_code_after_residence() -> None:
    def line(*cells: tuple[str, float]) -> Line:
        return Line(0, 700.0, tuple(Word(t, x, x + 5.5 * len(t)) for t, x in cells))

    res = parse_ranking_lines([
        line(("3", 39), ("a", 51), ("Gottofrey", 63), ("Marc", 138), ("K", 204), ("/", 213),
             ("Jg.", 219), ("95Waadtland", 235), ("Gast", 330), ("VD", 354), ("57.25", 439),
             ("-++o++", 471)),
        line(("4", 39), ("b", 51), ("Wittwer", 63), ("Josias", 138), ("Reichenbach", 240),
             ("BKSV", 305), ("57.00", 439), ("++-+-+", 471)),
        line(("5", 39), ("Mollet", 63), ("Ivan", 138), ("Ollon", 240), ("VD", 272),
             ("56.75", 439), ("+o++-+", 471)),
    ])
    a, b, c = res.entries
    assert (a.birth_year, a.residence, a.assoc_code, a.club_raw, a.status) == (
        1995, "Waadtland", "VD", None, "K")
    assert (b.residence, b.assoc_code) == ("Reichenbach", "BKSV")
    assert (c.residence, c.assoc_code) == ("Ollon VD", None)  # canton suffix of the place
