"""Tests for the SQLite festivals schema and upsert helper."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from src.db import SCHEMA_VERSION, Festival, connect, load_festivals, upsert_festivals

F = Festival(fest_id=26400, name="Brünig-Schwinget 2011", date="2011-07-31",
             category="Bergkranz", location="Brünig-Passhöhe",
             url="https://www.schlussgang.ch/event/bruenig-schwinget-2011",
             statistic_pdf_url="https://www.schlussgang.ch/x/stat_bruenig.pdf")


def test_schema_created_with_version(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db" / "schwingen.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(festivals)")}
    assert {"fest_id", "name", "date", "category", "location"} <= cols  # spec Festival


def test_insert_update_unchanged(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    s1 = upsert_festivals(conn, [F], now="2026-01-01T00:00:00+00:00")
    assert (s1.inserted, s1.updated, s1.unchanged) == (1, 0, 0)
    s2 = upsert_festivals(conn, [F], now="2026-02-01T00:00:00+00:00")
    assert (s2.inserted, s2.updated, s2.unchanged) == (0, 0, 1)
    changed = replace(F, participant_count=120)
    s3 = upsert_festivals(conn, [changed], now="2026-03-01T00:00:00+00:00")
    assert (s3.inserted, s3.updated, s3.unchanged) == (0, 1, 0)
    assert load_festivals(conn)[F.fest_id] == changed
    first, last = conn.execute("SELECT first_seen, last_seen FROM festivals").fetchone()
    assert first.startswith("2026-01") and last.startswith("2026-03")


def test_duplicate_ids_in_one_batch_count_once(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    s = upsert_festivals(conn, [F, F])
    assert (s.inserted, s.unchanged) == (1, 1)
    assert len(load_festivals(conn)) == 1


def test_bool_roundtrip(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    g = replace(F, fest_id=1, cancelled=True)
    upsert_festivals(conn, [g])
    assert load_festivals(conn)[1].cancelled is True


@pytest.mark.parametrize("kw", [
    {"category": "Schwingfest"},
    {"kind": "junior"},
    {"category": None},  # active festivals need a category
    {"date": "2011-13-01"},
])
def test_festival_validation(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(F, **kw)


def test_non_active_may_lack_category() -> None:
    assert replace(F, kind="non_competition", category=None).category is None


def test_db_check_constraint_rejects_bad_category(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO festivals (fest_id, name, date, category, kind, url, "
                     "first_seen, last_seen) VALUES (1,'x','2020-01-01','Foo','active','u','n','n')")


def test_refuses_newer_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError):
        connect(tmp_path / "s.db")


def test_eidg_type_validation() -> None:
    esaf = replace(F, category="ESAF", eidg_type="Kilchberg")
    assert esaf.eidg_type == "Kilchberg"
    with pytest.raises(ValueError):
        replace(F, category="ESAF")  # ESAF needs an eidg_type
    with pytest.raises(ValueError):
        replace(F, eidg_type="ESAF")  # eidg_type only for category ESAF
    with pytest.raises(ValueError):
        replace(F, category="ESAF", eidg_type="Brünig")


def test_migrates_v1_database(tmp_path: Path) -> None:
    """A v1 db (no eidg_type) gains the column; old ESAF rows become eidg_type='ESAF'."""
    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE festivals (
            fest_id INTEGER PRIMARY KEY, name TEXT NOT NULL, date TEXT NOT NULL,
            category TEXT, location TEXT, kind TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
            source_category TEXT, source_category_tid INTEGER, association TEXT, esv_id INTEGER,
            event_type TEXT, participant_count INTEGER, url TEXT NOT NULL,
            statistic_pdf_url TEXT, ranking_pdf_url TEXT, first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL);
        INSERT INTO festivals (fest_id, name, date, category, kind, url, first_seen, last_seen)
        VALUES (1, 'ESAF Zug 2019', '2019-08-24', 'ESAF', 'active', 'u', 'n', 'n'),
               (2, 'Kilchberg 2021', '2021-09-25', 'Bergkranz', 'active', 'u', 'n', 'n');
        PRAGMA user_version = 1;
    """)
    raw.close()
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
    fests = load_festivals(conn)
    assert fests[1].eidg_type == "ESAF" and fests[2].eidg_type is None
    # re-derived row (as `crawl --offline` does) updates cleanly
    s = upsert_festivals(conn, [replace(fests[2], category="ESAF", eidg_type="Kilchberg")])
    assert s.updated == 1 and load_festivals(conn)[2].category == "ESAF"
    connect(path).close()  # idempotent


def test_db_rejects_eidg_type_on_non_esaf(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO festivals (fest_id, name, date, category, eidg_type, kind, "
                     "url, first_seen, last_seen) VALUES "
                     "(1,'x','2020-01-01','Bergkranz','Kilchberg','active','u','n','n')")
