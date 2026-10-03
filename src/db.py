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

SCHEMA_VERSION = 6  # v2: festivals.eidg_type; v3: event_flags/elo_eligible + parse tables;
                    # v4: nullable bout grades (extra bouts) + schlussgang, festival_parse.n_gaenge;
                    # v5: 'one_sided' NULL-grade flag, athletes_raw.flags;
                    # v6: identity evidence (ranking lists, portraits, clubs)

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
# Gänge of the festival as derived from the sheet (bouts_parser.festival_gang_count)
_N_GAENGE_COLUMN = "n_gaenge INTEGER"
_ATHLETE_FLAGS_COLUMN = "flags TEXT NOT NULL DEFAULT ''"
# Bout flags that allow a NULL grade: an extra bout's placeholder line (0.00 / 0.25 /
# none), an entry printed without grade whose mirror entry is complete, and a
# Schlussgang whose loser's line the sheet omits (bout from the winner's entry).
NULL_GRADE_FLAGS = ("extra_bout", "grade_missing", "one_sided")
_null_ok = " OR ".join(f"(',' || flags || ',') LIKE '%,{f},%'" for f in NULL_GRADE_FLAGS)
_BOUTS_TABLE = f"""-- spec Bout (athlete ids are athletes_raw ids until Phase 3 resolves identities)
CREATE TABLE IF NOT EXISTS bouts (
    bout_id         TEXT PRIMARY KEY,
    fest_id         INTEGER NOT NULL REFERENCES festivals(fest_id),
    gang_nr         INTEGER NOT NULL CHECK (gang_nr BETWEEN 1 AND 8),
    athlete_a_id    TEXT    NOT NULL REFERENCES athletes_raw(athlete_raw_id),
    athlete_b_id    TEXT    NOT NULL REFERENCES athletes_raw(athlete_raw_id),
    outcome         TEXT    NOT NULL CHECK (outcome IN ('WIN_A', 'WIN_B', 'DRAW')),
    grade_a         REAL    CHECK (grade_a BETWEEN 8.25 AND 10.0),  -- NULL: see flags
    grade_b         REAL    CHECK (grade_b BETWEEN 8.25 AND 10.0),
    -- 1/0 only where the sheet marks the Schlussgang explicitly ('s+' in block layouts);
    -- NULL = unknown (most sheets, incl. all modern ESV sheets, have no marker)
    schlussgang     INTEGER CHECK (schlussgang IN (0, 1)),
    flags           TEXT    NOT NULL DEFAULT '',
    CHECK (athlete_a_id <> athlete_b_id),
    CHECK ((grade_a IS NOT NULL AND grade_b IS NOT NULL) OR {_null_ok})
);"""

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
    parsed_at       TEXT    NOT NULL,
    {_N_GAENGE_COLUMN}
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
    place           TEXT,
    {_ATHLETE_FLAGS_COLUMN}  -- e.g. entries_overflow, interim_sheet
);
CREATE INDEX IF NOT EXISTS idx_athletes_raw_fest ON athletes_raw(fest_id);
CREATE INDEX IF NOT EXISTS idx_athletes_raw_key ON athletes_raw(name_base_key);

{_BOUTS_TABLE}
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
""" + """
-- Phase 3 identity evidence (all rebuilt by `parse`, never edited by hand) ------------
-- one row per festival whose Schlussrangliste PDF was looked at
CREATE TABLE IF NOT EXISTS ranking_parse (
    fest_id         INTEGER PRIMARY KEY REFERENCES festivals(fest_id),
    pdf_url         TEXT,
    pdf_sha256      TEXT,
    parser_version  INTEGER NOT NULL,
    layout          TEXT,               -- esv | header | plain
    status          TEXT    NOT NULL,   -- ok|partial|failed|no_pdf|pdf_not_cached|pdf_error
    n_entries       INTEGER NOT NULL DEFAULT 0,
    n_rejects       INTEGER NOT NULL DEFAULT 0,
    n_linked        INTEGER NOT NULL DEFAULT 0,  -- entries linked to athletes_raw
    n_stat_athletes INTEGER NOT NULL DEFAULT 0,  -- athletes_raw rows of the festival
    parsed_at       TEXT    NOT NULL
);

