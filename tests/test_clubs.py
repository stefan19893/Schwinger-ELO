"""Schwingklub normalisation and Teilverband mapping (src/pipeline/clubs.py)."""

from __future__ import annotations

import json

import pytest

from src.pipeline import clubs

from src.pipeline.clubs import (CODE_TO_SUB, SUB_ASSOCIATIONS, ClubRegistry, club_aliases,
                                club_key, code_in_club, pseudo_club_sub, strip_code,
                                sub_association_for_code, sub_association_for_festival,
                                sub_association_for_name)
from src.scraper.festival_reference import REFERENCE_PATH


@pytest.mark.parametrize("a, b", [
    ("Schwingklub Rapperswil und Umgebung", "Rapperswil u. Umgebung"),
    ("SK Rapperswil", "Rapperswil u. Umgebung"),
    ("Fribourg & Environs", "Fribourg et environs"),
    ("Club des lutteurs de la Gruyère", "La Gruyère"),
    ("Schwingklub am Mythen", "am Mythen"),
    ("Mümliswil-Ramiswil", "Muemliswil Ramiswil"),
    ("Mümliswil-Ramiswil", "MÜMLISWIL-RAMISWIL"),
    ("VD Aigle", "Aigle"),
    ("St. Gallen u. Umgebung", "St.Gallen und Umgebung"),
    ("Estavayer-le-Lac", "Estavayer le Lac"),
])
def test_club_key_variants_share_a_key(a: str, b: str) -> None:
    assert club_key(a) == club_key(b) is not None


@pytest.mark.parametrize("a, b", [
    ("Oberwil-Zug", "Oberwil BL"),
    ("Wil", "Wilchingen"),
    ("Biel", "Bienne-Seeland"),
])
def test_different_clubs_keep_different_keys(a: str, b: str) -> None:
    assert club_key(a) != club_key(b)


@pytest.mark.parametrize("raw", [None, "", "*", "o", "1.", "(SWS)", "ARLS", "  "])
def test_no_club_key_for_codes_and_debris(raw: str | None) -> None:
    assert club_key(raw) is None or raw == "ARLS"


def test_codes_inside_club_column() -> None:
    assert code_in_club("(SWS)") == "SWS" and code_in_club("NOSV") == "NOSV"
    assert code_in_club("Wil") is None and code_in_club("SG") is None  # bare 2-letter: ambiguous
    assert pseudo_club_sub("ARLS") == "SWSV"
    assert strip_code("OTT 98") == (None, "OTT")
    assert strip_code("GST Schaffhausen") == ("GST", "Schaffhausen")


def test_bilingual_aliases() -> None:
    assert club_aliases("Kerzers/Chiètres") == ["kerzers", "chietres"]
    assert club_aliases("Sense/La Singine") == ["sense", "singine"]
    assert club_aliases("Entlebuch") == []


def test_code_mapping_covers_every_teilverband() -> None:
    assert set(CODE_TO_SUB.values()) == set(SUB_ASSOCIATIONS)
    assert sub_association_for_code("ONW") == "ISV"
    assert sub_association_for_code("BO") == "BKSV"
    assert sub_association_for_code("NOS") == "NOSV"
    assert sub_association_for_code("ag") == "NWSV"
    assert sub_association_for_code("VD") == "SWSV"
    assert sub_association_for_code("GA") is None  # guest
    assert sub_association_for_code("AG Freiamt") == "NWSV"


@pytest.mark.parametrize("name, sub", [
    ("Innerschweiz", "ISV"), ("Bern", "BKSV"), ("Nordostschweiz", "NOSV"),
    ("Nordwestschweiz", "NWSV"), ("Suedwestschweiz", "SWSV"), ("Südwestschweiz", "SWSV"),
    ("Association romande de lutte suisse", "SWSV"), ("Ausland", None), (None, None),
])
def test_association_names(name: str | None, sub: str | None) -> None:
    assert sub_association_for_name(name) == sub


def _reference() -> dict[str, object]:
    return json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["schwingfeste_schweiz"]


def test_every_reference_kantonal_festival_maps_to_its_teilverband() -> None:
    groups = _reference()["kantonal_und_gauverbandsfeste"]["unterteilung_nach_teilverband"]  # type: ignore[index]
    for sub, names in groups.items():
        for name in names:
            assert sub_association_for_festival(name, "Kantonal") == sub, name


def test_every_reference_teilverband_festival_maps() -> None:
    for t in _reference()["teilverbandsfeste"]["turniere"]:  # type: ignore[index]
        assert sub_association_for_festival(t["fest"], "Teilverband") == t["teilverband_code"]


