"""Parse cached Schlussrangliste PDFs into ``ranking_entries`` and link every entry
to the ``athletes_raw`` row of the same festival (Phase 3 identity evidence).

* Parsing is offline (``data/raw`` cache) and incremental on PDF sha256 +
  :data:`RANKING_PARSER_VERSION`.
* Linking is recomputed for every festival on each run (cheap) because
  ``athletes_raw`` is replaced whenever the statistic sheets are re-parsed.

Link order per festival (one-to-one, greedy by strength):

1. ``rank_points``  same rank number and points, best unique name: similarity ≥ 0.7
                    with the identical full rank ('4a'), ≥ 0.85 otherwise (a stat key
                    that is a prefix of the ranking key - residence printed in the
                    name cell - counts 0.95)
2. ``name``         identical name key, unique on both sides
3. ``name_points``  identical name key (namesakes) disambiguated by points
4. ``name_fuzzy``   name similarity ≥ 0.85 and points within 0.25, unique best

Gang results are *not* used to link: tied athletes often share points and very
similar result strings, so they do not discriminate (checked on the corpus).

Unlinked entries become ``ranking_rejects`` (stage ``link``) with the reason.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tqdm import tqdm

from src.db import Festival, load_festivals
from src.pipeline.names import clean_raw_name, name_key, name_similarity
from src.scraper.client import CacheMiss, FetchError, HttpClient
from src.scraper.pdf_layout import Line, Word
from src.scraper.ranking_parser import (RANKING_PARSER_VERSION, RankingParse, parse_ranking_lines,
                                        parse_ranking_pdf)
from src.scraper.ranking_pdfs import fetch_ranking_pdf

log = logging.getLogger("schwingen.ranking")

_ENTRY_COLS = ["fest_id", "idx", "rank", "rank_num", "points", "result_str", "schlussgang",
               "name_raw", "name", "name_key", "sennen_turner", "stars", "birth_year",
               "residence", "assoc_code", "club_raw", "club_nr", "status", "page", "line_no"]

# A parse is 'failed' below this many entries, 'partial' when a sizeable share of
# data-looking lines was rejected.
MIN_ENTRIES = 3


@dataclass
class RankingRunReport:
    parsed: int = 0
    unchanged: int = 0
    status: Counter[str] = field(default_factory=Counter)
    entries: int = 0
    linked: int = 0
    link_methods: Counter[str] = field(default_factory=Counter)
    unlinked: Counter[str] = field(default_factory=Counter)


def max_gaenge(fest: Festival) -> int:
    return 8 if fest.eidg_type == "ESAF" else 6


def _status_for(res: RankingParse) -> str:
    if len(res.entries) < MIN_ENTRIES:
        return "failed"
    bad = sum(1 for r in res.rejects if r.reason != "bad_result_length")
    return "partial" if bad > 0.2 * len(res.entries) else "ok"


def store_ranking(conn: sqlite3.Connection, fest: Festival, res: RankingParse | None, *,
                  status: str, sha: str | None, reject: tuple[str, str] | None = None,
                  now: str | None = None) -> None:
    """Replace the stored ranking entries / line rejects of ``fest`` (one transaction)."""
    now = now or _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM ranking_entries WHERE fest_id = ?", (fest.fest_id,))
        conn.execute("DELETE FROM ranking_rejects WHERE fest_id = ?", (fest.fest_id,))
        rows = []
        for e in (res.entries if res else []):
            values = {**e.__dict__, "fest_id": fest.fest_id,
                      "name_key": name_key(clean_raw_name(e.name)[0] or e.name),
                      "schlussgang": int(e.schlussgang)}
            rows.append([values[c] for c in _ENTRY_COLS])
        conn.executemany(
            f"INSERT INTO ranking_entries ({', '.join(_ENTRY_COLS)}) "
            f"VALUES ({', '.join('?' for _ in _ENTRY_COLS)})", rows)
        rejects = [(fest.fest_id, "line", r.reason, r.detail, r.line_no)
                   for r in (res.rejects if res else [])]
        if reject:
            rejects.append((fest.fest_id, "festival", reject[0], reject[1], None))
        conn.executemany("INSERT INTO ranking_rejects (fest_id, stage, reason, detail, line) "
                         "VALUES (?, ?, ?, ?, ?)", rejects)
        conn.execute(
            "INSERT OR REPLACE INTO ranking_parse (fest_id, pdf_url, pdf_sha256, parser_version, "
            "layout, status, n_entries, n_rejects, parsed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (fest.fest_id, fest.ranking_pdf_url, sha, RANKING_PARSER_VERSION,
             res.layout if res else None, status, len(res.entries) if res else 0,
             len(rejects), now))


def _existing(conn: sqlite3.Connection, fest_id: int) -> tuple[str | None, int | None, str | None]:
    row = conn.execute("SELECT pdf_sha256, parser_version, status FROM ranking_parse "
                       "WHERE fest_id = ?", (fest_id,)).fetchone()
    return (row[0], row[1], row[2]) if row else (None, None, None)


def parse_rankings(conn: sqlite3.Connection, client: HttpClient, *,
                   today: _dt.date | None = None, force: bool = False, progress: bool = False,
                   pdf_max_age_hours: float = 24.0, pdf_grace_days: int = 14,
                   relink: bool = True) -> RankingRunReport:
    """Parse every active festival's cached ranking PDF, then (re)link all entries."""
    today = today or _dt.date.today()
    rep = RankingRunReport()
    fests = sorted((f for f in load_festivals(conn).values()
                    if f.kind == "active" and not f.cancelled), key=lambda f: (f.date, f.fest_id))
    for fest in tqdm(fests, desc="rankings", unit="fest", disable=not progress):
        old_sha, old_version, old_status = _existing(conn, fest.fest_id)
        if not fest.ranking_pdf_url:
            if force or old_status != "no_pdf":
                store_ranking(conn, fest, None, status="no_pdf", sha=None,
                              reject=("no_ranking_pdf", "festival has no Schlussrangliste PDF"))
                rep.status["no_pdf"] += 1
            else:
                rep.unchanged += 1
            continue
        try:
            pdf = fetch_ranking_pdf(client, fest, today, pdf_max_age_hours, pdf_grace_days)
        except CacheMiss:
            store_ranking(conn, fest, None, status="pdf_not_cached", sha=None,
                          reject=("pdf_not_cached", "run `crawl` first"))
            rep.status["pdf_not_cached"] += 1
            continue
        except FetchError as exc:
            store_ranking(conn, fest, None, status="pdf_error", sha=None,
                          reject=("pdf_fetch_error", str(exc)))
            rep.status["pdf_error"] += 1
            continue
        sha = hashlib.sha256(pdf.content).hexdigest()
        if not force and old_sha == sha and old_version == RANKING_PARSER_VERSION:
            rep.unchanged += 1
            continue
        _parse_one(conn, fest, sha, rep,
                   lambda: parse_ranking_pdf(pdf.content, max_gaenge=max_gaenge(fest)))
    if relink:
        link_all(conn, rep)
    return rep