-- one row per athlete printed in a Schlussrangliste
CREATE TABLE IF NOT EXISTS ranking_entries (
    fest_id         INTEGER NOT NULL REFERENCES festivals(fest_id),
    idx             INTEGER NOT NULL,
    rank            TEXT,               -- normalised '1a', '12'
    rank_num        INTEGER,
    points          REAL,
    result_str      TEXT,               -- '+' win, '-' gestellt, 'o' loss per Gang
    schlussgang     INTEGER NOT NULL DEFAULT 0,
    name_raw        TEXT    NOT NULL,   -- the printed line
    name            TEXT    NOT NULL,
    name_key        TEXT    NOT NULL,
    sennen_turner   TEXT,
    stars           TEXT,
    birth_year      INTEGER,
    residence       TEXT,
    assoc_code      TEXT,               -- cantonal / sub-association code: 'LU', 'ONW', 'BO', 'NOS' ...
    club_raw        TEXT,               -- Schwingklub as printed
    club_nr         INTEGER,            -- Bernese lists: club number '(181)'
    status          TEXT,               -- 'Kranz', 'Neukranzer', 'Unfall', ...
    page            INTEGER,
    line_no         INTEGER,
    athlete_raw_id  TEXT,               -- linked athletes_raw row of the same festival (or NULL)
    link_method     TEXT,               -- rank_points | rank_points_result | name | name_fuzzy | ...
    link_score      REAL,
    PRIMARY KEY (fest_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_ranking_entries_raw ON ranking_entries(athlete_raw_id);

-- unparseable lines and unlinked entries (stage 'line' / 'link'), never dropped silently
CREATE TABLE IF NOT EXISTS ranking_rejects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fest_id         INTEGER NOT NULL,
    stage           TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    detail          TEXT,
    line            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ranking_rejects_fest ON ranking_rejects(fest_id);

-- schlussgang.ch athlete portraits (JSON:API node--portrait)
CREATE TABLE IF NOT EXISTS portraits (
    portrait_id         INTEGER PRIMARY KEY,  -- drupal nid
    slug                TEXT,                 -- 'joel-wicki' (URL /portraet/<slug>)
    url                 TEXT,
    title               TEXT,
    last_name           TEXT,
    first_name          TEXT,
    name_key            TEXT,                 -- of 'Last First'
    birthday            TEXT,
    city                TEXT,
    hknr                INTEGER,              -- ESV licence number
    club_tid            INTEGER,
    club_name           TEXT,
    club_esv_id         INTEGER,
    association_name    TEXT,                 -- Teilverband, e.g. 'Innerschweiz'
    canton_association  TEXT,                 -- e.g. 'Luzern'
    activity            TEXT,
    end_of_career       TEXT,
    fetched_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_portraits_key ON portraits(name_key);

-- festival -> portrait (event field_ref_portrait, schlussgang 2023+), linked to athletes_raw
CREATE TABLE IF NOT EXISTS portrait_appearances (
    fest_id         INTEGER NOT NULL,
    portrait_id     INTEGER NOT NULL,
    athlete_raw_id  TEXT,
    ranking_idx     INTEGER,
    link_method     TEXT,
    PRIMARY KEY (fest_id, portrait_id)
);
CREATE INDEX IF NOT EXISTS idx_portrait_app_raw ON portrait_appearances(athlete_raw_id);

-- canonical Schwingklubs (src.pipeline.clubs.ClubRegistry) with their Teilverband
CREATE TABLE IF NOT EXISTS clubs (
    club_key        TEXT PRIMARY KEY,
    name            TEXT    NOT NULL,   -- most frequent spelling
    sub_association TEXT,               -- BKSV / ISV / NOSV / NWSV / SWSV (weighted vote)
    n_obs           INTEGER NOT NULL,
    conflict        INTEGER NOT NULL DEFAULT 0,  -- runner-up Teilverband has >= 25 % of votes
    esv_id          INTEGER,            -- schlussgang club term's ESV id (from portraits)
    votes           TEXT                -- e.g. 'ISV:42,NOSV:1'
);

-- per athletes_raw row: the identity evidence collected above, with normalised club
CREATE TABLE IF NOT EXISTS athlete_evidence (
    athlete_raw_id  TEXT PRIMARY KEY,
    fest_id         INTEGER NOT NULL,
    ranking_idx     INTEGER,
    residence       TEXT,
    birth_year      INTEGER,
    assoc_code      TEXT,
    club_raw        TEXT,
    club_key        TEXT,               -- src.pipeline.clubs canonical key
    club            TEXT,               -- canonical display name
    sub_association TEXT,               -- BKSV / ISV / NOSV / NWSV / SWSV
    sub_assoc_source TEXT,              -- code | club | portrait | festival
    portrait_id     INTEGER,
    portrait_slug   TEXT
);
CREATE INDEX IF NOT EXISTS idx_athlete_evidence_fest ON athlete_evidence(fest_id);
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
    # v3 -> v4: nullable bout grades (rebuild: SQLite cannot alter constraints) and
    # festival_parse.n_gaenge. Old rows are copied; the bumped PARSER_VERSION makes
    # the next `parse` replace them anyway.
    if "n_gaenge" not in {r[1] for r in conn.execute("PRAGMA table_info(festival_parse)")}:
        conn.execute(f"ALTER TABLE festival_parse ADD COLUMN {_N_GAENGE_COLUMN}")
    if "flags" not in {r[1] for r in conn.execute("PRAGMA table_info(athletes_raw)")}:
        conn.execute(f"ALTER TABLE athletes_raw ADD COLUMN {_ATHLETE_FLAGS_COLUMN}")
    notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(bouts)")}
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'bouts'").fetchone()
    outdated_check = bool(sql) and any(f not in sql[0] for f in NULL_GRADE_FLAGS)
    if any(notnull.get(c) for c in ("grade_a", "grade_b", "schlussgang")) or outdated_check:
        cols_b = ", ".join(r[1] for r in conn.execute("PRAGMA table_info(bouts)"))
        conn.execute("ALTER TABLE bouts RENAME TO bouts_v3")
        conn.execute("DROP INDEX IF EXISTS idx_bouts_fest")
        conn.executescript(_BOUTS_TABLE)
        conn.execute(f"INSERT INTO bouts ({cols_b}) SELECT {cols_b} FROM bouts_v3")
        conn.execute("DROP TABLE bouts_v3")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bouts_fest ON bouts(fest_id)")


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