@pytest.mark.parametrize("name, category, assoc, sub", [
    ("Luzerner Kantonalschwingfest Rothenburg 2011", "Kantonal", None, "ISV"),
    ("Bern-Jurassisches Schwingfest Tramelan 2019", "Gauverband", None, "BKSV"),
    ("Jurassisches Kantonalschwingfest Saignelégier 2026", "Kantonal", None, "SWSV"),
    ("Tessiner Kantonalschwingfest Biasca 2025", "Kantonal", None, "ISV"),
    ("Basellandschaftliches Kantonalschwingfest Ormalingen 2015", "Kantonal", None, "NWSV"),
    ("Glarner-Bündner Kantonalschwingfest Matt 2013", "Kantonal", None, "NOSV"),
    ("Fête Cantonale Valaisanne", "Kantonal", None, "SWSV"),
    ("Solothurner Kantonalschwingfest Matzendorf 2021", "Kantonal", None, "NWSV"),
    ("Urner Kantonalschwingfest Bürglen 2019", "Kantonal", None, "ISV"),
    ("Schwägalp-Schwinget 2025", "Bergkranz", None, "NOSV"),
    ("Brünig-Schwinget 2025", "Bergkranz", None, None),  # ISV + BKSV
    ("Eidgenössisches Schwing- und Älplerfest Zug 2019", "ESAF", None, None),
    ("Frühjahrsschwinget Cham 2012", "Regional", None, None),  # Regional: only via association
    ("Frühjahrsschwinget Cham 2012", "Regional", "Innerschweiz", "ISV"),
])
def test_festival_sub_association(name: str, category: str, assoc: str | None,
                                  sub: str | None) -> None:
    assert sub_association_for_festival(name, category, assoc) == sub


# ----------------------------------------------------------------- registry
def test_registry_votes_display_name_and_conflicts() -> None:
    reg = ClubRegistry()
    reg.add("Entlebuch", code="LU", count=10)
    reg.add("Schwingklub Entlebuch", festival_sub="ISV", count=2)
    reg.add("Entlebuch", festival_sub="BKSV", count=1)  # guest at a Bernese festival
    reg.add("Freiamt", code="AG", count=5)
    reg.add("Freiamt", code="LU", count=3)
    clubs = reg.build()
    e = clubs["entlebuch"]
    assert (e.name, e.sub_association, e.n, e.conflict) == ("Entlebuch", "ISV", 13, False)
    f = clubs["freiamt"]
    assert (f.sub_association, f.conflict) == ("NWSV", True)
    assert reg.resolve("SK Entlebuch") is e
    assert reg.resolve("Unknown Club") is None


def test_registry_prefers_spelling_with_diacritics_on_ties() -> None:
    reg = ClubRegistry()
    reg.add("Kerzers/Chietres", code="FR")
    reg.add("Kerzers/Chiètres", code="FR")
    assert reg.build()["kerzers chietres"].name == "Kerzers/Chiètres"


def test_bilingual_part_folds_into_the_club() -> None:
    reg = ClubRegistry()
    reg.add("Kerzers/Chiètres", code="FR", count=50)
    reg.add("Kerzers", code="FR", count=5)
    assert reg.resolve("Kerzers").key == "kerzers chietres"  # type: ignore[union-attr]
    assert reg.resolve("Chiètres").key == "kerzers chietres"  # type: ignore[union-attr]
    assert reg.build()["kerzers chietres"].n == 55


def test_merged_cell_debris_does_not_swallow_a_club() -> None:
    """'Fribourg/Rothenburg' (two cells merged by the PDF) must not absorb the
    much bigger 'Rothenburg' club or carry its votes to SWSV."""
    reg = ClubRegistry()
    reg.add("Rothenburg", code="LU", count=200)
    reg.add("Fribourg/Rothenburg", code="FR", count=1)
    rot = reg.resolve("Rothenburg")
    assert rot is not None and rot.key == "rothenburg" and rot.sub_association == "ISV"


def test_abbreviations_fold_only_when_unique_and_established() -> None:
    reg = ClubRegistry()
    reg.add("Solothurn u. Umgebung", code="SO", count=40)
    reg.add("Sol", code="SO", count=6)           # abbreviated list -> folds
    reg.add("Wil", code="SG", count=500)         # real club, no longer club to fold into
    reg.add("Wilchingen", code="SH", count=20)
    reg.add("Mels", code="SG", count=50)
    clubs = reg.build()
    assert "sol" not in clubs and clubs["solothurn"].n == 46
    assert reg.resolve("Sol").key == "solothurn"  # type: ignore[union-attr]
    assert reg.resolve("Wil").key == "wil"  # type: ignore[union-attr]
    assert reg.resolve("Mels").key == "mels"  # type: ignore[union-attr]
    assert [c.key for c in reg.short_clubs()] == ["wil"]
    # unseen abbreviation: unique prefix within the Teilverband
    assert reg.resolve("Mel", code="SG").key == "mels"  # type: ignore[union-attr]
    assert reg.resolve("Wi", code="SH") is None  # 'wil' and 'wilchingen', both NOSV


def test_pseudo_clubs_are_not_registered() -> None:
    reg = ClubRegistry()
    reg.add("ARLS", count=10)
    reg.add("(SWS)", count=3)
    assert reg.build() == {}