def _parse_one(conn: sqlite3.Connection, fest: Festival, sha: str, rep: RankingRunReport,
               parse: Callable[[], RankingParse]) -> None:
    try:
        res = parse()
    except Exception as exc:  # noqa: BLE001 - any PDFium / fixture failure is recorded
        store_ranking(conn, fest, None, status="pdf_error", sha=sha,
                      reject=("pdf_unreadable", f"{type(exc).__name__}: {exc}"))
        rep.status["pdf_error"] += 1
        return
    status = _status_for(res)
    store_ranking(conn, fest, res, status=status, sha=sha)
    rep.parsed += 1
    rep.status[status] += 1
    rep.entries += len(res.entries)


def lines_to_json(lines: list[Line]) -> str:
    """Positioned lines as compact JSON (fixtures: ``lines_<fest_id>.json``)."""
    return "[" + ",\n".join(json.dumps(
        {"page": ln.page, "y": round(ln.y, 2),
         "words": [[w.text, round(w.x0, 2), round(w.x1, 2)] for w in ln.words]},
        ensure_ascii=False) for ln in lines) + "]\n"


def lines_from_json(text: str) -> list[Line]:
    return [Line(d["page"], d["y"], tuple(Word(*w) for w in d["words"]))
            for d in json.loads(text)]


