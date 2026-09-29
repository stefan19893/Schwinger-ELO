"""Batch-parse cached statistic PDFs into SQLite (``bouts``, ``athletes_raw``,
``parse_rejects``, ``festival_parse``).

Works offline on ``data/raw/`` (spec §5: ``parse`` parses *cached* sheets; the
downloads happen in ``crawl``). Incremental: a festival is re-parsed only when
its PDF content (sha256) or :data:`PARSER_VERSION` changed, or with ``force``.
Every active, non-cancelled festival gets a ``festival_parse`` row, also when
it has no PDF or the PDF is missing/broken, so nothing disappears silently.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tqdm import tqdm

from src.db import Festival, load_festivals
from src.scraper import bouts_parser as bp
from src.scraper.client import CacheMiss, FetchError, HttpClient
from src.scraper.statistic_pdfs import fetch_statistic_pdf, pdf_to_text

log = logging.getLogger("schwingen.parse")

# Bump whenever parsing rules change so cached sheets are re-parsed.
PARSER_VERSION = 1

_ATHLETE_COLS = ["athlete_raw_id", "fest_id", "idx", "rank", "name_raw", "name", "name_key",
                 "name_base_key", "status", "mark", "sennen_turner", "withdrawn", "points",
                 "points_mismatch", "n_entries", "grade_sum", "birth_year", "association", "place"]
_BOUT_COLS = ["bout_id", "fest_id", "gang_nr", "athlete_a_id", "athlete_b_id", "outcome",
              "grade_a", "grade_b", "schlussgang", "flags"]


@dataclass
class ParseRunReport:
    parsed: int = 0
    unchanged: int = 0
    status: Counter[str] = field(default_factory=Counter)
    bouts: int = 0
    athletes: int = 0
    rejects: Counter[str] = field(default_factory=Counter)


def festivals_to_parse(conn: sqlite3.Connection) -> list[Festival]:
    """Active, non-cancelled festivals (borderline ones too; ELO filters later)."""
    return [f for f in load_festivals(conn).values() if f.kind == "active" and not f.cancelled]


def _status(conn: sqlite3.Connection, fest_id: int) -> str | None:
    row = conn.execute("SELECT status FROM festival_parse WHERE fest_id = ?", (fest_id,)).fetchone()
    return row[0] if row else None


def _existing(conn: sqlite3.Connection, fest_id: int) -> tuple[str | None, int | None]:
    row = conn.execute("SELECT pdf_sha256, parser_version FROM festival_parse WHERE fest_id = ?",
                       (fest_id,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def store_result(conn: sqlite3.Connection, fest: Festival, res: bp.FestivalParse | None, *,
                 status: str, sha: str | None, extra_rejects: Iterable[bp.Reject] = (),
                 now: str | None = None) -> None:
    """Replace everything stored for ``fest`` with ``res`` (one transaction)."""
    now = now or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    rejects = list(res.rejects if res else []) + list(extra_rejects)
    with conn:
        for table in ("bouts", "athletes_raw", "parse_rejects"):
            conn.execute(f"DELETE FROM {table} WHERE fest_id = ?", (fest.fest_id,))
        if res:
            conn.executemany(
                f"INSERT INTO athletes_raw ({', '.join(_ATHLETE_COLS)}) "
                f"VALUES ({', '.join('?' for _ in _ATHLETE_COLS)})",
                [[_sql(a.get(c)) for c in _ATHLETE_COLS] for a in res.athletes])
            conn.executemany(
                f"INSERT INTO bouts ({', '.join(_BOUT_COLS)}) "
                f"VALUES ({', '.join('?' for _ in _BOUT_COLS)})",
                [[_sql(b.get(c)) for c in _BOUT_COLS] for b in res.bouts])
        conn.executemany(
            "INSERT INTO parse_rejects (fest_id, stage, reason, detail, line) VALUES (?,?,?,?,?)",
            [(r.fest_id, r.stage, r.reason, r.detail, r.line) for r in rejects])
        conn.execute(
            "INSERT OR REPLACE INTO festival_parse (fest_id, pdf_url, pdf_sha256, parser_version, "
            "layout, status, header_check, n_athletes, n_entries, n_bouts, n_rejects, "
            "youth_blocks, parsed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fest.fest_id, fest.statistic_pdf_url, sha, PARSER_VERSION,
             res.layout if res else None, status, res.header_check if res else None,
             len(res.athletes) if res else 0, res.entries_total if res else 0,
             len(res.bouts) if res else 0, len(rejects), res.youth_blocks if res else 0, now))


def _sql(v: object) -> object:
    return int(v) if isinstance(v, bool) else v


def parse_text(fest: Festival, text: str, *, min_pair_rate: float) -> bp.FestivalParse:
    return bp.parse_festival(text, fest.fest_id, fest.date, fest.name,
                             max_gang=bp.max_gaenge(fest.category, fest.eidg_type),
                             min_pair_rate=min_pair_rate)


def parse_all(conn: sqlite3.Connection, client: HttpClient, *, today: _dt.date | None = None,
              min_pair_rate: float = 0.5, force: bool = False, progress: bool = False,
              pdf_max_age_hours: float = 24.0, pdf_grace_days: int = 14) -> ParseRunReport:
    """Parse every active festival's cached statistic PDF into the database."""
    today = today or _dt.date.today()
    rep = ParseRunReport()
    fests = sorted(festivals_to_parse(conn), key=lambda f: (f.date, f.fest_id))
    for fest in tqdm(fests, desc="parse", unit="fest", disable=not progress):
        if not fest.statistic_pdf_url:
            if not force and _status(conn, fest.fest_id) == "no_pdf":
                rep.unchanged += 1
                continue
            _store_failure(conn, fest, rep, "no_pdf", None, "no_statistic_pdf",
                           "festival has no statistic PDF on schlussgang")
            continue
        try:
            pdf = fetch_statistic_pdf(client, fest, today, pdf_max_age_hours, pdf_grace_days)
        except CacheMiss:
            _store_failure(conn, fest, rep, "pdf_not_cached", None, "pdf_not_cached",
                           "run `crawl` first (PDF not in data/raw)")
            continue
        except FetchError as exc:
            _store_failure(conn, fest, rep, "pdf_error", None, "pdf_fetch_error", str(exc))
            continue
        sha = hashlib.sha256(pdf.content).hexdigest()
        old_sha, old_version = _existing(conn, fest.fest_id)
        if not force and old_sha == sha and old_version == PARSER_VERSION:
            rep.unchanged += 1
            continue
        try:
            text = pdf_to_text(pdf.content)
        except Exception as exc:  # noqa: BLE001 - any PDFium failure is recorded, not raised
            _store_failure(conn, fest, rep, "pdf_error", sha, "pdf_unreadable",
                           f"{type(exc).__name__}: {exc}")
            continue
        res = parse_text(fest, text, min_pair_rate=min_pair_rate)
        store_result(conn, fest, res, status=res.status, sha=sha)
        rep.parsed += 1
        rep.status[res.status] += 1
        rep.bouts += len(res.bouts)
        rep.athletes += len(res.athletes)
        rep.rejects.update(r.reason for r in res.rejects)
    return rep


