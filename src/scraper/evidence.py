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


def _code_sub(code: str | None) -> str | None:
    if not code or code.split()[0] in GUEST_CODES:
        return None
    return sub_association_for_code(code)


def build_registry(conn: sqlite3.Connection) -> tuple[ClubRegistry, dict[int, str | None]]:
    fests = load_festivals(conn)
    fest_sub = {fid: sub_association_for_festival(f.name, f.category, f.association)
                for fid, f in fests.items()}
    home = {fid for fid, f in fests.items() if f.category in _HOME_CATEGORIES}
    reg = ClubRegistry(min_obs=MIN_CLUB_OBS)
    for fid, club, code, n in conn.execute(
            "SELECT fest_id, club_raw, assoc_code, COUNT(*) FROM ranking_entries "
            "WHERE club_raw IS NOT NULL GROUP BY 1, 2, 3"):
        reg.add(club, code=code, festival_sub=fest_sub.get(fid) if fid in home else None, count=n)
    for club, assoc, esv, n in conn.execute(
            "SELECT club_name, association_name, club_esv_id, COUNT(*) FROM portraits "
            "WHERE club_name IS NOT NULL GROUP BY 1, 2, 3"):
        reg.add(club, portrait_sub=sub_association_for_name(assoc), esv_id=esv, count=n)
    return reg, fest_sub


def build_evidence(conn: sqlite3.Connection) -> EvidenceReport:
    """Rebuild ``clubs`` and ``athlete_evidence`` (one transaction)."""
    rep = EvidenceReport()
    reg, fest_sub = build_registry(conn)
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
        for cand, src in ((_code_sub(code) or pseudo_club_sub(club_raw)
                           or _code_sub(strip_code(club_raw)[0] if club_raw else None), "code"),
                          (_code_sub(assoc_stat), "code"),
                          (club.sub_association if club else None, "club"),
                          (sub_association_for_name(p_assoc), "portrait"),
                          (fest_sub.get(fid) if fest_cat.get(fid) in _HOME_CATEGORIES else None,
                           "festival")):
            if cand:
                sub, source = cand, src
                break
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