def parse_rankings_from_dir(conn: sqlite3.Connection, directory: Path, *, force: bool = False,
                            relink: bool = True) -> RankingRunReport:
    """Offline variant for ``--sample``: lists as ``<fest_id>.pdf`` or, for large
    PDFs, their positioned lines as ``lines_<fest_id>.json`` (:func:`lines_to_json`)."""
    rep = RankingRunReport()
    fests = sorted((f for f in load_festivals(conn).values()
                    if f.kind == "active" and not f.cancelled), key=lambda f: (f.date, f.fest_id))
    for fest in fests:
        old_sha, old_version, old_status = _existing(conn, fest.fest_id)
        pdf, js = directory / f"{fest.fest_id}.pdf", directory / f"lines_{fest.fest_id}.json"
        path = pdf if pdf.is_file() else js if js.is_file() else None
        if path is None:
            if force or old_status != "pdf_not_cached":
                store_ranking(conn, fest, None, status="pdf_not_cached", sha=None,
                              reject=("pdf_not_cached", f"no ranking list in {directory}"))
                rep.status["pdf_not_cached"] += 1
            else:
                rep.unchanged += 1
            continue
        content = path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        if not force and old_sha == sha and old_version == RANKING_PARSER_VERSION:
            rep.unchanged += 1
            continue
        gaenge = max_gaenge(fest)
        if path.suffix == ".pdf":
            _parse_one(conn, fest, sha, rep,
                       lambda: parse_ranking_pdf(content, max_gaenge=gaenge))
        else:
            _parse_one(conn, fest, sha, rep, lambda: parse_ranking_lines(
                lines_from_json(content.decode("utf-8")), max_gaenge=gaenge))
    if relink:
        link_all(conn, rep)
    return rep


# --------------------------------------------------------------------------- linking
@dataclass
class _Stat:
    raw_id: str
    rank: str | None
    points: float | None
    key: str
    results: dict[int, str]


@dataclass
class _Rank:
    idx: int
    rank: str | None
    points: float | None
    key: str
    result: str | None


@dataclass(frozen=True)
class Link:
    idx: int
    raw_id: str
    method: str
    score: float


def norm_rank(rank: str | None) -> str | None:
    if rank is None:
        return None
    r = re.sub(r"[\s.]", "", str(rank)).lower()
    return r or None


def result_agreement(result: str | None, gangs: dict[int, str]) -> tuple[int, int]:
    """(agreeing, compared) Gang symbols between a ranking result string and the
    symbols derived from the bouts of a statistic-sheet athlete."""
    if not result:
        return 0, 0
    agree = compared = 0
    for g, sym in gangs.items():
        if 1 <= g <= len(result):
            compared += 1
            agree += result[g - 1] == sym
    return agree, compared


def rank_number(rank: str | None) -> int | None:
    m = re.match(r"^(\d+)", norm_rank(rank) or "")
    return int(m.group(1)) if m else None


def _rank_name_score(r: _Rank, s: _Stat) -> float:
    """Name similarity; a ranking key that starts with the stat key (residence printed
    in the name cell) counts 0.95; +0.01 for the identical full rank as tie-break."""
    sim = name_similarity(r.key, s.key)
    if sim < 0.95 and s.key and r.key.startswith(s.key + " "):
        sim = 0.95
    return sim + (0.01 if norm_rank(s.rank) == norm_rank(r.rank) else 0.0)


