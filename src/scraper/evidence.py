"""Build ``clubs`` and ``athlete_evidence`` from ranking entries, portraits and festivals.

Runs offline after the ranking lists and portraits are loaded (``parse`` stage).
One ``athlete_evidence`` row per ``athletes_raw`` row with what the identity
resolver (``src/pipeline/cleaner.py``) can use: residence, birth year, club
(raw + canonical key/name), Teilverband and portrait slug.

Teilverband per row, by source priority (``sub_assoc_source``):
``code`` (cantonal / Gau code printed next to the athlete, or the statistic
sheet's association marker) > ``club`` (the canonical club's voted Teilverband)
> ``portrait`` > ``festival`` (organising Teilverband of a Kantonal / Gau /
Teilverband festival — weakest: guests take part too).
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from src.db import load_festivals
from src.pipeline.clubs import (GUEST_CODES, ClubRegistry, code_in_club, pseudo_club_sub,
                                strip_code, sub_association_for_code,
                                sub_association_for_festival, sub_association_for_name)

log = logging.getLogger("schwingen.evidence")

# A printed club name without an ESV club id (portraits) is a canonical club only
# with this many observations; rarer names are parsing debris or stay unresolved.
MIN_CLUB_OBS = 20

# A code printed on every coded row of a sheet (>= this many rows) says "took part in
# this festival", not where the athlete is from (Leukerbad 2013: `(SWS)` on all 78 rows
# incl. the Bernese guests): it only counts like the weak `festival` source.
MIN_UNIFORM_ROWS = 15

# Festivals whose field is mostly the organising Teilverband's own athletes.
_HOME_CATEGORIES = frozenset({"Kantonal", "Gauverband", "Regional", "Teilverband"})


@dataclass
class EvidenceReport:
    rows: int = 0
    with_club: int = 0
    with_sub: Counter[str] = field(default_factory=Counter)
    with_portrait: int = 0
    clubs: int = 0
    unresolved_clubs: Counter[str] = field(default_factory=Counter)
    uniform_sheets: int = 0     # (festival, source) pairs with one blanket code
    uniform_rows: int = 0       # rows whose code was downgraded to `festival`


def _code_sub(code: str | None) -> str | None:
    if not code or code.split()[0] in GUEST_CODES:
        return None
    return sub_association_for_code(code)


def _ranking_code(code: str | None, club_raw: str | None) -> str | None:
    """The association code of a ranking entry: its own column, else a code in the
    club column ('(SWS)', 'La Gruyère ARLS'); the first one that names a Teilverband."""
    for c in (code, code_in_club(club_raw), strip_code(club_raw)[0] if club_raw else None):
        if _code_sub(c):
            return c
    return None


def uniform_codes(conn: sqlite3.Connection) -> dict[tuple[int, str], str]:
    """``{(fest_id, 'ranking' | 'sheet'): code}`` for the sheets that print one and the
    same association code on all their coded rows (guest codes aside)."""
    seen: dict[tuple[int, str], Counter[str]] = {}
    for fid, code, club_raw in conn.execute(
            "SELECT fest_id, assoc_code, club_raw FROM ranking_entries"):
        c = _ranking_code(code, club_raw)
        if c:
            seen.setdefault((fid, "ranking"), Counter())[c.strip().upper()] += 1
    for fid, code in conn.execute(
            "SELECT fest_id, association FROM athletes_raw WHERE association IS NOT NULL"):
        if _code_sub(code):
            seen.setdefault((fid, "sheet"), Counter())[code.strip().upper()] += 1
    return {k: next(iter(v)) for k, v in seen.items()
            if len(v) == 1 and sum(v.values()) >= MIN_UNIFORM_ROWS}


def _is_uniform(uniform: dict[tuple[int, str], str], fid: int, source: str,
                code: str | None) -> bool:
    return bool(code) and uniform.get((fid, source)) == code.strip().upper()  # type: ignore[union-attr]


# The club registry (names, spellings, Teilverband votes) is learned from the ranking
# lists from this date on and from the portraits. The lists of the seasons before 2011
# (Phase 10) are read against it but do not shape it: they rarely print a club, their
# club column holds remarks ("Couronne", "Punkte", "Neueidgenosse") in several layouts,
# and their spellings would rename clubs of published athletes ("Basel-Stadt" -> "Basel")
# and unfold established ones ("Sense" from "Sense/La Singine").
CLUB_REGISTRY_FROM = "2011-01-01"


def build_registry(conn: sqlite3.Connection, uniform: dict[tuple[int, str], str] | None = None
                   ) -> tuple[ClubRegistry, dict[int, str | None]]:
    uniform = uniform or {}
    fests = load_festivals(conn)
    fest_sub = {fid: sub_association_for_festival(f.name, f.category, f.association)
                for fid, f in fests.items()}
    home = {fid for fid, f in fests.items() if f.category in _HOME_CATEGORIES}
    reg = ClubRegistry(min_obs=MIN_CLUB_OBS)
    for fid, club, code, n in conn.execute(
            "SELECT fest_id, club_raw, assoc_code, COUNT(*) FROM ranking_entries "
            "WHERE club_raw IS NOT NULL GROUP BY 1, 2, 3"):
        if fests[fid].date < CLUB_REGISTRY_FROM:
            continue
        if _is_uniform(uniform, fid, "ranking", _ranking_code(code, club)):
            code = None                         # a blanket code is no vote for the club
        reg.add(club, code=code, festival_sub=fest_sub.get(fid) if fid in home else None, count=n)
    for club, assoc, esv, n in conn.execute(
            "SELECT club_name, association_name, club_esv_id, COUNT(*) FROM portraits "
            "WHERE club_name IS NOT NULL GROUP BY 1, 2, 3"):
        reg.add(club, portrait_sub=sub_association_for_name(assoc), esv_id=esv, count=n)
    return reg, fest_sub


def build_evidence(conn: sqlite3.Connection) -> EvidenceReport:
    """Rebuild ``clubs`` and ``athlete_evidence`` (one transaction)."""
    rep = EvidenceReport()
    uniform = uniform_codes(conn)
    rep.uniform_sheets = len(uniform)
    reg, fest_sub = build_registry(conn, uniform)
    clubs = reg.build()
    rep.clubs = len(clubs)
    entries = {r[0]: r[1:] for r in conn.execute(
        "SELECT athlete_raw_id, idx, residence, birth_year, assoc_code, club_raw "
        "FROM ranking_entries WHERE athlete_raw_id IS NOT NULL")}
    portraits = {r[0]: r[1:] for r in conn.execute(
        "SELECT a.athlete_raw_id, p.portrait_id, p.slug, p.association_name "
        "FROM portrait_appearances a JOIN portraits p USING (portrait_id) "
        "WHERE a.athlete_raw_id IS NOT NULL")}
    fest_cat = {fid: f.category for fid, f in load_festivals(conn).items()}
    rows = []
    for raw_id, fid, by_stat, assoc_stat, place_stat in conn.execute(
            "SELECT athlete_raw_id, fest_id, birth_year, association, place FROM athletes_raw"):
        idx, residence, by_rank, code, club_raw = entries.get(raw_id, (None,) * 5)
        residence = residence or place_stat or None
        pid, slug, p_assoc = portraits.get(raw_id, (None, None, None))
        club = reg.resolve(club_raw, code=code, festival_sub=fest_sub.get(fid)) if club_raw else None
        if club_raw and club is None and not pseudo_club_sub(club_raw) \
                and not code_in_club(club_raw):
            rep.unresolved_clubs[club_raw] += 1
        sub, source = None, None
        rank_code = _ranking_code(code, club_raw)
        codes = [None if _is_uniform(uniform, fid, src, c) else c
                 for c, src in ((rank_code, "ranking"), (assoc_stat, "sheet"))]
        blanket = _code_sub(rank_code if codes[0] is None else None) or _code_sub(
            assoc_stat if codes[1] is None else None)
        for cand, src in ((_code_sub(codes[0]), "code"),
                          (_code_sub(codes[1]), "code"),
                          (club.sub_association if club else None, "club"),
                          (sub_association_for_name(p_assoc), "portrait"),
                          (blanket, "festival"),
                          (fest_sub.get(fid) if fest_cat.get(fid) in _HOME_CATEGORIES else None,
                           "festival")):
            if cand:
                sub, source = cand, src
                break
        rep.uniform_rows += bool(blanket) and source != "code"
        birth = by_rank or (int(by_stat) if by_stat and str(by_stat).isdigit() else None)
        rows.append((raw_id, fid, idx, residence, birth, code, club_raw,
                     club.key if club else None, club.name if club else None, sub, source,
                     pid, slug))
        rep.rows += 1
        rep.with_club += club is not None
        rep.with_sub[source or "none"] += 1
        rep.with_portrait += pid is not None
    with conn:
        conn.execute("DELETE FROM clubs")
        conn.executemany(
            "INSERT INTO clubs (club_key, name, sub_association, n_obs, conflict, esv_id, votes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(c.key, c.name, c.sub_association, c.n, int(c.conflict), c.esv_id,
              ",".join(f"{k}:{v:g}" for k, v in sorted(c.votes.items(), key=lambda t: -t[1])))
             for c in clubs.values()])
        conn.execute("DELETE FROM athlete_evidence")
        conn.executemany(
            "INSERT INTO athlete_evidence (athlete_raw_id, fest_id, ranking_idx, residence, "
            "birth_year, assoc_code, club_raw, club_key, club, sub_association, "
            "sub_assoc_source, portrait_id, portrait_slug) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
    return rep