def _store_failure(conn: sqlite3.Connection, fest: Festival, rep: ParseRunReport, status: str,
                   sha: str | None, reason: str, detail: str) -> None:
    store_result(conn, fest, None, status=status, sha=sha,
                 extra_rejects=[bp.Reject(fest.fest_id, "festival", reason, detail)])
    rep.parsed += 1
    rep.status[status] += 1
    rep.rejects[reason] += 1


def parse_from_dir(conn: sqlite3.Connection, directory: Path, *, min_pair_rate: float = 0.5,
                   force: bool = False) -> ParseRunReport:
    """Offline variant for ``--sample``: sheets as ``<fest_id>.pdf`` or ``<fest_id>.txt``."""
    rep = ParseRunReport()
    for fest in sorted(festivals_to_parse(conn), key=lambda f: (f.date, f.fest_id)):
        pdf, txt = directory / f"{fest.fest_id}.pdf", directory / f"{fest.fest_id}.txt"
        path = pdf if pdf.is_file() else txt if txt.is_file() else None
        if path is None:
            _store_failure(conn, fest, rep, "pdf_not_cached", None, "pdf_not_cached",
                           f"no sheet in {directory}")
            continue
        content = path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        old_sha, old_version = _existing(conn, fest.fest_id)
        if not force and old_sha == sha and old_version == PARSER_VERSION:
            rep.unchanged += 1
            continue
        text = pdf_to_text(content) if path.suffix == ".pdf" else content.decode("utf-8")
        res = parse_text(fest, text, min_pair_rate=min_pair_rate)
        store_result(conn, fest, res, status=res.status, sha=sha)
        rep.parsed += 1
        rep.status[res.status] += 1
        rep.bouts += len(res.bouts)
        rep.athletes += len(res.athletes)
        rep.rejects.update(r.reason for r in res.rejects)
    return rep
