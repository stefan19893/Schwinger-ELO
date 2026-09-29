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

SCHEMA_VERSION = 3  # v2: festivals.eidg_type; v3: event_flags/elo_eligible + parse tables

CATEGORIES = ("ESAF", "Bergkranz", "Teilverband", "Kantonal", "Gauverband", "Regional")
KINDS = ("active", "youth", "women", "non_competition")
# Festivals with eidgenössischem Charakter; all share category 'ESAF' (K=48).
EIDG_TYPES = ("ESAF", "Kilchberg", "Unspunnen", "Jubilaeum")

_cat_list = ", ".join(f"'{c}'" for c in CATEGORIES)
_kind_list = ", ".join(f"'{k}'" for k in KINDS)
_eidg_list = ", ".join(f"'{e}'" for e in EIDG_TYPES)
_EIDG_COLUMN = (f"eidg_type TEXT CHECK (eidg_type IS NULL OR "
                f"(eidg_type IN ({_eidg_list}) AND category = 'ESAF'))")
# comma-separated borderline markers: team, jungaktive, ausland, hallenschwinget
_FLAGS_COLUMN = "event_flags TEXT NOT NULL DEFAULT ''"
_ELIGIBLE_COLUMN = "elo_eligible INTEGER NOT NULL DEFAULT 1 CHECK (elo_eligible IN (0, 1))"
EVENT_FLAGS = ("team", "jungaktive", "ausland", "hallenschwinget")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS festivals (
    fest_id             INTEGER PRIMARY KEY,   -- schlussgang node id
    name                TEXT    NOT NULL,
    date                TEXT    NOT NULL,      -- ISO yyyy-mm-dd
    category            TEXT CHECK (category IS NULL OR category IN ({_cat_list})),
    {_EIDG_COLUMN},  -- which eidg. festival; 'ESAF' = the real ESAF
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
    {_FLAGS_COLUMN},
    {_ELIGIBLE_COLUMN},
    CHECK (kind <> 'active' OR category IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_festivals_date ON festivals(date);
CREATE INDEX IF NOT EXISTS idx_festivals_category ON festivals(category);

-- Phase 2: one row per parsed festival (also for failures / missing PDFs)
CREATE TABLE IF NOT EXISTS festival_parse (
    fest_id         INTEGER PRIMARY KEY REFERENCES festivals(fest_id),
    pdf_url         TEXT,
    pdf_sha256      TEXT,
    parser_version  INTEGER NOT NULL,
    layout          TEXT,
    status          TEXT    NOT NULL,   -- ok|partial|failed|header_mismatch|no_pdf|pdf_not_cached|pdf_error
    header_check    TEXT,
    n_athletes      INTEGER NOT NULL DEFAULT 0,
    n_entries       INTEGER NOT NULL DEFAULT 0,
    n_bouts         INTEGER NOT NULL DEFAULT 0,
    n_rejects       INTEGER NOT NULL DEFAULT 0,
    youth_blocks    INTEGER NOT NULL DEFAULT 0,
    parsed_at       TEXT    NOT NULL
);

-- raw athletes as printed on one sheet (identity resolution in Phase 3)
CREATE TABLE IF NOT EXISTS athletes_raw (
    athlete_raw_id  TEXT PRIMARY KEY,   -- "<fest_id>-<idx>"
    fest_id         INTEGER NOT NULL REFERENCES festivals(fest_id),
    idx             INTEGER NOT NULL,
    rank            TEXT,
    name_raw        TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    name_key        TEXT    NOT NULL,
    name_base_key   TEXT    NOT NULL,
    status          TEXT,               -- Kranz status: '*'..'***' or E/K/EK/TK
    mark            TEXT,               -- '*' award / '°' withdrawn
    sennen_turner   TEXT,
    withdrawn       INTEGER NOT NULL DEFAULT 0,
    points          REAL,
    points_mismatch INTEGER NOT NULL DEFAULT 0,
    n_entries       INTEGER NOT NULL DEFAULT 0,
    grade_sum       REAL,
    birth_year      TEXT,
    association     TEXT,
    place           TEXT
);
CREATE INDEX IF NOT EXISTS idx_athletes_raw_fest ON athletes_raw(fest_id);
CREATE INDEX IF NOT EXISTS idx_athletes_raw_key ON athletes_raw(name_base_key);

-- spec Bout (athlete ids are athletes_raw ids until Phase 3 resolves identities)
CREATE TABLE IF NOT EXISTS bouts (
    bout_id         TEXT PRIMARY KEY,
    fest_id         INTEGER NOT NULL REFERENCES festivals(fest_id),
    gang_nr         INTEGER NOT NULL CHECK (gang_nr BETWEEN 1 AND 8),
    athlete_a_id    TEXT    NOT NULL REFERENCES athletes_raw(athlete_raw_id),
    athlete_b_id    TEXT    NOT NULL REFERENCES athletes_raw(athlete_raw_id),
    outcome         TEXT    NOT NULL CHECK (outcome IN ('WIN_A', 'WIN_B', 'DRAW')),
    grade_a         REAL    NOT NULL CHECK (grade_a BETWEEN 8.25 AND 10.0),
    grade_b         REAL    NOT NULL CHECK (grade_b BETWEEN 8.25 AND 10.0),
    schlussgang     INTEGER NOT NULL DEFAULT 0,
    flags           TEXT    NOT NULL DEFAULT '',
    CHECK (athlete_a_id <> athlete_b_id)
);
CREATE INDEX IF NOT EXISTS idx_bouts_fest ON bouts(fest_id);

-- everything that did not become a bout, with a reason (never dropped silently)
CREATE TABLE IF NOT EXISTS parse_rejects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fest_id         INTEGER NOT NULL,
    stage           TEXT    NOT NULL,   -- festival|athlete|entry|bout|line
    reason          TEXT    NOT NULL,
    detail          TEXT,
    line            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_parse_rejects_fest ON parse_rejects(fest_id);
"""


@dataclass(frozen=True)
class Festival:
    fest_id: int
    name: str
    date: str
    category: str | None
    location: str | None
    eidg_type: str | None = None  # set iff category == 'ESAF'
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
    event_flags: str = ""          # comma-separated EVENT_FLAGS
    elo_eligible: bool = True      # counts toward ELO (see Config.elo_exclude_flags)

    def __post_init__(self) -> None:
        if self.category is not None and self.category not in CATEGORIES:
            raise ValueError(f"invalid category {self.category!r}")
        if self.kind not in KINDS:
            raise ValueError(f"invalid kind {self.kind!r}")
        if self.kind == "active" and self.category is None:
            raise ValueError(f"active festival {self.fest_id} needs a category")
        if self.eidg_type is not None and self.eidg_type not in EIDG_TYPES:
            raise ValueError(f"invalid eidg_type {self.eidg_type!r}")
        unknown = set(filter(None, self.event_flags.split(","))) - set(EVENT_FLAGS)
        if unknown:
            raise ValueError(f"unknown event flags {sorted(unknown)}")
        if (self.category == "ESAF") != (self.eidg_type is not None):
            raise ValueError(f"festival {self.fest_id}: eidg_type must be set iff "
                             f"category is ESAF (got {self.category!r}/{self.eidg_type!r})")
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
    conn.executescript(SCHEMA)  # no-op for existing tables
    _migrate(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring older tables up to date (idempotent, keyed on actual columns)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(festivals)")}
    if "eidg_type" not in cols:  # v1 -> v2
        conn.execute(f"ALTER TABLE festivals ADD COLUMN {_EIDG_COLUMN}")
        # v1 stored only the real ESAF as 'ESAF'; the other eidg. festivals are
        # re-derived by the next crawl (`crawl --offline` re-parses the cache).
        conn.execute("UPDATE festivals SET eidg_type = 'ESAF' WHERE category = 'ESAF'")
    if "event_flags" not in cols:  # v2 -> v3 (values re-derived by the next crawl)
        conn.execute(f"ALTER TABLE festivals ADD COLUMN {_FLAGS_COLUMN}")
        conn.execute(f"ALTER TABLE festivals ADD COLUMN {_ELIGIBLE_COLUMN}")


def _row_to_festival(row: sqlite3.Row) -> Festival:
    values = {c: row[c] for c in FESTIVAL_COLUMNS}
    values["cancelled"] = bool(values["cancelled"])
    values["elo_eligible"] = bool(values["elo_eligible"])
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
