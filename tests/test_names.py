"""Tests for src/pipeline/names.py (pure name cleaning / matching helpers).

The raw strings in tests/fixtures/names/raw_names.json are real `athletes_raw.name_raw`
values from the statistic sheets (one per problem class)."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from src.pipeline.names import (
    NameIndex, clean_raw_name, compact_key, edit_distance, first_name_canonical,
    fuzzy_candidates, has_wildcard, name_key, name_similarity, wildcard_match,
)

FIXTURE = Path(__file__).parent / "fixtures" / "names" / "raw_names.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _extras(ex) -> dict:
    return {k: v for k, v in dataclasses.asdict(ex).items() if v not in (None, 0, [])}


# ------------------------------------------------------------------ clean_raw_name
@pytest.mark.parametrize("case", CASES, ids=[c["raw"][:40] for c in CASES])
def test_clean_raw_name_fixture(case):
    clean, ex = clean_raw_name(case["raw"], case["fest_year"])
    assert clean == case["clean"]
    assert _extras(ex) == case["extras"]


@pytest.mark.parametrize("case", CASES, ids=[c["raw"][:40] for c in CASES])
def test_clean_raw_name_idempotent(case):
    clean, _ = clean_raw_name(case["raw"], case["fest_year"])
    assert clean_raw_name(clean, case["fest_year"])[0] == clean


def test_fixture_covers_problem_classes():
    flags = {f for c in CASES for f in c["extras"].get("flags", [])}
    assert {"wildcard", "truncated", "mojibake", "glued_bout_line", "title_prefix",
            "rank_prefix", "glued_words", "surname_only", "not_a_name",
            "implausible_birth_year"} <= flags
    keys = {k for c in CASES for k in c["extras"]}
    assert {"birth_year", "suffix", "ordinal", "stars", "status", "st_marker",
            "association", "code", "club", "place", "generation", "number"} <= keys


def test_birth_year_forms():
    assert clean_raw_name("Muster Hans (2004)")[1].birth_year == 2004
    assert clean_raw_name("Muster Hans (04)", 2022)[1].birth_year == 2004
    assert clean_raw_name("Muster Hans (98)", 2022)[1].birth_year == 1998
    assert clean_raw_name("Muster Hans, 90 (ISV) *", 2011)[1].birth_year == 1990
    # bare trailing two-digit number: only a birth year when the age is plausible
    assert clean_raw_name("Muster Hans 97", 2013)[1].birth_year == 1997
    clean, ex = clean_raw_name("Muster Hans 10", 2013)
    assert clean == "Muster Hans" and ex.birth_year is None and ex.number == 10


def test_suffix_is_not_part_of_the_name():
    a, ea = clean_raw_name("Gasser Dominik 1")
    b, eb = clean_raw_name("Gasser Dominik 2 *")
    assert a == b == "Gasser Dominik"
    assert (ea.suffix, eb.suffix) == (1, 2)
    assert eb.stars == 1


def test_stars_never_change_the_name():
    assert clean_raw_name("Glarner Matthias S**")[0] == clean_raw_name("Glarner Matthias S***")[0]
    assert clean_raw_name("Glarner Matthias, S ***")[0] == "Glarner Matthias"


def test_french_berger_gymnaste_markers():
    assert clean_raw_name("Moser Steven, B **")[1].st_marker == "S"
    assert clean_raw_name("Sturny Nicolas, G (98)")[1].st_marker == "T"


def test_empty_and_garbage():
    assert "not_a_name" in clean_raw_name("")[1].flags
    assert "not_a_name" in clean_raw_name("S")[1].flags


# ----------------------------------------------------------------------- name_key
@pytest.mark.parametrize("variants", [
    ("Müller Simon", "Mueller Simon", "Muller Simon", "MÜLLER Simon"),
    ("Übersax Remo", "Uebersax Remo"),
    ("Hänni Stéphane", "Haenni Stephane", "Hanni Stéphane"),
    ("Maridor Loïc", "Maridor Loic"),
    ("Kläy Jean-Philippe", "Klay Jean Philippe", "kläy jean-philippe"),
    ("von Büren Stephan", "Von Büren Stephan", "VON BÜREN Stephan"),
    ("Z'Rotz Roman", "Zrotz Roman"),
    ("Schläfli Hugo", "Schlaefli Hugo"),
    ("Strauß Max", "Strauss Max"),
])
def test_name_key_unifies_spellings(variants):
    keys = {name_key(v) for v in variants}
    assert len(keys) == 1, keys


def test_name_key_keeps_distinct_names_apart():
    assert name_key("Bauer Hans") != name_key("Baur Hans")   # 'au'+'e' is no umlaut
    assert name_key("Keller Markus") != name_key("Koller Markus")
    assert name_key("Marco Hans") != name_key("Hans Marco")   # order is kept


def test_name_key_shape():
    assert name_key("  Kenel   Franz-Toni ") == "kenel franz toni"
    assert name_key("Sch?nenberger Tobias") == "sch?nenberger tobias"
    assert has_wildcard(name_key("M?ller Simon"))
    assert compact_key("auf der maur alex") == "aufdermauralex"


# ------------------------------------------------------------------- similarity
def test_wildcard_match():
    assert wildcard_match("m?ller simon", name_key("Müller Simon"))
    assert wildcard_match("ny?enegger florian", "nyffenegger florian")  # ff ligature
    assert wildcard_match("schl??i hugo", name_key("Schläfli Hugo"))      # ä + fl
    assert wildcard_match("r??sli janis", name_key("Röösli Janis"))
    assert not wildcard_match("m?ller simon", "muller simona")
    assert not wildcard_match("m?ller simon", "moser simon")
    assert wildcard_match("muller simon", "muller simon")


def test_edit_distance():
    assert edit_distance("maridor", "marridor") == 1
    assert edit_distance("walther", "walthert") == 1
    assert edit_distance("abcd", "abdc") == 1          # adjacent swap
    assert edit_distance("kitten", "sitting") == 3
    assert edit_distance("a", "abcdefgh", limit=2) == 3  # early exit returns limit+1


def test_name_similarity_classes():
    k = name_key
    assert name_similarity(k("Maridor Loïc"), k("Marridor Loic")) == 0.85
    assert name_similarity(k("Von Laufen Lukas"), k("Vonlaufen Lukas")) == 0.95
    assert name_similarity(k("Fankhauser Hans-Jakob"), k("Fankhauser Hansjakob")) == 0.95
    assert name_similarity(k("Stephan von Büren"), k("von Büren Stephan")) == 0.9
    assert name_similarity(k("Steiner Michi"), k("Steiner Michael")) == 0.85
    assert name_similarity(k("Diener Toni"), k("Diener Anton")) == 0.85
    assert name_similarity("m?ller simon", k("Müller Simon")) == 0.95
    assert name_similarity(k("Forrer Arnold"), k("Forrer Arnold")) == 1.0
    assert name_similarity(k("Forrer Arnold"), k("Glarner Matthias")) < 0.5
    # symmetric
    a, b = k("Kenel Franz-Toni"), k("Kennel Franz Toni")
    assert name_similarity(a, b) == name_similarity(b, a) == 0.85


def test_first_name_canonical():
    assert first_name_canonical("Michi") == first_name_canonical("Michael")
    assert first_name_canonical("Sämi") == first_name_canonical("Samuel")  # ä folds to a
    assert first_name_canonical("Sami") == first_name_canonical("Samuel")
    assert first_name_canonical("Reto") == "reto"


# -------------------------------------------------------------------------- index
KEYS = [name_key(n) for n in [
    "Maridor Loïc", "Kenel Franz-Toni", "Keller Markus", "Koller Markus",
    "Müller Simon", "Mueller Fabian", "von Büren Stephan", "Vonlaufen Lukas",
    "Steiner Michael", "Nyffenegger Florian", "Schläfli Hugo", "Forrer Arnold",
    "Gasser Dominik", "Stucki Simon", "Stucki Timon",
]]


def test_index_candidates():
    idx = NameIndex(KEYS)
    assert [k for k, _ in idx.candidates(name_key("Marridor Loic"))] == ["maridor loic"]
    assert [k for k, _ in idx.candidates(name_key("Kennel Franz Toni"))] == ["kenel franz toni"]
    assert [k for k, _ in idx.candidates(name_key("Stephan von Büren"))] == ["von buren stephan"]
    assert [k for k, _ in idx.candidates(name_key("Von Laufen Lukas"))] == ["vonlaufen lukas"]
    assert [k for k, _ in idx.candidates(name_key("Steiner Michi"))] == ["steiner michal"]
    assert [k for k, _ in idx.candidates("m?ller simon")] == ["muller simon"]
    assert [k for k, _ in idx.candidates("ny?enegger florian")] == ["nyffenegger florian"]
    assert [k for k, _ in idx.candidates("schl??i hugo")] == ["schlafli hugo"]
    assert idx.candidates(name_key("Glarner Matthias")) == []


def test_index_excludes_self_and_is_deterministic():
    idx = NameIndex(KEYS + KEYS)
    got = idx.candidates("keller markus")
    assert got == [("koller markus", 0.85)]
    assert NameIndex(reversed(KEYS)).candidates("keller markus") == got


def test_index_finds_wildcard_keys_from_the_clean_side():
    idx = NameIndex(KEYS + ["m?ller simon"])
    assert ("m?ller simon", 0.95) in idx.candidates("muller simon")


def test_duplicate_name_collisions_are_candidates_not_merges():
    """Same-name and near-name collisions: the module only proposes candidates.

    'Gasser Dominik 1' / '2' (two people in the same sheet) share one key; the
    suffix stays in the extras so the resolver can keep them apart. 'Stucki Simon' /
    'Stucki Timon' and 'Keller' / 'Koller Markus' are real, distinct athletes that
    compete in the same festivals: they are only candidates (score < 1)."""
    (a, ea), (b, eb) = clean_raw_name("Gasser Dominik 1"), clean_raw_name("Gasser Dominik 2")
    assert name_key(a) == name_key(b) and ea.suffix != eb.suffix
    idx = NameIndex(KEYS)
    for k, other in [("stucki simon", "stucki timon"), ("keller markus", "koller markus")]:
        cands = dict(idx.candidates(k))
        assert other in cands and cands[other] < 1.0


def test_min_score_filters():
    idx = NameIndex(KEYS)
    assert idx.candidates(name_key("Stephan von Büren"), min_score=0.95) == []


def test_fuzzy_candidates_wrapper():
    assert fuzzy_candidates(name_key("Marridor Loic"), KEYS) == [("maridor loic", 0.85)]
