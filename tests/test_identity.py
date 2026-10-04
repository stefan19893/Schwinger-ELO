"""Evidence-based identity resolver: duplicate-name collisions, spelling variants,
portrait anchors, determinism. Offline: synthetic frames + the --sample fixtures."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from src import cli
from src.pipeline import cleaner as cl
from src.pipeline import identity as idn

_SEQ = iter(range(10**6))


def row(fest: int, date: str, name: str, **ev: object) -> dict[str, object]:
    """One athletes_raw row with evidence; `fest` + `date` identify the festival."""
    idx = next(_SEQ)
    base: dict[str, object] = {
        "athlete_raw_id": f"{fest}-{idx:06d}", "fest_id": fest, "idx": idx, "name_raw": name,
        "name": name, "fest_date": date, "fest_year": int(date[:4]), "flags": "",
        "birth_year": None, "club": None, "sub_association": None, "sub_assoc_source": None,
        "residence": None, "portrait_id": None, "portrait_slug": None}
    if "tv" in ev:
        ev["sub_association"], ev["sub_assoc_source"] = ev.pop("tv"), ev.pop("src", "code")
    return base | ev


def portrait(pid: int, last: str, first: str, birthday: str, club: str | None = None,
             city: str | None = None, assoc: str | None = None) -> dict[str, object]:
    return {"portrait_id": pid, "slug": f"{first}-{last}-{pid}".lower(), "last_name": last,
            "first_name": first, "birthday": birthday, "city": city, "club_name": club,
            "association_name": assoc}


def resolve(rows: list[dict], portraits: list[dict] | None = None,
            bouts: list[tuple[str, str]] | None = None) -> tuple[dict[str, str], pd.DataFrame, pd.DataFrame]:
    raw = pd.DataFrame(rows)
    b = pd.DataFrame(bouts or [], columns=["athlete_a_id", "athlete_b_id"])
    p = pd.DataFrame(portraits, columns=list(cl.PORTRAIT_COLUMNS)) if portraits else None
    res = idn.EvidenceResolver().resolve(cl.ResolverInput(raw=raw, bouts=b, portraits=p))
    ident = res.identity.set_index("athlete_raw_id")
    assert set(ident.index) == set(raw["athlete_raw_id"])  # nothing unmapped
    assert set(ident["athlete_id"]) == set(res.athletes["athlete_id"])
    return ident["athlete_id"].to_dict(), ident, res.athletes.set_index("athlete_id")


def ids(m: dict[str, str], rows: list[dict]) -> list[str]:
    return [m[r["athlete_raw_id"]] for r in rows]


# ------------------------------------------------------------------ duplicate names
def test_unique_name_is_one_person_across_sheets() -> None:
    rows = [row(1, "2012-05-01", "Muster Hans **"), row(2, "2013-05-01", "Muster Hans, S *"),
            row(3, "2016-05-01", "MUSTER Hans (1990)")]
    m, ident, ath = resolve(rows)
    assert len(set(m.values())) == 1
    a = ath.iloc[0]
    assert a["full_name"] == "Muster Hans" and a["evidence"] == "unique_name"
    assert a.name == f"muster-hans-{rows[0]['athlete_raw_id']}"  # id = earliest row
    assert set(ident["evidence"]) <= {"name", "name+birth_year"} and ident["confidence"].min() >= 0.85


def test_same_sheet_namesakes_stay_two_people_without_self_bout() -> None:
    a1 = row(1, "2018-05-01", "Hermann Lukas 1", club="Aarau", tv="NWSV", residence="Muhen")
    a2 = row(1, "2018-05-01", "Hermann Lukas 2", club="Oberwil BL", tv="NWSV",
             residence="Hofstetten SO")
    b1 = row(2, "2018-06-01", "Hermann Lukas", club="Aarau", tv="NWSV", residence="Muhen")
    b2 = row(3, "2019-06-01", "Hermann Lukas", club="Oberwil BL", tv="NWSV",
             residence="Hofstetten")
    rows = [a1, a2, b1, b2]
    m, ident, ath = resolve(rows, bouts=[(a1["athlete_raw_id"], a2["athlete_raw_id"])])
    assert m[a1["athlete_raw_id"]] != m[a2["athlete_raw_id"]]
    assert ids(m, [a1, a2]) == ids(m, [b1, b2])  # each later row follows its club
    assert ath["evidence"].str.startswith("namesakes=2(same_date)").all()
    assert set(ident["evidence"]) == {"name+club"}
    raw_bouts = pd.DataFrame({"bout_id": ["x"], "athlete_a_id": [a1["athlete_raw_id"]],
                              "athlete_b_id": [a2["athlete_raw_id"]]})
    remap = cl.remap_bouts(raw_bouts, ident.reset_index())
    assert remap.counts.get("rejected_self_bout", 0) == 0 and len(remap.bouts) == 1


def test_same_sheet_namesakes_without_any_evidence() -> None:
    rows = [row(1, "2012-05-01", "Wicki David 1"), row(1, "2012-05-01", "Wicki David 2"),
            row(2, "2012-06-01", "Wicki David")]
    m, ident, ath = resolve(rows)
    assert len(set(ids(m, rows[:2]))) == 2 and len(set(m.values())) == 2
    # the third row cannot be decided from the data: attached, but flagged and low confidence
    third = ident.loc[rows[2]["athlete_raw_id"]]
    assert "ambiguous" in third["evidence"] and third["confidence"] <= 0.4
    assert ath["evidence"].str.contains("ambiguous_rows=1").sum() == 1


def test_same_date_at_two_festivals_is_two_people() -> None:
    rows = [row(1, "2014-05-01", "Gwerder Andreas", residence="Muotathal", tv="ISV"),
            row(2, "2014-05-01", "Gwerder Andreas", residence="Hütten", tv="NOSV"),
            row(3, "2015-05-01", "Gwerder Andreas", residence="Muotathal SZ", tv="ISV"),
            row(4, "2016-05-01", "Gwerder Andreas", residence="Hütten", tv="NOSV")]
    m, _, ath = resolve(rows)
    a, b, c, d = ids(m, rows)
    assert a != b and a == c and b == d
    assert set(ath["sub_association"]) == {"ISV", "NOSV"}


def test_birth_year_split_and_absent_year_is_no_information() -> None:
    old = [row(1, "2016-05-01", "Giger Samuel (1998)", club="Ottenberg", tv="NOSV"),
           row(2, "2019-05-01", "Giger Samuel", club="Ottenberg", tv="NOSV"),     # year not printed
           row(4, "2024-05-01", "Giger Samuel", club="Ottenberg", tv="NOSV")]
    young = [row(3, "2021-05-01", "Giger Samuel (2005)", club="Entlebuch", tv="ISV"),
             row(5, "2024-06-01", "Giger Samuel", club="Entlebuch", tv="ISV")]
    m, _, ath = resolve(old + young)
    assert len(set(ids(m, old))) == 1 and len(set(ids(m, young))) == 1
    assert ids(m, old)[0] != ids(m, young)[0]
    assert sorted(ath["birth_year"]) == [1998, 2005]
    # same club and village, years two apart: still two people (cousins)
    rows = [row(1, "2016-05-01", "Thalmann Adrian (1997)", club="Entlebuch", residence="Ebnet"),
            row(2, "2017-05-01", "Thalmann Adrian (1999)", club="Entlebuch", residence="Ebnet")]
    assert len(set(resolve(rows)[0].values())) == 2
    # one year apart is a typo / age-based list, not a second person
    rows = [row(1, "2016-05-01", "Thalmann Adrian (1997)", club="Entlebuch"),
            row(2, "2017-05-01", "Thalmann Adrian (1998)", club="Entlebuch")]
    assert len(set(resolve(rows)[0].values())) == 1


def test_club_change_over_a_career_stays_one_person() -> None:
    rows = [row(1, "2012-05-01", "Zangger Dominik", club="Langenthal", tv="BKSV", residence="Roggwil"),
            row(2, "2013-05-01", "Zangger Dominik", club="Langenthal", tv="BKSV", residence="Roggwil"),
            row(3, "2016-05-01", "Zangger Dominik", club="Wolhusen", tv="BKSV", residence="Willisau"),
            row(4, "2019-05-01", "Zangger Dominik", club="Wolhusen", tv="ISV", residence="Willisau"),
            # double membership: clubs alternate within a season
            row(5, "2019-06-01", "Zangger Dominik", club="Langenthal", tv="BKSV", residence="Willisau"),
            row(6, "2020-06-01", "Zangger Dominik", club="Wolhusen", tv="ISV", residence="Willisau")]
    m, _, ath = resolve(rows)
    assert len(set(m.values())) == 1
    assert ath.iloc[0]["club"] == "Wolhusen" and "teilverbaende=BKSV/ISV" in ath.iloc[0]["evidence"]


def test_other_teilverband_without_shared_evidence_is_split_and_flagged() -> None:
    main = [row(i, f"20{10 + i}-05-01", "Betschart Daniel", residence="Muotathal", tv="ISV")
            for i in range(1, 12)]
    other = [row(20, "2012-07-01", "Betschart Daniel", residence="Landquart", tv="NOSV")]
    weak = [row(22, "2014-07-01", "Betschart Daniel", tv="SWSV", src="festival")]
    m, _, ath = resolve(main + other + weak)
    assert len(set(m.values())) == 2
    assert ids(m, weak) == ids(m, main[:1])    # the organiser's Teilverband is no evidence
    frag = ath.loc[ids(m, other)[0]]
    assert "-teilverband" in frag["evidence"] and frag["evidence"].endswith("fragment")


def test_namesakes_sharing_club_and_village() -> None:
    """Gasser Dominik 1 / 2: same club, same village, sheet numbers not stable."""
    one = [row(1, "2015-04-06", "Gasser Dominik 1", residence="Lungern", tv="ISV"),
           row(3, "2016-05-05", "Gasser Dominik 1", club="Lungern", tv="ISV", birth_year="1996"),
           row(4, "2017-05-05", "Gasser Dominik", club="Lungern", tv="ISV", birth_year="1996")]
    two = [row(1, "2015-04-06", "Gasser Dominik 2", residence="Lungern", tv="ISV"),
           row(3, "2016-05-05", "Gasser Dominik 2", club="Lungern", tv="ISV", birth_year="1998"),
           row(5, "2017-06-05", "Gasser Dominik", club="Lungern", tv="ISV", birth_year="1998")]
    loose = [row(6 + i, f"2015-0{i + 5}-01", "Gasser Dominik", residence="Lungern", tv="ISV")
             for i in range(4)]
    m, ident, ath = resolve(one + two + loose)
    assert len(ath) == 2 and sorted(ath["birth_year"]) == [1996, 1998]  # no phantom third person
    assert m[one[0]["athlete_raw_id"]] != m[two[0]["athlete_raw_id"]]
    undecided = ident.loc[[r["athlete_raw_id"] for r in loose]]
    assert undecided["evidence"].str.contains("ambiguous").all()
    assert (undecided["confidence"] <= 0.4).all()
    assert ath["evidence"].str.contains("ambiguous_rows").any()


def test_rows_with_identical_evidence_of_two_namesakes_are_assigned_one_by_one() -> None:
    """Rows that look alike but collide with both persons by date are a mix of the
    two: each goes to the person who was not at another festival that day."""
    one = [row(1, "2016-05-05", "Gasser Dominik", club="Lungern", tv="ISV", birth_year="1996"),
           row(2, "2017-05-05", "Gasser Dominik", club="Lungern", tv="ISV", birth_year="1996")]
    two = [row(3, "2016-06-05", "Gasser Dominik", club="Lungern", tv="ISV", birth_year="1998"),
           row(4, "2017-06-05", "Gasser Dominik", club="Lungern", tv="ISV", birth_year="1998")]
    x = row(5, "2017-05-05", "Gasser Dominik", club="Lungern", tv="ISV")   # one is elsewhere
    y = row(6, "2017-06-05", "Gasser Dominik", club="Lungern", tv="ISV")   # two is elsewhere
    m, _, ath = resolve(one + two + [x, y])
    assert len(ath) == 2
    assert ids(m, [x]) == ids(m, two[:1]) and ids(m, [y]) == ids(m, one[:1])


# ------------------------------------------------------------------ portraits
def test_portrait_is_the_hard_key() -> None:
    reg = [portrait(682, "Schuler", "Alex", "1991-02-03", "Einsiedeln", "Rothenthurm", "Innerschweiz"),
           portrait(9246, "Schuler", "Alex", "1998-06-07", "am Mythen", "Rothenthurm", "Innerschweiz")]
    linked = [row(10, "2024-05-01", "Schuler Alex", portrait_id=682.0, club="Einsiedeln",
                  residence="Rothenthurm", tv="ISV"),
              row(10, "2024-05-01", "Schuler Alex", portrait_id=9246.0, club="am Mythen",
                  residence="Rothenthurm", tv="ISV"),
              row(11, "2025-05-01", "Schuler Alex", portrait_id=682.0, club="Schwyz",
                  residence="Schwyz", tv="NOSV")]             # same portrait: everything changed
    early = [row(1, "2012-05-01", "Schuler Alex", residence="Rothenthurm", tv="ISV"),   # 1998: age 14
             row(2, "2018-05-01", "Schuler Alex", club="am Mythen", residence="Rothenthurm", tv="ISV"),
             row(3, "2018-06-01", "Schuler Alex", club="Einsiedeln", residence="Rothenthurm", tv="ISV")]
    m, ident, ath = resolve(linked + early, reg)
    assert len(ath) == 2
    a, b, c = ids(m, linked)
    assert a == c == "schuler-alex-p682" and b == "schuler-alex-p9246"
    assert ids(m, early) == [a, b, a]
    assert ath.loc[a, "slug"] == "alex-schuler-682" and ath.loc[a, "birth_year"] == 1991
    assert ident.loc[linked[0]["athlete_raw_id"], ["evidence", "confidence"]].tolist() == [
        "name+portrait", 1.0]
    per_athlete = pd.DataFrame(linked).assign(a=ids(m, linked)).groupby("a")["portrait_id"].nunique()
    assert (per_athlete == 1).all()


def test_registry_anchors_old_rows_and_respects_age() -> None:
    reg = [portrait(7, "Tissot", "Joffrey", "2006-03-01", "Val-de-Travers", "Boudry", "Suedwestschweiz")]
    old = [row(1, "2012-05-01", "Tissot Joffrey", residence="Fleurier", tv="SWSV")]  # he was 6
    new = [row(2, "2022-05-01", "Tissot Joffrey", club="Val-de-Travers", tv="SWSV"),
           row(3, "2023-05-01", "Tissot Joffrey", residence="Boudry", tv="SWSV")]
    m, _, ath = resolve(old + new, reg)
    assert ids(m, new) == ["tissot-joffrey-p7"] * 2
    assert ids(m, old) != ids(m, new[:1]) and "(age)" in ath.loc[ids(m, old)[0], "evidence"]
    assert ath.loc["tissot-joffrey-p7", ["birth_year", "slug", "evidence"]].tolist()[:2] == [
        2006, "joffrey-tissot-7"]
    assert "registry_anchor" in ath.loc["tissot-joffrey-p7", "evidence"]
    # an unused anchor creates no athlete
    assert len(resolve(old, reg)[2]) == 1


def test_duplicate_registry_portraits_are_one_person() -> None:
    reg = [portrait(13423, "Huwiler", "Roman", "1998-04-01", "Cham-Ennetsee", "Auw", "Innerschweiz"),
           portrait(37807, "Huwiler", "Roman", "1998-04-01", "Oberhabsburg", "Auw", "Innerschweiz")]
    rows = [row(1, "2015-05-01", "Huwiler Roman", club="Cham-Ennetsee", tv="ISV"),
            row(2, "2024-05-01", "Huwiler Roman", club="Oberhabsburg", tv="ISV", portrait_id=37807.0)]
    m, _, ath = resolve(rows, reg)
    assert set(m.values()) == {"huwiler-roman-p37807"} and len(ath) == 1  # the linked one is kept


def test_one_portrait_under_two_spellings_is_one_person() -> None:
    reg = [portrait(5, "Kennel", "Stefan", "1995-01-01", "Rigiverband")]
    rows = [row(1, "2023-05-01", "Kennel Stefan", portrait_id=5.0),
            row(2, "2023-06-01", "Kenel Stefan H.", portrait_id=5.0)]
    m, ident, _ = resolve(rows, reg)
    assert len(set(m.values())) == 1
    assert ident["evidence"].tolist() == ["name+portrait", "variant:portrait|name+portrait"]


# ------------------------------------------------------------------ review fixes (2026-10-03)
def _same_club_portraits(birth_a: str, birth_b: str) -> list[dict]:
    return [portrait(19828, "Christen", "Thomas", birth_a, "Nidwalden", "Ennetmoos", "Innerschweiz"),
            portrait(38050, "Christen", "Thomas", birth_b, "Nidwalden", "Ennetmoos", "Innerschweiz")]


def test_equally_fitting_anchors_are_chosen_by_age_not_by_portrait_id() -> None:
    """M1: father (b. 1964) and son (b. 1992) with the same club and village."""
    rows = [row(i, f"20{18 + i}-05-01", "Christen Thomas", club="Nidwalden", residence="Ennetmoos",
                tv="ISV") for i in range(5)]
    m, ident, ath = resolve(rows, _same_club_portraits("1964-12-26", "1992-06-17"))
    assert set(m.values()) == {"christen-thomas-p38050"}       # aged 26-30, not 54-58
    assert ath.iloc[0]["birth_year"] == 1992
    assert not ident["evidence"].str.contains("ambiguous").any() and ident["confidence"].min() > 0.9
    # the registry order must not matter
    m2, _, _ = resolve(rows, list(reversed(_same_club_portraits("1964-12-26", "1992-06-17"))))
    assert m2 == m


def test_rows_between_two_equally_fitting_anchors_are_flagged() -> None:
    """M1: the alternative is a registry anchor (no rows) - the flag goes to the rows."""
    rows = [row(i, f"20{18 + i}-05-01", "Christen Thomas", club="Nidwalden", residence="Ennetmoos",
                tv="ISV") for i in range(5)]
    m, ident, ath = resolve(rows, _same_club_portraits("1990-12-26", "1992-06-17"))
    assert len(set(m.values())) == 1
    assert ident["evidence"].str.endswith("|ambiguous").all() and (ident["confidence"] <= 0.4).all()
    assert "ambiguous_rows=5" in ath.iloc[0]["evidence"] and ath.iloc[0]["confidence"] <= 0.4


def test_old_age_does_not_split_rows_that_have_nobody_else() -> None:
    """The > 40 penalty decides between candidates; a veteran keeps his rows."""
    reg = [portrait(504, "Dejung", "Anton", "1970-01-01", "Davos", "Davos", "Nordostschweiz")]
    rows = [row(1, "2011-05-01", "Dejung Anton", residence="Davos", tv="NOSV"),
            row(2, "2012-05-01", "Dejung Anton", residence="Davos", tv="NOSV"),
            row(3, "2013-05-01", "Dejung Anton")]            # aged 43, no evidence at all
    m, _, ath = resolve(rows, reg)
    assert set(m.values()) == {"dejung-anton-p504"} and len(ath) == 1


def test_registry_anchor_needs_more_than_the_name() -> None:
    """Reviewer: 11 Buttisholz rows were attached by name only to a b. 1958 portrait."""
    reg = [portrait(2849, "Arnold", "Thomas", "1958-12-21", "Bürglen", "Unterschächen", "Innerschweiz")]
    rows = [row(i, f"201{i}-05-01", "Arnold Thomas", residence="Buttisholz", tv="ISV")
            for i in range(1, 4)]
    m, _, ath = resolve(rows, reg)
    a = ath.iloc[0]
    assert len(ath) == 1 and a.name == f"arnold-thomas-{rows[0]['athlete_raw_id']}"
    assert pd.isna(a["birth_year"]) and pd.isna(a["slug"]) and a["evidence"] == "unique_name"
    # with the village it is the registered person
    reg[0]["city"] = "Buttisholz"
    reg[0]["birthday"] = "1988-12-21"
    assert set(resolve(rows, reg)[0].values()) == {"arnold-thomas-p2849"}


def _schmid_reto() -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    reg = [portrait(2159, "Schmid", "Reto", "2007-06-05", "Frutigen", "Frutigen", "Bern"),
           portrait(37904, "Schmid", "Reto", "2016-06-18", "Frutigen", "Frutigen", "Bern")]
    dates = ["2011-06-05", "2012-06-24", "2013-08-04"]
    frutigen = [row(10 + i, d, "Schmid Reto", residence="Frutigen", tv="BKSV", birth_year="1980")
                for i, d in enumerate(dates)]
    frutigen += [row(20, "2012-07-01", "Schmid Reto", residence="Frutigen"),       # no code
                 row(21, "2013-07-01", "Schmid Reto", residence="Frutigen", tv="BKSV"),
                 row(22, "2014-07-01", "Schmid Reto", club="Frutigen", residence="Frutigen", tv="BKSV"),
                 row(23, "2016-07-01", "Schmid Reto", club="Frutigen", residence="Frutigen", tv="BKSV")]
    muttenz = [row(30 + i, d, "Schmid Reto", club="Muttenz", residence="Münchenstein", tv="NWSV")
               for i, d in enumerate(dates + ["2014-08-01"])]
    son = [row(40 + i, f"202{3 + i}-05-01", "Schmid Reto", club="Frutigen", residence="Frutigen",
               tv="BKSV") for i in range(4)]
    loose = [row(50, "2013-09-01", "Schmid Reto")]
    return reg, frutigen, muttenz, son, loose


def test_no_chained_merge_across_a_same_date_collision() -> None:
    """S1: Frutigen (b. 1980) and Muttenz meet on three dates. The Frutigen rows used
    to be taken apart because they fit two registry namesakes (b. 2007 / 2016) they
    cannot be; row by row they then chained into the Muttenz identity."""
    reg, frutigen, muttenz, son, loose = _schmid_reto()
    m, ident, ath = resolve(frutigen + muttenz + son + loose, reg)
    assert len(set(ids(m, frutigen))) == 1 and len(set(ids(m, muttenz))) == 1
    assert ids(m, frutigen)[0] != ids(m, muttenz)[0]            # incl. the colliding rows
    assert set(ids(m, son)) == {"schmid-reto-p2159"}            # aged 16+, not the father at 43+
    assert ids(m, son)[0] != ids(m, frutigen)[0]
    assert ath.loc[ids(m, frutigen)[0], "birth_year"] == 1980
    assert len(ath) == 3                                        # no fragment
    per_athlete = pd.DataFrame(frutigen + muttenz + son + loose).assign(
        a=ids(m, frutigen + muttenz + son + loose))
    assert not per_athlete.duplicated(["a", "fest_date"]).any()
    assert (per_athlete.groupby("a")["sub_association"].nunique() <= 1).all()
    # the son's rows are decided by age, not flagged
    assert not ident.loc[[r["athlete_raw_id"] for r in son], "evidence"].str.contains("ambiguous").any()


def test_father_and_son_with_the_same_club_and_village() -> None:
    """S2: rows 2011 and 2023+, registry portraits b. 1975 and b. 2007 (Beglinger)."""
    reg = [portrait(490, "Beglinger", "Fridolin", "1975-03-06", "Niederurnen u. Umgebung", "Mollis",
                    "Nordostschweiz"),
           portrait(1970, "Beglinger", "Fridolin", "2007-01-31", "Niederurnen u. Umgebung", "Mollis",
                    "Nordostschweiz")]
    old = [row(i, f"2011-0{i}-01", "Beglinger Fridolin", residence="Mollis", tv="NOSV")
           for i in range(4, 8)]
    new = [row(10 + i, f"202{3 + i // 2}-0{5 + i % 2}-01", "Beglinger Fridolin",
               club="Niederurnen u. Umgebung", residence="Mollis", tv="NOSV") for i in range(6)]
    m, _, ath = resolve(old + new, reg)
    assert set(ids(m, old)) == {"beglinger-fridolin-p490"}
    assert set(ids(m, new)) == {"beglinger-fridolin-p1970"}
    assert sorted(ath["birth_year"]) == [1975, 2007]
    assert not ath["evidence"].str.contains("career_gap").any()


def test_same_evidence_after_a_long_gap_is_not_glued_to_the_old_rows() -> None:
    """S2: club and village are printed in both periods; only the son is registered."""
    reg = [portrait(1970, "Beglinger", "Fridolin", "2007-01-31", "Niederurnen u. Umgebung", "Mollis",
                    "Nordostschweiz")]
    old = [row(i, f"201{i}-05-01", "Beglinger Fridolin", club="Niederurnen u. Umgebung",
               residence="Mollis", tv="NOSV") for i in range(4, 7)]       # the son was 7-9
    new = [row(10 + i, f"202{3 + i}-05-01", "Beglinger Fridolin", club="Niederurnen u. Umgebung",
               residence="Mollis", tv="NOSV") for i in range(3)]
    m, _, ath = resolve(old + new, reg)
    assert set(ids(m, new)) == {"beglinger-fridolin-p1970"}
    assert len(set(ids(m, old))) == 1 and ids(m, old)[0] != ids(m, new)[0]
    assert "(age)" in ath.loc[ids(m, old)[0], "evidence"]


def test_unbridged_gap_caps_the_smaller_side() -> None:
    """S2: >= 8 seasons apart and nothing shared: still one identity (a plain gap rule
    split unique names in sparse data), but the smaller side is capped at 0.4."""
    old = [row(1, "2011-05-01", "Wicki Markus", residence="Sörenberg")]
    new = [row(2 + i, f"202{1 + i}-05-01", "Wicki Markus", club="Rottal", residence="Ruswil")
           for i in range(3)]
    m, ident, ath = resolve(old + new)
    assert len(set(m.values())) == 1
    o = ident.loc[old[0]["athlete_raw_id"]]
    assert o["evidence"].endswith("|gap") and o["confidence"] <= 0.4
    assert ident.loc[[r["athlete_raw_id"] for r in new], "confidence"].min() >= 0.9
    assert ath.iloc[0]["evidence"] == "career_gap=9;unbridged_gap_rows=1"
    # a shared village bridges the gap
    old[0]["residence"] = "Ruswil"
    _, ident, ath = resolve(old + new)
    assert ident["confidence"].min() >= 0.9 and ath.iloc[0]["evidence"] == "career_gap=9"
    # 7 seasons: flag only
    old[0].update(residence="Sörenberg", fest_date="2013-05-01", fest_year=2013)
    _, ident, ath = resolve(old + new)
    assert ident["confidence"].min() >= 0.85 and ath.iloc[0]["evidence"] == "career_gap=7"


def test_fifteen_year_old_without_evidence_joins_his_only_candidate() -> None:
    """S5: Hallenschwinget 2012 without any evidence; the career starts in 2013."""
    reg = [portrait(853, "Schmid", "Patrick", "1997-03-01", "Wattwil", "Wattwil", "Nordostschweiz")]
    career = [row(i, f"201{i}-05-01", "Schmid Patrick", residence="Wattwil", tv="NOSV")
              for i in range(3, 7)]
    young = [row(9, "2012-12-01", "Schmid Patrick")]             # aged 15
    child = [row(8, "2010-12-01", "Schmid Patrick")]             # aged 13: cannot be him
    m, ident, ath = resolve(career + young + child, reg)
    assert set(ids(m, career + young)) == {"schmid-patrick-p853"}
    assert ids(m, child) != ids(m, young) and len(ath) == 2
    _, ident, _ = resolve(career + young, reg)
    assert not ident["evidence"].str.contains("ambiguous").any()


def test_fifteen_year_old_without_evidence_goes_to_the_other_candidate() -> None:
    """S5: with a second candidate the age penalty decides."""
    reg = [portrait(853, "Schmid", "Patrick", "1997-03-01", "Wattwil", "Wattwil", "Nordostschweiz")]
    career = [row(i, f"201{i}-05-01", "Schmid Patrick", residence="Wattwil", tv="NOSV")
              for i in range(3, 7)]
    other = [row(20 + i, f"201{i}-06-01", "Schmid Patrick", residence="Thun", tv="BKSV",
                 birth_year="1988") for i in range(1, 5)]
    young = [row(9, "2012-12-01", "Schmid Patrick")]
    m, _, ath = resolve(career + other + young, reg)
    assert ids(m, young) == ids(m, other[:1]) and len(ath) == 2


def test_leaked_youth_row_keeps_its_birth_year() -> None:
    """Reviewer (Reichmuth Marco): an 11-year-old printed with his birth year in an
    active list is the b. 2005 namesake, not the adult; later rows follow his club."""
    reg = [portrait(13416, "Reichmuth", "Marco", "1997-11-06", "Cham-Ennetsee", "Uffikon", "Innerschweiz"),
           portrait(1680, "Reichmuth", "Marco", "2005-05-04", "Einsiedeln", "Rothenthurm", "Innerschweiz")]
    adult = [row(i, f"201{i}-05-01", "Reichmuth Marco", club="Cham-Ennetsee", residence="Cham",
                 tv="ISV") for i in range(4, 9)]
    youth = [row(20, "2016-08-01", "Reichmuth Marco (2005)", club="Einsiedeln",
                 residence="Rothenthurm SZ", tv="ISV", birth_year="2005")]
    later = [row(21 + i, f"202{1 + i}-06-01", "Reichmuth Marco", club="Einsiedeln",
                 residence="Rothenthurm", tv="ISV") for i in range(3)]
    m, _, ath = resolve(adult + youth + later, reg)
    assert set(ids(m, adult)) == {"reichmuth-marco-p13416"}
    assert set(ids(m, youth + later)) == {"reichmuth-marco-p1680"} and len(ath) == 2
    # an impossible age (4) is still not evidence
    typo = [row(30, "2016-09-01", "Reichmuth Marco (2012)", club="Cham-Ennetsee", birth_year="2012")]
    m, _, _ = resolve(adult + typo, reg)
    assert ids(m, typo) == ids(m, adult[:1])


def test_one_portrait_twice_on_one_date_is_not_one_person() -> None:
    """S6: a wrong portrait link must not put one athlete at two festivals in a day."""
    reg = [portrait(5, "Kennel", "Stefan", "1995-01-01", "Rigiverband", "Arth", "Innerschweiz")]
    known = [row(1, "2023-05-01", "Kennel Stefan", portrait_id=5.0, club="Rigiverband", tv="ISV")]
    home = row(2, "2024-05-01", "Kennel Stefan", portrait_id=5.0, club="Rigiverband", tv="ISV")
    away = row(3, "2024-05-01", "Kennel Stefan", portrait_id=5.0, club="Wil", tv="NOSV")
    same_sheet = [row(4, "2025-05-01", "Kennel Stefan", portrait_id=5.0, club="Rigiverband"),
                  row(4, "2025-05-01", "Kennel Stefan", portrait_id=5.0, club="Wil")]
    rows = known + [home, away] + same_sheet
    m, ident, ath = resolve(rows, reg)
    assert m[home["athlete_raw_id"]] != m[away["athlete_raw_id"]]
    assert ids(m, same_sheet)[0] != ids(m, same_sheet)[1]
    assert ids(m, [home, same_sheet[0]]) == ids(m, known) * 2   # the club decides
    assert len(ath) == 2
    frame = pd.DataFrame(rows).assign(a=ids(m, rows))
    assert not frame.duplicated(["a", "fest_date"]).any()
    # the untrusted links are not reported as portrait evidence
    assert ident.loc[away["athlete_raw_id"], "evidence"] != "name+portrait"


def test_resolver_checks_its_own_invariants() -> None:
    two = idn.Cluster(order=0, key="x", rows=[0, 1])
    with pytest.raises(ValueError, match="two rows on one date"):
        idn._check_invariants([two], ["2024-05-01"] * 2, [None, None], ["1-000", "2-000"])
    with pytest.raises(ValueError, match="several portraits"):
        idn._check_invariants([two], ["2024-05-01", "2024-06-01"], [1, 2], ["1-000", "2-000"])
    a, b = idn.Cluster(order=0, key="x", rows=[0]), idn.Cluster(order=1, key="x", rows=[1])
    with pytest.raises(ValueError, match="split over two identities"):
        idn._check_invariants([a, b], ["2024-05-01", "2024-06-01"], [7, 7], ["1-000", "2-000"])


def test_guest_starts_in_another_teilverband_stay_with_the_athlete() -> None:
    """Reviewer (Teilverband-only splits): once an athlete has shown a second
    Teilverband on several rows (double membership), a further row with it is no
    evidence against him; a first, single one still is."""
    home = [row(i, f"201{i}-05-01", "Schwander Severin", residence="Riggisberg", tv="BKSV")
            for i in range(1, 9)]
    away = [row(20 + i, f"201{4 + i}-07-01", "Schwander Severin", residence="Riggisberg", tv="SWSV")
            for i in range(2)]
    guest = [row(30, "2014-08-01", "Schwander Severin", residence="Lausanne & Environs", tv="SWSV")]
    m, _, ath = resolve(home + away + guest)
    assert len(set(m.values())) == 1 and "teilverbaende=BKSV/SWSV" in ath.iloc[0]["evidence"]
    m, _, ath = resolve(home + guest)
    assert len(set(m.values())) == 2 and ath["evidence"].str.contains("-teilverband").all()


@pytest.mark.parametrize(("printed", "key"), [
    ("Lausanne & Environs", "lausanne"), ("Fribourg et environs", "fribourg"),
    ("Estavayer et Env.", "estavayer"), ("Oberriet SG", "oberriet"), ("Mollis", "mollis")])
def test_residence_key(printed: str, key: str) -> None:
    assert idn._residence_key(printed) == key


def test_clubs_are_compared_by_key_and_region_codes_are_no_display_club() -> None:
    """S4: an old club name is the same club; 'TO' (a NOSV region) is evidence only."""
    reg = [portrait(9, "Meier", "Urs", "1990-01-01", "Zurzibiet", "Döttingen", "Nordwestschweiz"),
           portrait(10, "Other", "Guy", "1990-01-01", "Wattwil", "Wattwil", "Nordostschweiz")]
    rows = [row(1, "2012-05-01", "Meier Urs", club="Zurzach", tv="NWSV"),
            row(2, "2018-05-01", "Meier Urs", club="Schwingklub Zurzibiet", tv="NWSV")]
    m, ident, ath = resolve(rows, reg)
    assert set(m.values()) == {"meier-urs-p9"} and set(ident["evidence"]) == {"name+club"}
    rows = [row(1, "2012-05-01", "Forrer Hans", club="TO", tv="NOSV"),
            row(2, "2013-05-01", "Forrer Hans", club="TO", tv="NOSV"),
            row(3, "2014-05-01", "Forrer Hans", club="Wattwil", tv="NOSV")]
    m, ident, ath = resolve(rows, reg)
    assert len(ath) == 1 and ath.iloc[0]["club"] == "Wattwil"
    assert ident["evidence"].tolist()[:2] == ["name+club"] * 2  # still linking evidence
    m, _, ath = resolve(rows[:2], reg)
    assert pd.isna(ath.iloc[0]["club"])


# ------------------------------------------------------------------ spelling variants
def test_accents_umlauts_and_case_share_a_key() -> None:
    rows = [row(1, "2012-05-01", "Rölli Loïc"), row(2, "2013-05-01", "Rolli Loic *"),
            row(3, "2014-05-01", "ROELLI Loïc"), row(4, "2015-05-01", "Rölli Loïc")]
    m, _, ath = resolve(rows)
    assert len(set(m.values())) == 1 and ath.iloc[0]["full_name"] == "Rölli Loïc"


def test_typo_merges_only_with_evidence() -> None:
    main = [row(i, f"201{i}-05-01", "Maridor Loïc", club="Vignoble", residence="Gorgier")
            for i in range(1, 6)]
    typo = [row(7, "2014-07-01", "Marridor Loïc", club="Vignoble", residence="Gorgier")]
    stranger = [row(8, "2014-08-01", "Maridot Loïc", residence="Sion")]
    m, ident, ath = resolve(main + typo + stranger)
    assert ids(m, typo) == ids(m, main[:1]) and ids(m, stranger) != ids(m, main[:1])
    t = ident.loc[typo[0]["athlete_raw_id"]]
    assert t["evidence"] == "variant:edit|name+club" and t["confidence"] == pytest.approx(0.855)
    assert "spellings=maridor loic/marridor loic" in ath.loc[ids(m, main)[0], "evidence"]
    assert ath.loc[ids(m, main)[0], "full_name"] == "Maridor Loïc"


def test_nickname_merges_with_shared_club_only() -> None:
    main = [row(i, f"201{i}-05-01", "Odermatt Michael", club="Stans", tv="ISV") for i in range(1, 5)]
    nick = [row(6, "2013-07-01", "Odermatt Michi", club="Stans", tv="ISV")]
    other = [row(7, "2013-08-01", "Odermatt Michi", tv="NOSV")]
    m, ident, _ = resolve(main + nick + other)
    assert ids(m, nick) == ids(m, main[:1]) and ids(m, other) != ids(m, main[:1])
    assert ident.loc[nick[0]["athlete_raw_id"], "evidence"] == "variant:alias|name+club"


def test_wildcard_name_from_a_lossy_sheet() -> None:
    main = [row(i, f"202{i}-05-01", "Müller Peter", residence="Thun") for i in range(0, 4)]
    wild = [row(9, "2021-06-01", "M?ller Peter")]
    m, ident, _ = resolve(main + wild)
    assert ids(m, wild) == ids(m, main[:1])
    assert ident.loc[wild[0]["athlete_raw_id"], "evidence"].startswith("variant:wildcard|")
    # ligature: '?' stands for 'ff'
    rows = [row(1, "2021-05-01", "Nyffenegger Florian"), row(2, "2021-06-01", "Ny?enegger Florian")]
    assert len(set(resolve(rows)[0].values())) == 1
    # two possible targets and no evidence: the lossy row stays on its own
    moeller = [row(10 + i, f"202{i}-07-01", "Möller Peter", residence="Bern") for i in range(0, 4)]
    m, _, _ = resolve(main + moeller + wild)
    assert len(set(m.values())) == 3
    # ... unless evidence decides
    wild_ev = [row(9, "2021-06-01", "M?ller Peter", residence="Bern")]
    m, _, _ = resolve(main + moeller + wild_ev)
    assert ids(m, wild_ev) == ids(m, moeller[:1])
    # same festival: the wildcard row is somebody else
    same = [row(2, "2022-05-01", "M?ller Peter")]
    m, _, _ = resolve(main + same)
    assert ids(m, same) != ids(m, main[:1])


def test_swapped_order_and_spacing() -> None:
    rows = [row(1, "2014-05-01", "von Büren Stephan", club="Thun"),
            row(2, "2015-05-01", "Stephan von Büren", club="Thun"),
            row(3, "2016-05-01", "Vonbüren Stephan")]
    m, ident, _ = resolve(rows)
    assert len(set(m.values())) == 1
    assert sorted(e.split("|")[0] for e in ident["evidence"][1:]) == ["variant:spacing", "variant:swap"]


def test_proven_distinct_near_names_are_never_merged() -> None:
    simon = [row(i, f"201{i}-05-01", "Stucki Simon", club="Siehen", residence="Röthenbach")
             for i in range(1, 5)]
    timon = [row(1, "2011-05-01", "Stucki Timon", club="Siehen", residence="Röthenbach"),
             row(8, "2015-05-01", "Stucki Timon", club="Siehen", residence="Röthenbach")]
    m, _, ath = resolve(simon + timon)
    assert len(set(ids(m, simon))) == 1 and len(set(ids(m, timon))) == 1
    assert ids(m, simon)[0] != ids(m, timon)[0] and len(ath) == 2


def test_two_established_first_names_need_a_birth_year() -> None:
    """Brothers share club and village; Remo / Reto never meet in this data."""
    names_ = [f"{s} {f}" for s in ("Kälin", "Meier", "Suter") for f in ("Remo", "Reto")]
    filler = [row(100 + i, f"2012-0{i + 1}-01", n, residence=f"Ort {i}") for i, n in enumerate(names_[2:])]
    remo = [row(i, f"201{i}-05-01", "Kälin Remo", club="Einsiedeln", residence="Egg") for i in range(1, 5)]
    reto = [row(9, "2015-05-01", "Kälin Reto", club="Einsiedeln", residence="Egg")]
    m, _, _ = resolve(filler + remo + reto)
    assert ids(m, reto) != ids(m, remo[:1])
    remo[0]["birth_year"], reto[0]["birth_year"] = "1995", "1995"  # e.g. a misread first name
    m, _, _ = resolve(filler + remo + reto)
    assert ids(m, reto) == ids(m, remo[:1])


# ------------------------------------------------------------------ robustness
def test_garbage_names_and_overflow_rows() -> None:
    rows = [row(1, "2014-05-01", "K"), row(1, "2014-05-01", "K"), row(2, "2015-05-01", "K"),
            row(3, "2016-05-01", "Muster Hans", club="Thun"),
            # merged entry blocks: this row's evidence belongs partly to somebody else
            row(4, "2017-05-01", "Muster Hans", club="Aarau", birth_year="1960", tv="NWSV",
                flags="entries_overflow")]
    m, ident, ath = resolve(rows)
    assert len(set(ids(m, rows[:3]))) == 3 and (ident["evidence"][:3] == "not_a_name").all()
    assert (ident["confidence"][:3] == 0).all()
    assert ids(m, rows[3:4]) == ids(m, rows[4:])
    hans = ath.loc[m[rows[3]["athlete_raw_id"]]]
    assert hans["club"] == "Thun" and pd.isna(hans["birth_year"])
    assert ident.loc[rows[4]["athlete_raw_id"], "evidence"] == "name"


def _mixed() -> tuple[list[dict], list[dict]]:
    reg = [portrait(1, "Wüthrich", "Jonas", "2001-01-01", "Trub", "Trub", "Bern"),
           portrait(2, "Wüthrich", "Jonas", "2005-01-01", "Zäziwil", "Signau", "Bern")]
    rows = [row(1, "2018-05-01", "Wüthrich Jonas (2001)", club="Trub"),
            row(1, "2018-05-01", "Wüthrich Jonas (2001)", club="Zäziwil", residence="Bowil"),
            row(2, "2021-05-01", "Wüthrich Jonas", club="Zäziwil", residence="Signau"),
            row(3, "2022-05-01", "Wuethrich Jonas", club="Trub"),
            row(4, "2023-05-01", "Wüthrich Jonas (1)", portrait_id=1.0, club="Trub"),
            row(5, "2023-06-01", "Wüthrich Jonas", residence="Bowil"),
            row(6, "2012-06-01", "Wüthrich Jonas"),
            row(7, "2019-06-01", "W?thrich Jonas", club="Trub"),
            row(8, "2019-07-01", "Muster Hans"), row(9, "2020-07-01", "Hans Muster")]
    return rows, reg


def test_deterministic_and_independent_of_input_order() -> None:
    rows, reg = _mixed()
    m1, i1, a1 = resolve(rows, reg)
    m2, i2, a2 = resolve(list(reversed(rows)), list(reversed(reg)))
    m3, _, _ = resolve(rows, reg)
    assert m1 == m2 == m3
    pd.testing.assert_frame_equal(i1.sort_index(), i2.sort_index())
    pd.testing.assert_frame_equal(a1, a2)
    assert len(a1) == 5  # 2001 Trub, 2001 Zäziwil/Bowil, 2005 Zäziwil/Signau, 2012 (age), Muster


def test_ids_survive_a_new_festival() -> None:
    rows, reg = _mixed()
    before, _, _ = resolve(rows, reg)
    later = [row(50, "2024-05-01", "Wüthrich Jonas (2)", portrait_id=2.0, club="Zäziwil"),
             row(50, "2024-05-01", "Neuling Max"), row(51, "2024-06-01", "Muster Hans")]
    after, _, _ = resolve(rows + later, reg)
    assert {k: after[k] for k in before} == before


def test_resolution_passes_validation_and_default_is_the_evidence_resolver() -> None:
    rows, reg = _mixed()
    raw = pd.DataFrame(rows)
    res = idn.EvidenceResolver().resolve(cl.ResolverInput(
        raw=raw, bouts=pd.DataFrame(columns=["athlete_a_id", "athlete_b_id"]),
        portraits=pd.DataFrame(reg)))
    out = cl.validate_resolution(res, raw)
    assert len(out.identity) == len(raw) and out.identity["confidence"].between(0, 1).all()
    assert out.identity["evidence"].notna().all() and out.athletes["evidence"].notna().all()
    assert isinstance(cl.default_resolver(), idn.EvidenceResolver)
    assert cl.default_resolver().name == "evidence" and cl.BaselineResolver().name == "baseline"


@pytest.mark.parametrize(("a", "b", "kind"), [
    ("m?ller peter", "muller peter", "wildcard"), ("vonlanthen kurt", "von lanthen kurt", "spacing"),
    ("hans muster", "muster hans", "swap"), ("odermatt michi", "odermatt michal", "alias"),
    ("stucki simon", "stucki timon", "edit"), ("walther marcel", "walthert marcel", "edit")])
def test_variant_kind(a: str, b: str, kind: str) -> None:
    assert idn.variant_kind(a, b) == kind


# ------------------------------------------------------------------ saved fixtures
def test_sample_fixtures_invariants(tmp_path: Path) -> None:
    """Real sheets, ranking lists and portraits of the --sample dataset."""
    data = tmp_path / "data"
    assert cli.main(["clean", "--sample", "--data-dir", str(data)]) == 0
    proc = data / "processed"
    ident = pq.read_table(proc / "identity_map.parquet").to_pandas()
    ath = pq.read_table(proc / "athletes.parquet").to_pandas()
    rejects = pq.read_table(proc / "bout_rejects.parquet").to_pandas()
    assert set(ident["resolver"]) == {"evidence"} and ident["athlete_id"].notna().all()
    assert len(rejects) == 0                                    # no self-bouts, nothing unmapped
    assert not ident.duplicated(["athlete_id", "fest_id"]).any()  # one row per festival
    linked = ident[ident["portrait_slug"].notna()]
    assert (linked.groupby("athlete_id")["portrait_slug"].nunique() == 1).all()
    assert (linked.groupby("portrait_slug")["athlete_id"].nunique() == 1).all()
    assert ident["evidence"].notna().all() and ident["confidence"].between(0, 1).all()
    # five festivals 2011-2025: long gaps alone must not split a unique name
    assert len(ath) < 560
    assert (ath["n_festivals"] >= 2).sum() > 40
    glarner = ath[ath["full_name"] == "Glarner Matthias"]  # Brünig 2011 + ESAF 2019
    assert len(glarner) == 1 and glarner.iloc[0]["n_festivals"] == 2


# ------------------------------------------------------------------ seasons before 2011 (Phase 10)
def test_history_before_2011_does_not_rename_an_athlete() -> None:
    later = [row(10 + i, f"201{i + 1}-05-01", "Muster Hans", residence="Thun") for i in range(4)]
    assert set(resolve(later)[0].values()) == {f"muster-hans-{later[0]['athlete_raw_id']}"}
    # seasons added before the published ones join him and leave his id alone
    earlier = [row(1, "2005-05-01", "Muster Hans", residence="Thun"),
               row(2, "2008-06-01", "Muster Hans *")]
    m, ident, ath = resolve(earlier + later)
    assert set(m.values()) == {f"muster-hans-{later[0]['athlete_raw_id']}"}
    assert ident.loc[earlier[0]["athlete_raw_id"], "evidence"] == "name+residence"
    assert len(ath) == 1


def test_athlete_seen_only_before_2011_takes_his_earliest_row() -> None:
    rows = [row(1, "2004-05-01", "Altmeister Karl"), row(2, "2006-05-01", "Altmeister Karl"),
            row(3, "2010-09-01", "Altmeister Karl")]
    m, _, _ = resolve(rows)
    assert set(m.values()) == {f"altmeister-karl-{rows[0]['athlete_raw_id']}"}
    assert idn.ID_ANCHOR_FROM == "2011-01-01"


def test_name_only_row_of_an_old_sheet_joins_a_unique_name() -> None:
    """An opponent the old sheet does not print: a row with a name and nothing else."""
    career = [row(10 + i, f"200{6 + i}-05-01", "Muster Hans", residence="Thun") for i in range(4)]
    ghost = [row(1, "2005-08-01", "Muster Hans", flags="unlisted")]
    m, ident, _ = resolve(ghost + career)
    assert len(set(m.values())) == 1
    assert ident.loc[ghost[0]["athlete_raw_id"], "evidence"] == "name"


def test_name_only_row_between_two_namesakes_is_flagged() -> None:
    a = [row(10 + i, f"200{6 + i}-05-01", "Muster Hans", residence="Thun", tv="BKSV")
         for i in range(3)]
    b = [row(20 + i, f"200{6 + i}-05-01", "Muster Hans", residence="Chur", tv="NOSV")
         for i in range(3)]
    ghost = [row(1, "2005-08-01", "Muster Hans", flags="unlisted")]
    m, ident, ath = resolve(ghost + a + b)
    assert len(set(ids(m, a))) == 1 and len(set(ids(m, b))) == 1 and ids(m, a) != ids(m, b)
    g = ident.loc[ghost[0]["athlete_raw_id"]]
    assert "ambiguous" in g["evidence"] and g["confidence"] <= 0.4
    assert len(ath) == 2


def test_old_rows_do_not_join_an_athlete_who_was_a_child_then() -> None:
    """The age rule keeps father and son (or two generations of a name) apart."""
    son = [row(10 + i, f"201{4 + i}-05-01", "Muster Hans (1998)", club="Thun") for i in range(4)]
    father = [row(1, "2005-05-01", "Muster Hans"), row(2, "2006-05-01", "Muster Hans")]
    m, _, ath = resolve(father + son)
    assert len(set(ids(m, son))) == 1 and len(set(ids(m, father))) == 1
    assert ids(m, father)[0] != ids(m, son)[0] and len(ath) == 2
    assert ids(m, son)[0] == f"muster-hans-{son[0]['athlete_raw_id']}"


def test_same_date_collision_in_an_old_season_splits_the_later_rows_by_evidence() -> None:
    old = [row(1, "2006-05-01", "Arnold Raphael", residence="Bürglen", tv="ISV"),
           row(2, "2006-05-01", "Arnold Raphael", residence="Triengen", tv="ISV")]
    new = [row(10, "2012-05-01", "Arnold Raphael", residence="Bürglen", tv="ISV"),
           row(11, "2013-05-01", "Arnold Raphael", residence="Triengen", tv="ISV"),
           row(12, "2014-05-01", "Arnold Raphael", residence="Bürglen", tv="ISV")]
    m, _, ath = resolve(old + new)
    a, b = ids(m, old)
    assert a != b and ids(m, new) == [a, b, a] and len(ath) == 2
    # without the old season the three later rows have nothing that separates them by date
    assert len(set(resolve(new)[0].values())) <= 2


def test_spellings_of_published_seasons_are_judged_on_those_seasons() -> None:
    """More seasons mean more name keys per token; that alone must not turn a typo of a
    published athlete into an "established" second name (Burkart / Burkhart)."""
    main = [row(10 + i, f"201{3 + i}-05-01", "Burkart Simon", club="Binningen", tv="NWSV")
            for i in range(4)]
    typo = [row(5, "2012-03-25", "Burkhart Simon", club="Binningen", tv="NWSV")]
    m, _, _ = resolve(main + typo)
    assert ids(m, typo) == ids(m, main[:1])
    # other people of the old seasons carry both tokens
    crowd = [row(100 + i, f"200{5 + i % 5}-0{1 + i}-01", n, residence=f"Ort {i}")
             for i, n in enumerate(["Burkart Hans", "Burkart Peter", "Burkhart Urs",
                                    "Burkhart Karl", "Burkhart Fritz", "Burkart Josef"])]
    m, _, _ = resolve(crowd + main + typo)
    assert ids(m, typo) == ids(m, main[:1])
    # two spellings that exist only in the old seasons get the stricter count
    old_main = [row(200 + i, f"200{5 + i}-05-01", "Burkart Simon", club="Binningen", tv="NWSV")
                for i in range(4)]
    old_typo = [row(210, "2006-03-25", "Burkhart Simon", club="Binningen", tv="NWSV")]
    m, _, _ = resolve(crowd + old_main + old_typo)
    assert ids(m, old_typo) != ids(m, old_main[:1])


def test_name_alone_does_not_bridge_a_long_gap_into_the_old_seasons() -> None:
    """2005 and then nothing until 2014: the name is all the two careers share."""
    old = [row(1, "2005-05-01", "Muster Hans"), row(2, "2005-08-01", "Muster Hans", flags="unlisted")]
    new = [row(10 + i, f"201{4 + i}-05-01", "Muster Hans", residence="Thun") for i in range(3)]
    m, ident, ath = resolve(old + new)
    assert len(set(ids(m, old))) == 1 and len(set(ids(m, new))) == 1
    assert ids(m, old)[0] != ids(m, new)[0] and len(ath) == 2
    assert ids(m, new)[0] == f"muster-hans-{new[0]['athlete_raw_id']}"
    assert ids(m, old)[0] == f"muster-hans-{old[0]['athlete_raw_id']}"
    # evidence beyond the name bridges the gap
    old_ev = [row(1, "2005-05-01", "Muster Hans", residence="Thun")]
    m, _, ath = resolve(old_ev + new)
    assert len(set(m.values())) == 1 and "career_gap=8" in ath.iloc[0]["evidence"]
    # a short hole is no gap (Kranzfeste only before 2011: careers have holes)
    near = [row(1, "2009-05-01", "Muster Hans")]
    assert len(set(resolve(near + new)[0].values())) == 1
    # and a career that reaches 2011 is not "before 2011": the published rule stays
    reach = [row(1, "2011-05-01", "Muster Hans")]
    later = [row(20 + i, f"201{8 + i}-05-01", "Muster Hans", residence="Thun") for i in range(2)]
    assert len(set(resolve(reach + later)[0].values())) == 1
    a, b = idn.Cluster(0, "x", years={2004, 2005}), idn.Cluster(1, "x", years={2012})
    assert idn.history_gap(a, b, []) and idn.history_gap(b, a, ["teilverband", "opponents"])
    assert not idn.history_gap(a, b, ["residence"])
    assert not idn.history_gap(idn.Cluster(0, "x", years={2007}), b, [])       # 4 seasons
    assert not idn.history_gap(idn.Cluster(0, "x", years={2005, 2011}), idn.Cluster(1, "x", years={2019}), [])
