"""SQLite staging database (``data/schwingen.db``): schema + persistence helpers.

``festivals`` follows the spec's ``Festival`` entity (``fest_id``, ``name``,
``date``, ``category``, ``location``) plus source metadata needed later
(bout PDF URLs, ESV reference id, kind flags). ``fest_id`` is the stable
schlussgang.ch node id (``drupal_internal__nid``).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1

CATEGORIES = ("ESAF", "Bergkranz", "Teilverband", "Kantonal", "Gauverband", "Regional")
KINDS = ("active", "youth", "women", "non_competition")

_cat_list = ", ".join(f"'{c}'" for c in CATEGORIES)
_kind_list = ", ".join(f"'{k}'" for k in KINDS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS festivals (
    fest_id             INTEGER PRIMARY KEY,   -- schlussgang node id
    name                TEXT    NOT NULL,
    date                TEXT    NOT NULL,      -- ISO yyyy-mm-dd
    category            TEXT CHECK (category IS NULL OR category IN ({_cat_list})),
    location            TEXT,
    kind                TEXT    NOT NULL CHECK (kind IN ({_kind_list})),
    cancelled           INTEGER NOT NULL DEFAULT 0 CHECK (cancelled IN (0, 1)),
    source_category     TEXT,                  -- schlussgang tag, e.g. 'Kantonal-/Gaufest'
    source_category_tid INTEGER,
    association         TEXT,                  -- schlussgang association term, e.g. 'Bern'
    esv_id              INTEGER,               -- ESV Anlass id (reference only, never fetched)
    event_type          TEXT,                  -- 'Aktivschwinger' / 'Jungschwinger' / ...
    participant_count   INTEGER,
    url                 TEXT    NOT NULL,      -- https://www.schlussgang.ch/event/<slug>
    statistic_pdf_url   TEXT,                  -- bout source for Phase 2 (may be NULL)
    ranking_pdf_url     TEXT,
    first_seen          TEXT    NOT NULL,
    last_seen           TEXT    NOT NULL,
    CHECK (kind <> 'active' OR category IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_festivals_date ON festivals(date);
CREATE INDEX IF NOT EXISTS idx_festivals_category ON festivals(category);
"""


@dataclass(frozen=True)
class Festival:
    fest_id: int
    name: str
    date: str
    category: str | None
    location: str | None
    kind: str = "active"
    cancelled: bool = False
    source_category: str | None = None
    source_category_tid: int | None = None
    association: str | None = None
    esv_id: int | None = None
    event_type: str | None = None
    participant_count: int | None = None
    url: str = ""
    statistic_pdf_url: str | None = None
    ranking_pdf_url: str | None = None

    def __post_init__(self) -> None:
        if self.category is not None and self.category not in CATEGORIES:
            raise ValueError(f"invalid category {self.category!r}")
        if self.kind not in KINDS:
            raise ValueError(f"invalid kind {self.kind!r}")
        if self.kind == "active" and self.category is None:
            raise ValueError(f"active festival {self.fest_id} needs a category")
        _dt.date.fromisoformat(self.date)  # raises on bad dates

    @property
    def year(self) -> int:
        return int(self.date[:4])


FESTIVAL_COLUMNS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(Festival))


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"database schema v{version} is newer than code v{SCHEMA_VERSION}")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _row_to_festival(row: sqlite3.Row) -> Festival:
    values = {c: row[c] for c in FESTIVAL_COLUMNS}
    values["cancelled"] = bool(values["cancelled"])
    return Festival(**values)


def load_festivals(conn: sqlite3.Connection) -> dict[int, Festival]:
    cols = ", ".join(FESTIVAL_COLUMNS)
    rows = conn.execute(f"SELECT {cols} FROM festivals ORDER BY date, fest_id")
    return {r["fest_id"]: _row_to_festival(r) for r in rows}


def upsert_festivals(conn: sqlite3.Connection, festivals: Iterable[Festival],
                     now: str | None = None) -> UpsertStats:
    """Insert new festivals, update changed ones, only touch ``last_seen`` otherwise."""
    now = now or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    known = load_festivals(conn)
    stats = UpsertStats()
    cols = ", ".join(FESTIVAL_COLUMNS)
    marks = ", ".join("?" for _ in FESTIVAL_COLUMNS)
    sets = ", ".join(f"{c} = ?" for c in FESTIVAL_COLUMNS if c != "fest_id")
    with conn:
        for f in festivals:
            values = [getattr(f, c) for c in FESTIVAL_COLUMNS]
            values = [int(v) if isinstance(v, bool) else v for v in values]
            old = known.get(f.fest_id)
            if old is None:
                conn.execute(
                    f"INSERT INTO festivals ({cols}, first_seen, last_seen) "
                    f"VALUES ({marks}, ?, ?)", [*values, now, now])
                stats.inserted += 1
            elif old != f:
                conn.execute(f"UPDATE festivals SET {sets}, last_seen = ? WHERE fest_id = ?",
                             [*values[1:], now, f.fest_id])
                stats.updated += 1
            else:
                conn.execute("UPDATE festivals SET last_seen = ? WHERE fest_id = ?",
                             [now, f.fest_id])
                stats.unchanged += 1
            known[f.fest_id] = f
    return stats
