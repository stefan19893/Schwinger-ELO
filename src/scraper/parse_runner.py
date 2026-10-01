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
from src.scraper.supplements import fetch_interim_sheet

log = logging.getLogger("schwingen.parse")

# Bump whenever parsing rules change so cached sheets are re-parsed.
# v2: extra bouts, wrapped / no-grade lines, Gang count (review fixes); v3: one_sided
# Schlussgang, interim sheets, entries_overflow, duplicate-content check
PARSER_VERSION = 3

_ATHLETE_COLS = ["athlete_raw_id", "fest_id", "idx", "rank", "name_raw", "name", "name_key",
                 "name_base_key", "status", "mark", "sennen_turner", "withdrawn", "points",
                 "points_mismatch", "n_entries", "grade_sum", "birth_year", "association", "place", "flags"]
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
    duplicates: list[tuple[int, int, str]] = field(default_factory=list)


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
            "youth_blocks, parsed_at, n_gaenge) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fest.fest_id, fest.statistic_pdf_url, sha, PARSER_VERSION,
             res.layout if res else None, status, res.header_check if res else None,
             len(res.athletes) if res else 0, res.entries_total if res else 0,
             len(res.bouts) if res else 0, len(rejects), res.youth_blocks if res else 0, now,
             res.gang_count if res else None))


def _sql(v: object) -> object:
    return int(v) if isinstance(v, bool) else v


def parse_text(fest: Festival, text: str, *, min_pair_rate: float,
               interim_text: str | None = None) -> bp.FestivalParse:
    return bp.parse_festival(text, fest.fest_id, fest.date, fest.name,
                             max_gang=bp.max_gaenge(fest.category, fest.eidg_type),
                             min_pair_rate=min_pair_rate, interim_text=interim_text)


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
        interim_text: str | None = None
        extra: list[bp.Reject] = []
        try:
            interim = fetch_interim_sheet(client, fest, today, pdf_max_age_hours, pdf_grace_days)
            if interim is not None:
                interim_text = pdf_to_text(interim.content)
                sha = hashlib.sha256(pdf.content + interim.content).hexdigest()
        except (CacheMiss, FetchError) as exc:
            extra.append(bp.Reject(fest.fest_id, "festival", "interim_sheet_not_cached",
                                   f"{type(exc).__name__}: run `crawl` first"))
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
        res = parse_text(fest, text, min_pair_rate=min_pair_rate, interim_text=interim_text)
        res.rejects.extend(extra)
        store_result(conn, fest, res, status=res.status, sha=sha)
        rep.parsed += 1
        rep.status[res.status] += 1
        rep.bouts += len(res.bouts)
        rep.athletes += len(res.athletes)
        rep.rejects.update(r.reason for r in res.rejects)
    rep.duplicates = flag_duplicate_sheets(conn)
    return rep


# ----------------------------------------------------------------- duplicate content
DUPLICATE_MIN_BOUTS = 20
DUPLICATE_MIN_OVERLAP = 0.8


def flag_duplicate_sheets(conn: sqlite3.Connection) -> list[tuple[int, int, str]]:
    """Safeguard against one sheet imported under two festivals (e.g. a reused PDF
    whose header could not be verified): identical PDF content (sha256) or ≥ 80 %
    of the smaller festival's bouts between the same two athletes (name keys).

    The festival whose header check is 'ok' keeps the bouts, else the earlier one;
    the other gets status ``duplicate_sheet`` (bouts and athletes removed, reject
    with the reason). Returns (kept, flagged, why)."""
    rows = conn.execute(
        "SELECT p.fest_id, p.pdf_sha256, p.header_check, f.date FROM festival_parse p "
        "JOIN festivals f USING(fest_id) WHERE p.status IN ('ok', 'partial')").fetchall()
    info = {r[0]: (r[2] or "", r[3]) for r in rows}
    pairs: dict[tuple[int, int], str] = {}
    by_sha: dict[str, list[int]] = {}
    for fid, sha, _, _ in rows:
        if sha:
            by_sha.setdefault(sha, []).append(fid)
    for fids in by_sha.values():
        for other in fids[1:]:
            pairs[(fids[0], other)] = "identical PDF content"
    sig: dict[int, set[tuple[str, str]]] = {}
    for fid, ka, kb in conn.execute(
            "SELECT b.fest_id, x.name_base_key, y.name_base_key FROM bouts b "
            "JOIN athletes_raw x ON x.athlete_raw_id = b.athlete_a_id "
            "JOIN athletes_raw y ON y.athlete_raw_id = b.athlete_b_id"):
        sig.setdefault(fid, set()).add((min(ka, kb), max(ka, kb)))
    owners: dict[tuple[str, str], list[int]] = {}
    for fid, keys in sig.items():
        for k in keys:
            owners.setdefault(k, []).append(fid)
    overlap: Counter[tuple[int, int]] = Counter()
    for fids in owners.values():
        if 1 < len(fids) <= 20:  # very common pairs carry no information
            for i, x in enumerate(fids):
                for y in fids[i + 1:]:
                    overlap[(min(x, y), max(x, y))] += 1
    for (x, y), n in overlap.items():
        small = min(len(sig[x]), len(sig[y]))
        if small >= DUPLICATE_MIN_BOUTS and n >= DUPLICATE_MIN_OVERLAP * small:
            pairs.setdefault((x, y), f"{n} of {small} bouts identical")
    out: list[tuple[int, int, str]] = []
    flagged: set[int] = set()
    for (x, y), why in sorted(pairs.items()):
        if x in flagged or y in flagged or x not in info or y not in info:
            continue
        keep, drop = sorted((x, y), key=lambda f: (info[f][0] != "ok", info[f][1], f))
        with conn:
            for table in ("bouts", "athletes_raw"):
                conn.execute(f"DELETE FROM {table} WHERE fest_id = ?", (drop,))
            conn.execute("INSERT INTO parse_rejects (fest_id, stage, reason, detail) "
                         "VALUES (?, 'festival', 'duplicate_sheet', ?)",
                         (drop, f"{why} with festival {keep}; not imported twice"))
            conn.execute("UPDATE festival_parse SET status = 'duplicate_sheet', n_bouts = 0, "
                         "n_athletes = 0, n_rejects = n_rejects + 1 WHERE fest_id = ?", (drop,))
        flagged.add(drop)
        out.append((keep, drop, why))
        log.warning("festival %d: duplicate content of %d (%s) - not imported", drop, keep, why)
    return out


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
    rep.duplicates = flag_duplicate_sheets(conn)
    return rep
