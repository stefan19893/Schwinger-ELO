"""athlete_evidence / clubs built from ranking entries, portraits and festivals (offline)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from src.db import Festival, connect, upsert_festivals
from src.scraper import evidence as ev
from src.scraper import portraits as P


def fest(fid: int, name: str, category: str = "Regional", **kw: Any) -> Festival:
    return Festival(fest_id=fid, name=name, date="2024-06-01", category=category,
                    location=None, url=f"https://www.schlussgang.ch/event/f{fid}", **kw)


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setattr(ev, "MIN_CLUB_OBS", 3)
    c = connect(tmp_path / "s.db")
    upsert_festivals(c, [fest(1, "Luzerner Kantonalschwingfest Rothenburg 2024", "Kantonal"),
                         fest(2, "Eidgenössisches Schwing- und Älplerfest 2024", "ESAF",
                              eidg_type="ESAF"),
                         fest(3, "Frühjahrsschwinget Cham 2024")])
    return c


def athletes(conn: sqlite3.Connection, fid: int, rows: list[tuple[str, Any, Any, Any]]) -> None:
    """rows: (name, birth_year, association, place) as printed in the statistic sheet."""
    conn.executemany(
        "INSERT INTO athletes_raw (athlete_raw_id, fest_id, idx, name_raw, name, name_key, "
        "name_base_key, birth_year, association, place) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(f"{fid}-{i:03d}", fid, i, n, n, n.lower(), n.lower(), by, assoc, place)
         for i, (n, by, assoc, place) in enumerate(rows)])


def rankings(conn: sqlite3.Connection, fid: int, rows: list[tuple[Any, ...]]) -> None:
    """rows: (linked athlete idx or None, residence, birth_year, assoc_code, club_raw)."""
    conn.executemany(
        "INSERT INTO ranking_entries (fest_id, idx, name_raw, name, name_key, residence, "
        "birth_year, assoc_code, club_raw, athlete_raw_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(fid, i, "x", "x", "x", res, by, code, club,
          None if a is None else f"{fid}-{a:03d}")
         for i, (a, res, by, code, club) in enumerate(rows)])


def evidence(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return {r["athlete_raw_id"]: dict(r) for r in conn.execute("SELECT * FROM athlete_evidence")}


def test_one_row_per_raw_athlete_with_source_priority(conn: sqlite3.Connection) -> None:
    athletes(conn, 1, [("Wicki Joel", None, None, None), ("Gast Hans", None, None, None),
                       ("Ohne Liste", "1999", "NOS", "Wil SG"), ("Nur Fest", None, None, None),
                       ("Klub Nurklub", None, None, None)])
    athletes(conn, 2, [("Wicki Joel", None, None, None), ("Fremd Paul", None, None, None)])
    rankings(conn, 1, [(0, "Sörenberg", 1997, "LU", "Entlebuch"),
                       (1, "Thun", None, "GST", "Thun und Umgebung"),
                       (None, "Root", None, "LU", "Entlebuch"),          # unlinked: club vote only
                       (4, "Hasle LU", None, None, "Schwingklub Entlebuch")])
    rankings(conn, 2, [(0, "Sörenberg", None, None, "LUEntlebuch"),      # code not split off
                       (1, "Bulle", None, None, "La Gruyère ARLS")])
    rankings(conn, 3, [(None, "Thun", None, "BO", "Thun und Umgebung")] * 3)
    conn.commit()
    rep = ev.build_evidence(conn)
    got = evidence(conn)
    assert rep.rows == len(got) == 7  # every athletes_raw row, linked or not

    joel = got["1-000"]
    assert (joel["residence"], joel["birth_year"], joel["assoc_code"], joel["club_raw"],
            joel["club_key"], joel["club"], joel["sub_association"], joel["sub_assoc_source"],
            joel["ranking_idx"]) == ("Sörenberg", 1997, "LU", "Entlebuch", "entlebuch",
                                     "Entlebuch", "ISV", "code", 0)
    # guest code 'GST' is no association: Teilverband comes from the club's vote (BKSV),
    # not from the organising festival (ISV)
    guest = got["1-001"]
    assert (guest["club"], guest["sub_association"], guest["sub_assoc_source"]) == (
        "Thun und Umgebung", "BKSV", "club")
    # no ranking entry: statistic-sheet birth year / association / place are used
    alone = got["1-002"]
    assert (alone["residence"], alone["birth_year"], alone["club"], alone["sub_association"],
            alone["sub_assoc_source"]) == ("Wil SG", 1999, None, "NOSV", "code")
    # nothing at all at a Kantonalfest: the organiser's Teilverband, marked as weakest source
    assert (got["1-003"]["sub_association"], got["1-003"]["sub_assoc_source"]) == ("ISV", "festival")
    assert (got["1-004"]["club_key"], got["1-004"]["sub_assoc_source"]) == ("entlebuch", "club")
    # ESAF: no organising Teilverband; unknown club with a trailing association code
    assert got["2-000"]["club_key"] is None and got["2-000"]["club_raw"] == "LUEntlebuch"
    assert (got["2-000"]["sub_association"], got["2-000"]["sub_assoc_source"]) == (None, None)
    paul = got["2-001"]
    assert (paul["club"], paul["sub_association"], paul["sub_assoc_source"]) == (None, "SWSV", "code")

    clubs = {r[0]: r[1:] for r in conn.execute(
        "SELECT club_key, name, sub_association, n_obs, conflict, votes FROM clubs")}
    assert clubs == {"entlebuch": ("Entlebuch", "ISV", 3, 0, "ISV:9"),
                     "thun": ("Thun und Umgebung", "BKSV", 4, 0, "BKSV:9,ISV:1")}
    assert rep.clubs == 2 and rep.with_club == 3
    assert rep.unresolved_clubs == {"LUEntlebuch": 1, "La Gruyère ARLS": 1}
    assert rep.with_sub == {"code": 3, "club": 2, "festival": 1, "none": 1}


def test_portrait_evidence_and_rebuild(conn: sqlite3.Connection) -> None:
    athletes(conn, 3, [("Wicki Joel", None, None, None), ("Muster Hans", None, None, None)])
    conn.commit()
    joel = P.Portrait(775, "joel-wicki", "https://www.schlussgang.ch/portraet/joel-wicki",
                      "Wicki Joel, 1997-02-20 (24642)", "Wicki", "Joel", P.name_key("Wicki Joel"), "1997-02-20",
                      "Sörenberg", 24642, 108, "Entlebuch", 200, "Innerschweiz", "Luzern", "Aktive",
                      None)
    P.store_portraits(conn, {775: joel}, {(3, 775)})
    rep = ev.build_evidence(conn)
    got = evidence(conn)
    assert (got["3-000"]["portrait_id"], got["3-000"]["portrait_slug"]) == (775, "joel-wicki")
    # Regional festival without association: the portrait's Teilverband is the only evidence
    assert (got["3-000"]["sub_association"], got["3-000"]["sub_assoc_source"]) == ("ISV", "portrait")
    assert got["3-000"]["club"] is None  # the portrait's *current* club is not copied to old rows
    assert got["3-001"]["portrait_id"] is None and rep.with_portrait == 1
    # the portrait club (ESV id 200) is a canonical club even without ranking observations
    assert [tuple(r) for r in conn.execute("SELECT name, esv_id, sub_association FROM clubs")] == [
        ("Entlebuch", 200, "ISV")]
    # rebuilding replaces both tables
    conn.execute("DELETE FROM athletes_raw WHERE athlete_raw_id = '3-001'")
    assert ev.build_evidence(conn).rows == 1
    assert conn.execute("SELECT COUNT(*) FROM athlete_evidence").fetchone()[0] == 1