def split_residence(name: str, stat_key: str) -> tuple[str, str] | None:
    """'Wehrli Christian Suhr' + stat key 'wehrli christian' -> ('Wehrli Christian',
    'Suhr'): plain layouts sometimes print name and residence as one cell."""
    words = name.split()
    for k in range(len(words) - 1, 1, -1):
        if name_key(" ".join(words[:k])) == stat_key:
            rest = " ".join(words[k:]).strip(" *,")
            return (" ".join(words[:k]).rstrip(" *,"), rest) if rest else None
    return None


def _same_points(a: float | None, b: float | None, tol: float = 0.001) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def link_festival(ranks: list[_Rank], stats: list[_Stat]) -> tuple[list[Link], dict[int, str]]:
    """Link ranking entries to statistic-sheet athletes; returns links and
    {entry idx: reason} for unlinked entries."""
    links: list[Link] = []
    free_r = {r.idx: r for r in ranks}
    free_s = {s.raw_id: s for s in stats}

    def take(r: _Rank, s: _Stat, method: str, score: float) -> None:
        links.append(Link(r.idx, s.raw_id, method, round(score, 3)))
        free_r.pop(r.idx, None)
        free_s.pop(s.raw_id, None)

    # 1/2: rank number + points (stat sheets sometimes omit the rank letter)
    by_rp: dict[tuple[int, float], list[_Stat]] = defaultdict(list)
    for s in stats:
        rn = rank_number(s.rank)
        if rn is not None and s.points is not None:
            by_rp[(rn, round(s.points, 2))].append(s)
    for r in sorted(free_r.values(), key=lambda r: r.idx):
        rn = rank_number(r.rank)
        if rn is None or r.points is None:
            continue
        cands = [s for s in by_rp.get((rn, round(r.points, 2)), []) if s.raw_id in free_s]
        if not cands:
            continue
        scored = sorted(((_rank_name_score(r, s), s) for s in cands), key=lambda t: -t[0])
        best, s = scored[0]
        unique = len(scored) == 1 or scored[1][0] < best
        same_rank = norm_rank(s.rank) == norm_rank(r.rank)
        if unique and best >= (0.7 if same_rank else 0.85):
            take(r, s, "rank_points", min(best, 1.0))
            continue
    # 3/4: identical name key
    by_key_s: dict[str, list[_Stat]] = defaultdict(list)
    for s in free_s.values():
        by_key_s[s.key].append(s)
    by_key_r: dict[str, list[_Rank]] = defaultdict(list)
    for r in free_r.values():
        by_key_r[r.key].append(r)
    for key, rs in by_key_r.items():
        ss = [s for s in by_key_s.get(key, []) if s.raw_id in free_s]
        if not ss:
            continue
        if len(rs) == 1 and len(ss) == 1:
            take(rs[0], ss[0], "name", 1.0)
            continue
        for r in rs:  # namesakes: disambiguate by points
            cands = [s for s in ss if s.raw_id in free_s and _same_points(s.points, r.points)]
            if len(cands) == 1:
                take(r, cands[0], "name_points", 1.0)
    # 5: fuzzy name + points
    for r in sorted(free_r.values(), key=lambda r: r.idx):
        scored = sorted(((name_similarity(r.key, s.key), s) for s in free_s.values()
                         if s.points is None or r.points is None
                         or abs(s.points - r.points) <= 0.25),
                        key=lambda t: -t[0])
        if not scored or scored[0][0] < 0.85:
            continue
        if len(scored) > 1 and scored[1][0] >= scored[0][0]:
            continue  # ambiguous
        take(r, scored[0][1], "name_fuzzy", scored[0][0])
    reasons: dict[int, str] = {}
    for r in free_r.values():
        if not stats:
            reasons[r.idx] = "no_statistic_athletes"
        elif any(name_similarity(r.key, s.key) >= 0.85 for s in stats):
            reasons[r.idx] = "ambiguous_or_taken"
        else:
            reasons[r.idx] = "not_in_statistic_sheet"
    return links, reasons