def test_portrait_votes_and_esv_id() -> None:
    reg = ClubRegistry()
    reg.add("Rottal", portrait_sub="ISV", esv_id=205, count=4)
    reg.add("Rottal und Umgebung", festival_sub="NOSV")
    c = reg.resolve("Rottal")
    assert c is not None and c.sub_association == "ISV" and c.esv_id == 205


# ----------------------------------------------------------------- established clubs
def test_trailing_codes_and_merged_cells() -> None:
    assert strip_code("La Gruyère ARLS") == ("ARLS", "La Gruyère")
    assert strip_code("Langnau EM") == ("EM", "Langnau")
    assert strip_code("Ollon VD") == (None, "Ollon VD")  # cantonal suffix of a place name
    assert club_key("La Gruyère ARLS") == club_key("La Gruyère") == "gruyere"
    assert club_key("Einsiedeln / Oberdiessbach") is None  # two cells merged by the PDF
    assert club_key("Kerzers/Chiètres") == "kerzers chietres"
    assert sub_association_for_code("EM") == "BKSV"


def test_min_obs_keeps_only_established_clubs() -> None:
    reg = ClubRegistry(min_obs=20)
    reg.add("Lausanne", code="VD", count=120)
    reg.add("Rottal", portrait_sub="ISV", esv_id=205, count=2)   # ESV club id: established
    reg.add("Villars-le-Terroir Lausanne", code="VD", count=3)   # residence glued to the club
    reg.add("Grandvillard", code="FR", count=4)                  # a residence, not a club
    reg.add("Pelagiberg", count=30)                              # no Teilverband evidence
    reg.add("Hasle", festival_sub="ISV", count=1)
    reg.add("Hasle", count=27)                                   # 28 obs, a single vote
    reg.add("KK", code="BO", count=40)                           # Kranz letters, not a club
    clubs = reg.build()
    assert sorted(clubs) == ["lausanne", "rottal"]
    assert clubs["lausanne"].n == 120  # debris does not add observations or votes
    assert reg.resolve("Villars-le-Terroir Lausanne") is clubs["lausanne"]
    assert reg.resolve("Grandvillard") is None and reg.resolve("Pelagiberg") is None
    assert reg.debris == {"villars terroir lausanne": 3, "grandvillard": 4, "pelagiberg": 30,
                          "hasle": 28}


def test_truncated_names_fold_into_the_unique_esv_club() -> None:
    reg = ClubRegistry(min_obs=20)
    reg.add("Mümliswil-Ramiswil", portrait_sub="NWSV", esv_id=278, count=30)
    reg.add("Mümliswil-Rami", code="SO", count=200)       # narrow column in 2011-2014 lists
    reg.add("Oberwil BL", portrait_sub="NWSV", esv_id=268, count=10)
    reg.add("Oberwil-Zug", portrait_sub="ISV", esv_id=230, count=10)
    reg.add("Oberwil", code="BL", count=90)                # NWSV: only 'Oberwil BL' fits
    reg.add("Bezirk Waldenburg", portrait_sub="NWSV", esv_id=270, count=5)
    reg.add("Waldenburg", code="BL", count=100)            # 'Bezirk' carries no identity
    clubs = reg.build()
    assert sorted(clubs) == ["mumliswil ramiswil", "oberwil bl", "oberwil zug", "waldenburg"]
    assert clubs["mumliswil ramiswil"].n == 230 and clubs["waldenburg"].n == 105
    assert reg.resolve("Mümliswil-Rami").esv_id == 278  # type: ignore[union-attr]
    assert reg.resolve("Oberwil", code="BL").esv_id == 268  # type: ignore[union-attr]


@pytest.mark.parametrize(("old", "club"), [
    ("Zurzach", "Schwingklub Zurzibiet"), ("Mythenverband", "am Mythen"),
    ("Schangnau-Siehen", "Siehen"), ("Weite-Wartau", "Wartau"), ("Ticino", "Tessin"),
    ("Basel", "Basel-Stadt")])
def test_other_names_of_one_club_share_its_key(old: str, club: str) -> None:
    assert clubs.club_key(old) == clubs.club_key(club)


def test_alias_folds_into_the_esv_club_under_the_esv_name() -> None:
    """'Zurzach' is printed more often than 'Zurzibiet': the ESV's name is displayed."""
    reg = ClubRegistry(min_obs=20)
    reg.add("Zurzach", code="AG", count=300)
    reg.add("Zurzibiet", portrait_sub="NWSV", esv_id=77, count=5)
    reg.add("Schwingklub Zurzibiet", code="AG", count=40)
    built = reg.build()
    assert list(built) == ["zurzibiet"]
    c = reg.resolve("Zurzach")
    assert c is not None and (c.name, c.esv_id, c.sub_association, c.n) == ("Zurzibiet", 77, "NWSV", 345)


@pytest.mark.parametrize(("name", "expected"), [
    ("TO", True), ("RO", True), ("RA", True), ("ST", True), ("Wil", False), ("Thun", False),
    ("Schaffhausen", False), ("", False), (None, False)])
def test_region_codes_are_not_clubs(name: str | None, expected: bool) -> None:
    assert clubs.is_region_code(name) is expected