def _stat_rows(conn: sqlite3.Connection) -> dict[int, list[_Stat]]:
    gangs: dict[str, dict[int, str]] = defaultdict(dict)
    sym_a = {"WIN_A": "+", "WIN_B": "o", "DRAW": "-"}
    sym_b = {"WIN_A": "o", "WIN_B": "+", "DRAW": "-"}
    for a, b, g, outcome in conn.execute(
            "SELECT athlete_a_id, athlete_b_id, gang_nr, outcome FROM bouts"):
        gangs[a][g] = sym_a[outcome]
        gangs[b][g] = sym_b[outcome]
    out: dict[int, list[_Stat]] = defaultdict(list)
    for raw_id, fid, rank, points, name in conn.execute(
            "SELECT athlete_raw_id, fest_id, rank, points, name FROM athletes_raw"):
        clean = clean_raw_name(name)[0] or name
        out[fid].append(_Stat(raw_id, rank, points, name_key(clean), gangs.get(raw_id, {})))
    return out


def link_all(conn: sqlite3.Connection, rep: RankingRunReport | None = None) -> RankingRunReport:
    """(Re)compute links of all ranking entries; rewrites stage-'link' rejects."""
    rep = rep or RankingRunReport()
    stats = _stat_rows(conn)
    ranks: dict[int, list[_Rank]] = defaultdict(list)
    for fid, idx, rank, points, key, result in conn.execute(
            "SELECT fest_id, idx, rank, points, name_key, result_str FROM ranking_entries"):
        ranks[fid].append(_Rank(idx, rank, points, key, result))
    updates: list[tuple[str | None, str | None, float | None, int, int]] = []
    rejects: list[tuple[int, str, str, str, None]] = []
    counts: dict[int, tuple[int, int]] = {}
    names = {(fid, idx): (name, residence) for fid, idx, name, residence in
             conn.execute("SELECT fest_id, idx, name, residence FROM ranking_entries")}
    stat_keys = {st.raw_id: st.key for rows in stats.values() for st in rows}
    repairs: list[tuple[str, str, str, int, int]] = []
    for fid, rs in ranks.items():
        links, reasons = link_festival(rs, stats.get(fid, []))
        for ln in links:
            updates.append((ln.raw_id, ln.method, ln.score, fid, ln.idx))
            rep.link_methods[ln.method] += 1
            name, residence = names[(fid, ln.idx)]
            split = split_residence(name, stat_keys[ln.raw_id]) if not residence else None
            if split:
                repairs.append((split[0], name_key(split[0]), split[1], fid, ln.idx))
        for idx, why in reasons.items():
            updates.append((None, None, None, fid, idx))
            rejects.append((fid, "link", why, names[(fid, idx)][0], None))
            rep.unlinked[why] += 1
        counts[fid] = (len(links), len(stats.get(fid, [])))
        rep.linked += len(links)
    with conn:
        conn.executemany("UPDATE ranking_entries SET athlete_raw_id = ?, link_method = ?, "
                         "link_score = ? WHERE fest_id = ? AND idx = ?", updates)
        conn.executemany("UPDATE ranking_entries SET name = ?, name_key = ?, residence = ? "
                         "WHERE fest_id = ? AND idx = ?", repairs)
        conn.execute("DELETE FROM ranking_rejects WHERE stage = 'link'")
        conn.executemany("INSERT INTO ranking_rejects (fest_id, stage, reason, detail, line) "
                         "VALUES (?, ?, ?, ?, ?)", rejects)
        conn.execute("UPDATE ranking_parse SET n_linked = 0, n_stat_athletes = 0")
        conn.executemany("UPDATE ranking_parse SET n_linked = ?, n_stat_athletes = ? "
                         "WHERE fest_id = ?", [(n, m, fid) for fid, (n, m) in counts.items()])
        for fid, rows in stats.items():
            if fid not in counts:
                conn.execute("UPDATE ranking_parse SET n_stat_athletes = ? WHERE fest_id = ?",
                             (len(rows), fid))
    return rep

