"""Evidence-based identity resolution (Phase 3 task 2): one ``athlete_id`` per person.

The statistic sheets carry no profile URL, so identities are inferred from the name
and the evidence attached to every raw row (``athlete_evidence``: canonical club,
Teilverband, residence, birth year, schlussgang portrait). The cascade:

1. **Blocks.** Every row is cleaned (:func:`names.clean_raw_name`) and blocked by
   :func:`names.name_key`. Rows that are not a name become singleton athletes.
2. **Anchors.** Every schlussgang portrait with the block's key joins it as an
   evidence-only pseudo cluster (club, city, Teilverband, birth year, ESV namesake
   number), so the registry can anchor rows from the years before portraits were
   linked (2011-22). Duplicate portraits of one person are collapsed (:func:`_registry`).
   An anchor is attached to rows only on evidence beyond the name
   (:func:`anchor_supported`).
3. **Atoms.** Rows of one portrait are one person (hard; a portrait linked to two
   rows of one date is not trusted). Other rows with the same signature (club,
   residence, birth year, Teilverband, printed ordinal) form an atom - unless the
   signature occurs twice on one date (namesakes with identical evidence): those rows
   stay single. A signature that returns after >= 5 seasons starts a new atom.
4. **Agglomeration** inside the block, best pair first (:func:`agglomerate`,
   :func:`pair_score`). Hard cannot-links (:func:`cannot_link`): same festival or
   date, different portraits, birth years >= 2 apart, an implausible age (< 14 /
   > 60) at a festival. The score is a prior for "same name" plus / minus evidence;
   two clusters merge while the best score is > 0. Pairs with an established side go
   first; an unusual age (< 16 / > 40) decides between candidates but does not split
   off rows that have no other candidate. A cluster that fits two persons of the
   block but collides with both by date is taken apart and its rows are assigned one
   by one. Rows with a nearly as good alternative person are flagged ``ambiguous``
   (:func:`_flag_ambiguous`, judged row by row on the final persons).
5. **One portrait, several spellings** are merged (hard), then **spelling variants**:
   clusters of near-identical keys (``?`` wildcard, spacing, swapped order, nickname,
   one edit) merge only under the rules of :func:`_variant_allowed` - never when the
   two spellings appear at the same festival or date (proven distinct names such as
   Stucki Simon / Timon), and not when the small side has two possible targets.
6. **Ids** (:func:`_athlete_ids`): ``<name-slug>-p<portrait_id>`` for anchored
   identities, else ``<name-slug>-<fest_id>-<idx>`` of the identity's earliest row from
   2011 on (:data:`ID_ANCHOR_FROM`; its earliest row if it has none). Both parts are
   facts of the source, so later festivals do not reshuffle ids, and seasons before 2011
   added later (Phase 10) do not rename anyone; an id changes only when its id row moves
   to another identity or a portrait gets attached / detached.

The statistic sheets have no profile slug (spec §4.1 "slug first"): the portrait link
(2023+) and the portrait registry take that role, then name + club and the other
evidence. Nothing is tuned to individual names.

Every raw row gets ``evidence`` (the strongest evidence it shares with the rest of
its identity) and a ``confidence``; every athlete lists why its name block was split
and which uncertainty flags apply (see :data:`CONFIDENCE`). Rows capped at 0.4:
``|ambiguous`` and ``|gap`` (the smaller side of a career gap of >= 8 seasons that no
club, residence or birth year bridges). The resolver raises if an identity ends up
with two rows on one date or with two portraits (:func:`_check_invariants`).
"""

from __future__ import annotations

import copy
import functools
import heapq
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pandas as pd

from src.pipeline import names
from src.pipeline.clubs import club_key, is_region_code
from src.pipeline.cleaner import NO_EVIDENCE_FLAGS, Resolution, ResolverInput, slugify

log = logging.getLogger("schwingen.clean")

# ----------------------------------------------------------------------------- parameters
PRIOR_SAME_NAME = 1.0      #: same name key, nothing else known
W_CLUB = 3.0               #: a shared canonical club with ESV id
W_CLUB_SOFT = 1.0          #: a shared club label without ESV id (regional codes ...)
W_CLUB_DISJOINT = -0.5     #: both sides have ESV clubs, none shared (club changes exist)
W_BIRTH_EQUAL = 3.0
W_BIRTH_NEAR = 1.0         #: birth years 1 apart (typos, age-based lists)
W_RESIDENCE = 2.0
W_TV = 1.0                 #: shared Teilverband from a strong source (code / club / portrait)
W_TV_DISJOINT = -2.0
W_ORDINAL = 1.0            #: same printed namesake ordinal ("(2)", "II")
W_ORDINAL_DISJOINT = -2.0
W_OPPONENTS = 1.0          #: >= MIN_SHARED_OPPONENTS shared opponents, >= 30 % of the smaller side
W_GAP = -0.5               #: careers >= GAP_SEASONS seasons apart: tips the balance only
                           #: together with other evidence against; otherwise a flag
GAP_SEASONS = 5
UNBRIDGED_GAP_SEASONS = 8  #: a gap this long with no club / residence / birth year shared
                           #: across it: the smaller side's rows are capped at 0.4
MIN_SHARED_OPPONENTS = 3
MIN_AGE, MAX_AGE = 14, 60  #: plausible age of an active athlete at a festival
YOUTH_MIN_AGE = 8          #: a printed birth year implying age 8-13 is a leaked youth entry:
                           #: the year is kept as evidence, the row is exempt from the age rules
YOUNG_AGE = 16             #: below this age a link needs positive evidence (0.3 % of rows)
W_YOUNG = -2.0
OLD_AGE = 40               #: above this age a link is unlikely (0.6 % of the portrait-linked
W_OLD = -2.0               #: rows, 17 of 794 persons): tips the choice between namesakes
AMBIGUITY_MARGIN = 0.75    #: a cannot-linked alternative this close makes a merge ambiguous
VARIANT_MAX_SPAN_GAP = 2   #: seasons between two spellings of one person
VARIANT_BIG_SIDE = 10      #: both spellings with >= this many rows need club / birth year
ESTABLISHED_TOKEN_KEYS = 3  #: a name token used by >= this many keys is a real name, not a typo
FRAGMENT_ROWS, FRAGMENT_NEXT_TO = 2, 10  #: flag `fragment`: <= 2 rows inside a >= 10-row namesake's career
TV_MIN_SHARE = 0.25        #: a Teilverband counts for a cluster from this share of its rows
TV_SEEN_ROWS = 2           #: ... and is no evidence against once it has this many rows
STRONG_TV_SOURCES = frozenset({"code", "club", "portrait"})
ANCHOR_EVIDENCE = frozenset({"club", "birth_year", "residence", "ordinal"})

#: per-row confidence by linking evidence: (name is unique, name has namesakes)
CONFIDENCE: dict[str, tuple[float, float]] = {
    "portrait": (1.0, 1.0), "club": (0.95, 0.95), "birth_year": (0.95, 0.95),
    "residence": (0.9, 0.9), "teilverband": (0.9, 0.7), "name": (0.85, 0.4),
    "not_a_name": (0.0, 0.0),
}
CONFIDENCE_AMBIGUOUS = 0.4   #: cap for rows whose assignment had a close alternative
CONFIDENCE_UNBRIDGED = 0.4   #: cap for rows beyond an unbridged career gap (father / son?)
CONFIDENCE_VARIANT = 0.9     #: factor for rows merged across spellings

_PORTRAIT_TV = {"Innerschweiz": "ISV", "Nordostschweiz": "NOSV", "Bern": "BKSV",
                "Suedwestschweiz": "SWSV", "Südwestschweiz": "SWSV", "Nordwestschweiz": "NWSV"}
_CANTON_RE = re.compile(r"\s+[A-Z]{2}$")
#: 'Lausanne & Environs', 'Fribourg et environs', 'Thun u. Umgebung': Romand lists print
#: the club in the residence column - the place is what is compared
_ENVIRONS_RE = re.compile(r"\s*(?:&|\bet\b|\bund\b|\bu\.?)\s*(?:environs|env\.?|umgebung|umg\.?)\s*$",
                          re.I)
_ESV_ORDINAL_RE = re.compile(r"\((\d)\)")


def _residence_key(value: object) -> str | None:
    """'Oberriet SG' / 'Oberriet' -> 'oberriet' (canton code dropped, folded)."""
    if not isinstance(value, str):
        return None
    k = names.name_key(_ENVIRONS_RE.sub("", _CANTON_RE.sub("", value.strip())))
    return k or None


def _int(value: object) -> int | None:
    try:
        if value is None or pd.isna(value):
            return None
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@functools.lru_cache(maxsize=None)
def _club_key(name: str) -> str:
    """Comparison key of a club name (:func:`clubs.club_key`: spelling, generic words
    and old names folded); a name without a key is its own key."""
    return club_key(name) or name


# ----------------------------------------------------------------------------- clusters
@dataclass
class Cluster:
    """A set of raw rows (and at most one registry anchor) believed to be one person."""

    order: int                                   # smallest row position (deterministic ties)
    key: str
    rows: list[int] = field(default_factory=list)
    portraits: set[int] = field(default_factory=set)
    anchor: int | None = None                    # registry portrait attached without a row link
    clubs: set[str] = field(default_factory=set)
    soft_clubs: set[str] = field(default_factory=set)
    births: Counter[int] = field(default_factory=Counter)
    residences: set[str] = field(default_factory=set)
    tv_n: Counter[str] = field(default_factory=Counter)
    ordinals: set[str] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)
    years: set[int] = field(default_factory=set)
    age_years: set[int] = field(default_factory=set)  # years minus those of leaked youth rows
    opponents: set[str] = field(default_factory=set)
    keys: Counter[str] = field(default_factory=Counter)
    ambiguous: set[int] = field(default_factory=set)
    variants: dict[int, str] = field(default_factory=dict)  # row -> variant kind
    weak: bool = False                           # exploded row: never a core on its own

    def birth(self) -> int | None:
        if not self.births:
            return None
        top = max(self.births.values())
        return min(b for b, n in self.births.items() if n == top)

    @property
    def tvs(self) -> set[str]:
        """Teilverbände with >= 25 % of the cluster's votes (a single guest listing
        must not make the cluster compatible with every namesake of that region)."""
        total = sum(self.tv_n.values())
        return {t for t, n in self.tv_n.items() if n >= TV_MIN_SHARE * total}

    def absorb(self, other: "Cluster") -> None:
        self.order = min(self.order, other.order)
        self.rows += other.rows
        self.portraits |= other.portraits
        self.anchor = self.anchor if self.anchor is not None else other.anchor
        self.clubs |= other.clubs
        self.soft_clubs |= other.soft_clubs
        self.births.update(other.births)
        self.residences |= other.residences
        self.tv_n.update(other.tv_n)
        self.ordinals |= other.ordinals
        self.dates |= other.dates
        self.years |= other.years
        self.age_years |= other.age_years
        self.opponents |= other.opponents
        self.keys.update(other.keys)
        self.ambiguous |= other.ambiguous
        self.variants.update(other.variants)
        self.weak = self.weak and other.weak


def _season_gap(a: Cluster, b: Cluster) -> int | None:
    """Seasons between two careers (0 = overlapping / adjacent); None for an anchor."""
    if not a.years or not b.years:
        return None
    lo, hi = (a, b) if min(a.years) <= min(b.years) else (b, a)
    return max(0, min(hi.years) - max(lo.years) - 1)


def cannot_link(a: Cluster, b: Cluster, dates: bool = True) -> str | None:
    """Reason why two clusters cannot be one person, or None (``dates=False``: leave
    the same-date rule out)."""
    pa, pb = a.portraits | ({a.anchor} - {None}), b.portraits | ({b.anchor} - {None})
    if pa and pb:
        if pa != pb:
            return "portrait"
    if dates and not a.dates.isdisjoint(b.dates):
        return "same_date"
    ba, bb = a.birth(), b.birth()
    if ba is not None and bb is not None and abs(ba - bb) >= 2:
        return "birth_year"
    for birth, years in ((ba, b.age_years), (bb, a.age_years)):
        if birth is not None and years and (
                min(years) - birth < MIN_AGE or max(years) - birth > MAX_AGE):
            return "age"
    return None


def pair_score(a: Cluster, b: Cluster, age_penalty: bool = True,
               opponents: bool = True) -> tuple[float, list[str]]:
    """Evidence score for "a and b are one person" (callers check :func:`cannot_link`).

    Returns ``(score, reasons)``: reasons name the positive evidence (``club``,
    ``birth_year``, ``residence``, ``teilverband``, ``ordinal``, ``opponents``) and,
    prefixed with ``-``, the evidence against. ``age_penalty=False`` leaves out the
    penalty for an unusual age (< 16 / > 40): an unusual age only decides between
    candidates (see :func:`agglomerate`). ``opponents=False`` leaves out the shared
    opponents: they order merges, but namesakes of one club share them too, so they
    do not make an assignment certain (see :func:`_flag_ambiguous`)."""
    s, why = PRIOR_SAME_NAME, []
    if a.clubs & b.clubs:
        s += W_CLUB
        why.append("club")
    elif a.clubs and b.clubs:
        s += W_CLUB_DISJOINT
        why.append("-club")
    if a.soft_clubs & b.soft_clubs:
        s += W_CLUB_SOFT
    ba, bb = a.birth(), b.birth()
    if ba is not None and bb is not None:
        if ba == bb:
            s += W_BIRTH_EQUAL
            why.append("birth_year")
        else:
            s += W_BIRTH_NEAR
    if a.residences & b.residences:
        s += W_RESIDENCE
        why.append("residence")
    if a.tvs & b.tvs:
        s += W_TV
        why.append("teilverband")
    elif a.tvs and b.tvs and not (
            any(a.tv_n[t] >= TV_SEEN_ROWS for t in b.tvs)
            or any(b.tv_n[t] >= TV_SEEN_ROWS for t in a.tvs)):
        # a Teilverband the other side already shows on several rows (guest starts,
        # double membership) is neither evidence for nor against
        s += W_TV_DISJOINT
        why.append("-teilverband")
    if a.ordinals & b.ordinals:
        s += W_ORDINAL
        why.append("ordinal")
    elif a.ordinals and b.ordinals:
        s += W_ORDINAL_DISJOINT
        why.append("-ordinal")
    if opponents and a.opponents and b.opponents:
        shared = len(a.opponents & b.opponents)
        share = shared / min(len(a.opponents), len(b.opponents))
        if shared >= MIN_SHARED_OPPONENTS and share >= 0.3:
            s += W_OPPONENTS
            why.append("opponents")
        s += 0.3 * share                       # tie-break only
    for birth, years in ((ba, b.age_years), (bb, a.age_years)) if age_penalty else ():
        if birth is not None and years and ba != bb and (
                min(years) - birth < YOUNG_AGE or max(years) - birth > OLD_AGE):
            s += W_YOUNG if min(years) - birth < YOUNG_AGE else W_OLD
            why.append("-age")
            break
    gap = _season_gap(a, b)
    if gap is not None:
        if gap >= GAP_SEASONS:
            s += W_GAP
            why.append("-gap")
        s += 0.2 / (1 + gap)                   # tie-break only
    return s, why


def _is_core(c: Cluster) -> bool:
    """A cluster that carries identifying evidence (portrait, ESV club or birth year)."""
    return not c.weak and bool(c.portraits or c.anchor is not None or c.clubs or c.births)


def anchor_supported(a: Cluster, b: Cluster, why: list[str]) -> bool:
    """A registry portrait without linked rows is attached to rows only on evidence
    beyond the name (club, birth year, residence, ESV namesake number): the registry
    also lists veterans, officials and children with the same name."""
    if bool(a.rows) == bool(b.rows):
        return True
    return bool(ANCHOR_EVIDENCE & set(why))


#: Seasons before 2011 (Phase 10) carry little beyond the name - no portraits, hardly a
#: birth year, a residence on a part of the rows, name-only rows for opponents a sheet
#: does not print - and no Regional festivals, so careers have holes. A career that lies
#: entirely before 2011 is therefore joined to a later one across this many seasons
#: without a festival only on evidence beyond the name (club, residence, birth year,
#: namesake number): the name alone would join two generations of a family, and would
#: move a newcomer's first season back by a decade (the publication rule for athletes of
#: unknown age looks at the first season).
HISTORY_BEFORE = 2011
HISTORY_GAP_SEASONS = 5
BRIDGE_EVIDENCE = frozenset({"club", "birth_year", "residence", "ordinal"})


def history_gap(a: Cluster, b: Cluster, why: list[str]) -> bool:
    """True if joining ``a`` and ``b`` would bridge a long gap between a career that
    ended before 2011 and a later one on the name alone (see HISTORY_GAP_SEASONS)."""
    if not a.years or not b.years or BRIDGE_EVIDENCE & set(why):
        return False
    lo, hi = (a, b) if min(a.years) <= min(b.years) else (b, a)
    return max(lo.years) < HISTORY_BEFORE and min(hi.years) - max(lo.years) - 1 >= HISTORY_GAP_SEASONS


def link_score(a: Cluster, b: Cluster, relaxed: bool = False) -> float | None:
    """Score of a permitted merge, or None.

    Normally the score must be > 0. ``relaxed``: a pair that fails only because of
    the age penalty is permitted as well (ranked by its penalised score) - used once
    every other merge is done, i.e. when the rows have no other candidate."""
    if cannot_link(a, b) is not None:
        return None
    s, why = pair_score(a, b)
    if not anchor_supported(a, b, why) or history_gap(a, b, why):
        return None
    if s > 0:
        return s
    if relaxed and "-age" in why and pair_score(a, b, age_penalty=False)[0] > 0:
        return s
    return None


def agglomerate(clusters: list[Cluster],
                singles: dict[int, Cluster] | None = None) -> list[Cluster]:
    """Greedy best-pair-first merging; deterministic (ties by cluster order).

    Pairs with a core side (see :func:`_is_core`) go first: rows without identifying
    evidence are attached to the established persons of the block before they are
    merged with each other - otherwise two such rows of different namesakes can
    form a cluster that fits none of them.

    Three passes: (1) pairs with a core side; (2) the same, now also pairs that failed
    only because of an unusual age (15-year-olds, over-40s) - the penalty therefore
    decides between candidates but does not split off rows that have no other
    established person to belong to; (3) all remaining pairs.

    ``singles``: one cluster per row (row position -> cluster), used to flag ambiguous
    assignments row by row (see :func:`_flag_ambiguous`)."""
    atoms = [copy.deepcopy(c) for c in clusters]
    live: dict[int, Cluster] = dict(enumerate(clusters))
    version = {i: 0 for i in live}
    for relaxed, core_only in ((False, True), (True, True), (True, False)):
        _agglomerate_pass(live, version, relaxed, core_only)
    done = sorted(live.values(), key=lambda c: c.order)
    _flag_ambiguous(atoms, done, singles or {})
    return done


def _flag_ambiguous(atoms: list[Cluster], done: list[Cluster],
                    singles: dict[int, Cluster]) -> None:
    """Flag the rows whose assignment had a nearly as good alternative.

    Judged on the final persons of the block (not at merge time, when a person is
    still in pieces): a row is ambiguous when another person - a cluster with rows or
    an unused registry anchor - is a permitted link for it and fits within
    :data:`AMBIGUITY_MARGIN` of how well the row's atom fits the rest of its own
    person. Rows are judged one by one because an atom (rows with identical evidence)
    of two namesakes sharing club and village may itself be a mix. A row that meets
    the other person on its date is decided - unless that person's row of the day
    could equally be swapped with it (both at the festival, nothing tells them
    apart). The flag always goes to rows; a registry anchor has none."""
    if len(done) < 2:
        return
    home: dict[int, Cluster] = {}
    for c in done:
        for i in c.rows:
            home[i] = c
        for p in c.portraits | ({c.anchor} - {None}):
            home[-1 - p] = c
    parts: dict[int, list[Cluster]] = defaultdict(list)
    for t in atoms:
        ref = t.rows[0] if t.rows else -1 - t.anchor  # type: ignore[operator]
        if ref in home:
            parts[id(home[ref])].append(t)
    own: dict[int, float] = {}                 # row -> its fit to the rest of its own person
    fixed: set[int] = set()                    # portrait-linked rows: never in question
    as_row: dict[int, Cluster] = {}
    for c in done:
        mine = parts[id(c)]
        for t in mine:
            if t.portraits:
                fixed.update(t.rows)
            rest = Cluster(order=c.order, key=c.key)
            for o in mine:
                if o is not t:
                    rest.absorb(copy.deepcopy(o))
            rest.weak = False
            for i in t.rows:                   # the row against its person without its atom
                as_row[i] = singles.get(i, t)
                own[i] = pair_score(as_row[i], rest, opponents=False)[0] if len(mine) > 1 else 0.0

    def fits(i: int, g: Cluster, dates: bool) -> bool:
        """Could row ``i`` be person ``g`` nearly as well as its own person?"""
        row = as_row[i]
        if cannot_link(row, g, dates=dates) is not None:
            return False
        s, why = pair_score(row, g, opponents=False)
        if not anchor_supported(row, g, why):
            return False
        if s <= 0 and not ("-age" in why and pair_score(
                row, g, age_penalty=False, opponents=False)[0] > 0):
            return False
        return s >= own[i] - AMBIGUITY_MARGIN

    for c in done:
        if len(parts[id(c)]) < 2:
            continue
        for i in c.rows:
            if i in fixed:
                continue
            day = singles[i].dates if i in singles else set()
            for g in done:
                if g is c:
                    continue
                if fits(i, g, dates=True):
                    c.ambiguous.add(i)
                    break
                if len(parts[id(g)]) < 2:      # a lone row is nobody to swap with
                    continue
                rivals = [j for j in g.rows if j in singles and singles[j].dates == day]
                if rivals and fits(i, g, dates=False) and any(
                        j not in fixed and fits(j, c, dates=False) for j in rivals):
                    c.ambiguous.add(i)
                    break


def _agglomerate_pass(live: dict[int, Cluster], version: dict[int, int], relaxed: bool,
                      core_only: bool) -> None:
    heap: list[tuple[int, float, int, int, int, int, int, int]] = []

    def push(i: int, j: int) -> None:
        a, b = live[i], live[j]
        s = link_score(a, b, relaxed)
        if s is not None:
            lo, hi = sorted((a.order, b.order))
            tier = 0 if _is_core(a) or _is_core(b) else 1
            if tier and core_only:
                return
            heapq.heappush(heap, (tier, -s, lo, hi, i, j, version[i], version[j]))

    ids = sorted(live)
    for x, i in enumerate(ids):
        for j in ids[x + 1:]:
            push(i, j)
    while heap:
        _, neg, _, _, i, j, vi, vj = heapq.heappop(heap)
        if i not in live or j not in live or version[i] != vi or version[j] != vj:
            continue
        a, b = live[i], live[j]
        a.absorb(b)
        del live[j]
        version[i] += 1
        for k in sorted(live):
            if k != i:
                push(*sorted((i, k)))


# ----------------------------------------------------------------------------- variants
def variant_kind(a: str, b: str) -> str:
    """How two different keys relate: wildcard / spacing / swap / alias / edit."""
    if "?" in a or "?" in b:
        return "wildcard"
    if names.compact_key(a) == names.compact_key(b):
        return "spacing"
    if sorted(a.split()) == sorted(b.split()):
        return "swap"
    ta, tb = a.split(), b.split()
    if len(ta) == len(tb) and ta[:-1] == tb[:-1] and (
            names.first_name_canonical(ta[-1]) == names.first_name_canonical(tb[-1])):
        return "alias"
    return "edit"


def _differing_tokens(a: str, b: str) -> tuple[str, str] | None:
    ta, tb = a.split(), b.split()
    if len(ta) != len(tb):
        return None
    diff = [(x, y) for x, y in zip(ta, tb) if x != y]
    return diff[0] if len(diff) == 1 else None


def _variant_allowed(kind: str, a: Cluster, b: Cluster, score: float, why: list[str],
                     keys_cooccur: bool, both_established: bool) -> bool:
    """May two clusters with different (similar) name keys be one person?

    * wildcard / spacing / swap (similarity >= 0.9): no net evidence against, careers
      at most :data:`VARIANT_MAX_SPAN_GAP` seasons apart (or positive evidence).
    * alias / one edit (0.85): never when the two spellings occur at the same festival
      or date; needs a shared club, birth year, or residence (without a Teilverband
      conflict); two big sides need club or birth year; when both differing tokens are
      established names (Simon / Timon, Keller / Koller) only an equal birth year
      counts - brothers share club and village.
    """
    strong = {"club", "birth_year"} & set(why)
    if kind in ("wildcard", "spacing", "swap"):
        if score - PRIOR_SAME_NAME < 0:
            return False
        gap = _season_gap(a, b)
        return bool(strong or "residence" in why or gap is None or gap <= VARIANT_MAX_SPAN_GAP)
    if keys_cooccur:
        return False
    if kind == "edit" and both_established:
        return "birth_year" in why
    if len(a.rows) >= VARIANT_BIG_SIDE and len(b.rows) >= VARIANT_BIG_SIDE:
        return bool(strong)
    return bool(strong or ("residence" in why and "-teilverband" not in why))


# ----------------------------------------------------------------------------- resolver
class EvidenceResolver:
    """The real resolver: see the module docstring for the cascade."""

    name = "evidence"

    def resolve(self, inp: ResolverInput) -> Resolution:  # noqa: C901 - one linear pipeline
        raw = inp.raw.sort_values(["fest_date", "fest_id", "idx"], kind="stable").reset_index(
            drop=True)
        n = len(raw)
        portraits = getattr(inp, "portraits", None)
        esv_clubs: set[str] | None = None      # club keys with an ESV club id
        if portraits is not None and len(portraits):
            esv_clubs = {_club_key(v) for v in portraits["club_name"].dropna().unique()}

        # ---- per-row features
        cleaned = [names.clean_raw_name(r, y) for r, y in zip(raw["name_raw"], raw["fest_year"])]
        display = [c[0] or str(nm) for c, nm in zip(cleaned, raw["name"])]
        keys = [names.name_key(d) for d in display]
        usable = [not (NO_EVIDENCE_FLAGS & set(str(f or "").split(","))) for f in raw["flags"]]
        not_name = ["not_a_name" in c[1].flags or not k for c, k in zip(cleaned, keys)]
        rid = raw["athlete_raw_id"].tolist()
        fest = raw["fest_id"].tolist()
        date = raw["fest_date"].tolist()
        year = [int(y) for y in raw["fest_year"]]
        registry, canon = _registry(portraits, set(
            int(v) for v in raw["portrait_id"].dropna()))
        pid = [canon.get(_int(v), _int(v)) if u and _int(v) is not None else None
               for v, u in zip(raw["portrait_id"], usable)]
        # one person is at one festival per day: a portrait linked to two rows of one
        # date is a wrong link on at least one of them - neither is trusted
        per_day = Counter((p, d) for p, d in zip(pid, date) if p is not None)
        clash = [i for i in range(n) if pid[i] is not None and per_day[(pid[i], date[i])] > 1]
        for i in clash:
            pid[i] = None
        if clash:
            log.warning("clean: %d rows share their portrait with another row of the same "
                        "date - portrait links ignored (e.g. %s)", len(clash), rid[clash[0]])
        club_name = [_str(v) if u else None for v, u in zip(raw["club"], usable)]
        club = [_club_key(v) if v else None for v in club_name]  # compared by key
        tv_src = raw["sub_assoc_source"].tolist()
        tv = [_str(v) if u and s in STRONG_TV_SOURCES else None
              for v, u, s in zip(raw["sub_association"], usable, tv_src)]
        res = [_residence_key(v) if u else None for v, u in zip(raw["residence"], usable)]
        birth: list[int | None] = []
        youth: list[bool] = []                 # leaked youth entry (printed age 8-13)
        for v, c, y, u in zip(raw["birth_year"], cleaned, year, usable):
            b = _int(v) if _int(v) is not None else c[1].birth_year
            birth.append(b if u and b is not None and YOUTH_MIN_AGE <= y - b <= MAX_AGE else None)
            youth.append(birth[-1] is not None and y - birth[-1] < MIN_AGE)
        ordinal: list[str | None] = []
        for r, c in zip(raw["name_raw"], cleaned):
            m = _ESV_ORDINAL_RE.search(r)       # ESV lists (2023+) number namesakes globally
            ordinal.append(m.group(1) if m else
                           {"I": "1", "II": "2", "III": "3"}.get(c[1].ordinal or ""))
        pos = {r: i for i, r in enumerate(rid)}
        opp: list[set[str]] = [set() for _ in range(n)]
        for a_id, b_id in zip(inp.bouts["athlete_a_id"], inp.bouts["athlete_b_id"]):
            i, j = pos.get(a_id), pos.get(b_id)
            if i is not None and j is not None:
                opp[i].add(keys[j])
                opp[j].add(keys[i])

        def row_cluster(i: int) -> Cluster:
            c = Cluster(order=i, key=keys[i], rows=[i], dates={date[i]}, years={year[i]},
                        age_years=set() if youth[i] else {year[i]})
            c.keys[keys[i]] += 1
            if pid[i] is not None:
                c.portraits.add(pid[i])
            if club[i]:
                (c.clubs if esv_clubs is None or club[i] in esv_clubs else c.soft_clubs).add(
                    club[i])
            if birth[i] is not None:
                c.births[birth[i]] += 1
            if res[i]:
                c.residences.add(res[i])
            if tv[i]:
                c.tv_n[tv[i]] += 1
            if ordinal[i]:
                c.ordinals.add(ordinal[i])
            if usable[i]:
                c.opponents |= opp[i]
            return c

        # ---- blocks, anchors, atoms, agglomeration
        blocks: dict[str, list[int]] = defaultdict(list)
        for i, k in enumerate(keys):
            if not not_name[i]:
                blocks[k].append(i)
        pinfo: dict[int, dict[str, object]] = {}
        key_anchors: dict[str, list[int]] = defaultdict(list)
        for p_id in sorted(registry):
            p = registry[p_id]
            pinfo[p_id] = {"slug": p["slug"], "birth": p["birth"], "club": p["club"]}
            key_anchors[p["key"]].append(p_id)

        def make_anchor(p_id: int) -> Cluster:
            p = registry[p_id]
            a = Cluster(order=10**9 + p_id, key=p["key"], anchor=p_id)
            a.clubs |= p["clubs"]
            if p["birth"] is not None:
                a.births[p["birth"]] += 1000   # the registry birthday outvotes printed years
            a.residences |= p["cities"]
            a.tv_n.update(p["tvs"])
            if p["ordinal"]:                   # "Gasser Dominik (1)": the ESV namesake number
                a.ordinals.add(p["ordinal"])
            return a

        def build_block(k: str, explode: frozenset[int] = frozenset()) -> list[Cluster]:
            """Atoms + anchors of one name block; rows in ``explode`` stay single."""
            atoms: dict[tuple, Cluster] = {}
            singles.clear()
            linked: set[int] = set()
            sigs: dict[int, tuple] = {}
            seen: dict[tuple, set[str]] = defaultdict(set)
            shared: set[tuple] = set()         # signatures used by two people at once
            last: dict[tuple, int] = {}        # signature -> year of its latest row
            run: Counter[tuple] = Counter()
            for i in blocks[k]:
                if pid[i] is not None:
                    sigs[i] = ("p", pid[i])
                    linked.add(pid[i])
                elif i not in explode and (club[i] or res[i] or birth[i] is not None):
                    sg = ("s", club[i], res[i], birth[i], tv[i], ordinal[i])
                    # the same evidence after >= GAP_SEASONS seasons without it is a new
                    # atom (father / son share club and village): the scores decide
                    if year[i] - last.get(sg, year[i]) - 1 >= GAP_SEASONS:
                        run[sg] += 1
                    last[sg] = year[i]
                    sigs[i] = sg = (*sg, run[sg])
                    if date[i] in seen[sg]:
                        shared.add(sg)
                    seen[sg].add(date[i])
                else:
                    sigs[i] = ("r", i)
            for i in blocks[k]:
                c = row_cluster(i)
                singles[i] = row_cluster(i)
                sg = sigs[i]
                if sg in shared:               # namesakes with identical evidence
                    sg, c.weak = ("r", i), True
                c.weak = c.weak or i in explode
                if sg in atoms:
                    atoms[sg].absorb(c)
                else:
                    atoms[sg] = c
            block = list(atoms.values())
            for p_id in key_anchors.get(k, ()):
                a = make_anchor(p_id)
                if p_id in linked:             # a linked portrait is the anchor itself
                    atoms[("p", p_id)].absorb(a)
                else:
                    block.append(a)
            return sorted(block, key=lambda c: c.order)

        singles: dict[int, Cluster] = {}       # per-row clusters of the current block
        clusters: list[Cluster] = []
        splits: dict[str, Counter[str]] = {}
        n_exploded = 0
        for k in sorted(blocks):
            done = agglomerate(build_block(k), singles)
            # a cluster that fits two persons of the block but collides with both by date
            # mixes their rows (namesakes sharing club / village): assign its rows one by
            # one instead. Only date collisions count: a cluster kept apart from two
            # namesakes by birth year or age is a third person (a father), not a mix
            torn = frozenset(
                i for u in done if len(u.rows) > 1 and not u.portraits and u.anchor is None
                and sum(1 for v in done if v is not u and cannot_link(u, v) == "same_date"
                        and pair_score(u, v)[0] > PRIOR_SAME_NAME) >= 2
                for i in u.rows)
            if torn:
                n_exploded += len(torn)
                done = agglomerate(build_block(k, torn), singles)
            real = [c for c in done if c.rows]
            if len(real) > 1:
                splits[k] = Counter()
                for x, a in enumerate(real):
                    for b in real[x + 1:]:
                        why = cannot_link(a, b) or ",".join(
                            w for w in pair_score(a, b)[1] if w.startswith("-")) or "score"
                        splits[k][why] += 1
            clusters += done

        clusters = _merge_same_portrait(clusters, pid)
        clusters = self._merge_variants(clusters, blocks, date)

        # ---- output
        real = [c for c in clusters if c.rows]
        for c in real:
            c.key = min(c.keys, key=lambda k: (-c.keys[k], k))
        per_key = Counter(c.key for c in real)
        ids = _athlete_ids(real, rid, keys, registry, date)
        long_careers: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for c in real:
            if len(c.rows) >= FRAGMENT_NEXT_TO:
                long_careers[c.key].append((min(c.years), max(c.years)))
        ident_rows: list[tuple[str, str, float, str]] = []
        athletes: list[dict[str, object]] = []
        for c, aid in zip(real, ids):
            multi = per_key[c.key] > 1
            club_n = Counter(club[i] for i in c.rows if club[i])
            res_n = Counter(res[i] for i in c.rows if res[i])
            tv_n = Counter(tv[i] for i in c.rows if tv[i])
            portrait = next(iter(c.portraits), c.anchor)
            info = pinfo.get(portrait, {}) if portrait is not None else {}
            if c.anchor is not None and not c.portraits:   # registry values count as support
                if info.get("club"):
                    club_n[_club_key(str(info["club"]))] += 1
            cbirth = c.birth()
            seasons = sorted(c.years)
            gap, gap_at = max(((b - a - 1, b) for a, b in zip(seasons, seasons[1:])),
                              default=(0, 0))
            beyond: set[int] = set()           # rows beyond a gap that nothing bridges
            if gap >= UNBRIDGED_GAP_SEASONS:
                sides = [[i for i in c.rows if year[i] < gap_at],
                         [i for i in c.rows if year[i] >= gap_at]]
                ev = [({club[i] for i in side} | {res[i] for i in side}
                       | {birth[i] for i in side}) - {None} for side in sides]
                reg = registry.get(portrait, {}) if portrait is not None else {}
                known = reg.get("clubs", set()) | reg.get("cities", set()) | {reg.get("birth")}
                if not ev[0] & ev[1] and not (ev[0] & known and ev[1] & known):
                    linked = [any(pid[i] is not None for i in side) for side in sides]
                    small = 0 if len(sides[0]) < len(sides[1]) else 1
                    if linked[small] and not linked[1 - small]:
                        small = 1 - small
                    beyond = set(sides[small]) - {i for i in sides[small] if pid[i] is not None}
            confs: list[float] = []
            for i in c.rows:
                if pid[i] is not None:
                    how = "portrait"
                elif club[i] and club_n[club[i]] >= 2:
                    how = "club"
                elif birth[i] is not None and (c.births[birth[i]] >= 2 or birth[i] == cbirth
                                               ) and len(c.rows) > 1:
                    how = "birth_year"
                elif res[i] and res_n[res[i]] >= 2:
                    how = "residence"
                elif tv[i] and tv_n[tv[i]] >= 2:
                    how = "teilverband"
                else:
                    how = "name"
                conf = CONFIDENCE[how][1 if multi else 0]
                label = "name" if how == "name" else f"name+{how}"
                if i in c.ambiguous and how != "portrait":
                    conf, label = min(conf, CONFIDENCE_AMBIGUOUS), label + "|ambiguous"
                if i in beyond:
                    conf, label = min(conf, CONFIDENCE_UNBRIDGED), label + "|gap"
                if i in c.variants:
                    conf, label = conf * CONFIDENCE_VARIANT, f"variant:{c.variants[i]}|{label}"
                confs.append(conf)
                ident_rows.append((rid[i], aid, round(conf, 3), label))
            notes = []
            if multi:
                why = ",".join(sorted(splits.get(c.key, {}))) or "variant"
                notes.append(f"namesakes={per_key[c.key]}({why})")
            if len(c.keys) > 1:
                notes.append("spellings=" + "/".join(sorted(c.keys)))
            if c.anchor is not None and not c.portraits:
                notes.append("registry_anchor")
            if c.ambiguous:
                notes.append(f"ambiguous_rows={len(c.ambiguous)}")
            if len(c.tvs) > 1:
                notes.append("teilverbaende=" + "/".join(sorted(c.tvs)))
            if gap >= GAP_SEASONS:
                notes.append(f"career_gap={gap}")
            if beyond:
                notes.append(f"unbridged_gap_rows={len(beyond)}")
            if multi and len(c.rows) <= FRAGMENT_ROWS and c.key in long_careers and any(
                    lo - 1 <= min(c.years) and max(c.years) <= hi + 1
                    for lo, hi in long_careers[c.key]):
                notes.append("fragment")
            spell = Counter(display[i] for i in c.rows if keys[i] == c.key)
            full = min(spell, key=lambda s: (-spell[s], -sum(ord(ch) > 127 for ch in s), s))
            # display club: a Schwingklub, not a regional-association code ('TO', 'RO')
            last_club = [club_name[i] for i in c.rows if club[i] and (
                club[i] in (esv_clubs or ()) or not is_region_code(club_name[i]))]
            athletes.append({
                "athlete_id": aid, "full_name": full,
                "club": _latest_mode(last_club) or info.get("club"),
                "sub_association": _latest_mode([tv[i] for i in c.rows if tv[i]]) or _latest_mode(
                    [v for i in c.rows if (v := _str(raw["sub_association"].iat[i]))]),
                "birth_year": info.get("birth") or cbirth,
                "slug": info.get("slug") or _latest_mode(
                    [v for i in c.rows if (v := _str(raw["portrait_slug"].iat[i]))]),
                "confidence": round(sum(confs) / len(confs), 3),
                "evidence": ";".join(notes) or "unique_name",
            })
        for i in range(n):                     # garbage names: one athlete per row
            if not_name[i]:
                aid = f"{slugify(display[i])}-{fest[i]}-{int(raw['idx'].iat[i]):03d}"
                ident_rows.append((rid[i], aid, 0.0, "not_a_name"))
                athletes.append({"athlete_id": aid, "full_name": display[i] or "?",
                                 "confidence": 0.0, "evidence": "not_a_name"})
        identity = pd.DataFrame(ident_rows, columns=["athlete_raw_id", "athlete_id",
                                                     "confidence", "evidence"])
        _check_invariants(real, date, pid, rid)
        order = {r: i for i, r in enumerate(rid)}
        identity = identity.sort_values("athlete_raw_id", key=lambda s: s.map(order),
                                        kind="stable").reset_index(drop=True)
        ath = pd.DataFrame(athletes, columns=["athlete_id", "full_name", "club",
                                              "sub_association", "birth_year", "slug",
                                              "confidence", "evidence"])
        log.info("clean: evidence resolver: %d name keys, %d with namesakes, %d identities "
                 "with several spellings, %d registry-anchored, %d rows assigned one by one",
                 len(per_key),
                 sum(v > 1 for v in per_key.values()), sum(len(c.keys) > 1 for c in real),
                 sum(c.anchor is not None and not c.portraits for c in real), n_exploded)
        return Resolution(identity=identity, athletes=ath.sort_values("athlete_id").reset_index(
            drop=True))

    @staticmethod
    def _merge_variants(clusters: list[Cluster], blocks: dict[str, list[int]],
                        date: list[str]) -> list[Cluster]:
        """Step 5: merge clusters of near-identical name keys (see _variant_allowed)."""
        by_key: dict[str, list[Cluster]] = defaultdict(list)
        for c in clusters:
            by_key[c.key].append(c)
        key_dates = {k: {date[i] for i in rows} for k, rows in blocks.items()}
        # "established" name tokens (used by several keys): two spellings that both occur
        # from 2011 on are judged on the names of those seasons, as before the history
        # was added (more seasons mean more keys per token, which would silently undo
        # merges of published athletes); any other pair on all names, the stricter count
        tokens: Counter[str] = Counter()
        tokens_recent: Counter[str] = Counter()
        recent = {k for k, d in key_dates.items() if max(d, default="") >= ID_ANCHOR_FROM}
        for k in blocks:
            tokens.update(set(k.split()))
            if k in recent:
                tokens_recent.update(set(k.split()))
        index = names.NameIndex(blocks)
        pairs: list[tuple[float, str, str]] = []
        for k in sorted(blocks):
            for k2, sim in index.candidates(k, 0.85):
                if k < k2:
                    pairs.append((-sim, k, k2))
        def check(k: str, k2: str, a: Cluster, b: Cluster) -> float | None:
            if not (a.rows or b.rows) or cannot_link(a, b) is not None:
                return None
            diff = _differing_tokens(k, k2)
            count = tokens_recent if k in recent and k2 in recent else tokens
            established = diff is not None and all(
                count[t] >= ESTABLISHED_TOKEN_KEYS for t in diff)
            s, why = pair_score(a, b)
            if not anchor_supported(a, b, why) or history_gap(a, b, why):
                return None
            ok = _variant_allowed(variant_kind(k, k2), a, b, s, why,
                                  not key_dates[k].isdisjoint(key_dates[k2]), established)
            return s if ok else None

        options: list[tuple[float, float, int, int, str, str, Cluster, Cluster]] = []
        for neg_sim, k, k2 in sorted(pairs):
            for a in by_key[k]:
                for b in by_key.get(k2, ()):
                    s = check(k, k2, a, b)
                    if s is not None:
                        options.append((neg_sim, -s, a.order, b.order, k, k2, a, b))
        options.sort(key=lambda o: o[:6])
        partners: dict[int, list[tuple[float, Cluster]]] = defaultdict(list)
        for _, neg, _, _, _, _, a, b in options:
            partners[id(a)].append((-neg, b))
            partners[id(b)].append((-neg, a))
        dead: set[int] = set()
        for _, _, _, _, k, k2, a, b in options:
            if id(a) in dead or id(b) in dead:
                continue
            s = check(k, k2, a, b)             # clusters may have grown since
            if s is None:
                continue
            big, small = (a, b) if len(a.rows) >= len(b.rows) else (b, a)
            # the small side (typo / lossy row) with a second, nearly as good target
            # cannot be decided: it stays separate
            if any(c is not big and id(c) not in dead and c.rows
                   and s2 >= s - AMBIGUITY_MARGIN for s2, c in partners[id(small)]):
                continue
            kind = variant_kind(k, k2)
            for i in small.rows:
                small.variants.setdefault(i, kind)
            big.absorb(small)
            partners[id(big)] += partners[id(small)]
            dead.add(id(small))
        return [c for c in clusters if id(c) not in dead]


def _registry(portraits: pd.DataFrame | None, linked: set[int]
              ) -> tuple[dict[int, dict], dict[int, int]]:
    """Portrait registry as ``{portrait_id: person}`` plus ``{duplicate id: kept id}``.

    schlussgang holds duplicate portraits of one person (slug ``-0``): portraits with
    the same name key and the same birthday - or the same birth year and city - are
    one person; the portrait linked to festival rows (else the lowest id) is kept."""
    people: dict[int, dict] = {}
    canon: dict[int, int] = {}
    if portraits is None or not len(portraits):
        return people, canon
    seen: dict[tuple, int] = {}
    recs = sorted(portraits.to_dict("records"),
                  key=lambda p: (int(p["portrait_id"]) not in linked, int(p["portrait_id"])))
    for p in recs:
        p_id = int(p["portrait_id"])
        key = names.name_key(f"{p['last_name']} {p['first_name']}")
        day = str(p.get("birthday") or "")
        birth = _int(day[:4])
        city = _residence_key(p.get("city"))
        club = _str(p.get("club_name"))
        ckey = _club_key(club) if club else None
        tv = _PORTRAIT_TV.get(p.get("association_name"))  # type: ignore[arg-type]
        m = _ESV_ORDINAL_RE.search(f"{p['last_name']} {p['first_name']}")
        sigs = [(key, day)] if day else []
        if birth is not None and city:
            sigs.append((key, birth, city))
        kept = next((seen[sg] for sg in sigs if sg in seen), None)
        if kept is None or (p_id in linked and kept in linked):
            kept = p_id
            people[p_id] = {"key": key, "slug": p["slug"], "birth": birth, "club": club,
                            "clubs": set(), "cities": set(), "tvs": set(),
                            "ordinal": m.group(1) if m else None}
        else:
            canon[p_id] = kept
        for sg in sigs:
            seen.setdefault(sg, kept)
        person = people[kept]
        if ckey:
            person["clubs"].add(ckey)
        if city:
            person["cities"].add(city)
        if tv:
            person["tvs"].add(tv)
    return people, canon


def _merge_same_portrait(clusters: list[Cluster], pid: list[int | None]) -> list[Cluster]:
    """One portrait = one person, also across name keys (a portrait whose rows are
    printed under another spelling than the registry's). A cluster that meets the
    portrait's other rows on a date contradicts the link: its rows stay apart and
    lose the link (``pid`` is updated in place)."""
    seen: dict[int, Cluster] = {}
    out: list[Cluster] = []
    for c in sorted(clusters, key=lambda c: (not c.rows, c.order)):
        p = next(iter(sorted(c.portraits)), c.anchor)
        first = seen.get(p) if p is not None else None
        if first is None:
            if p is not None:
                seen[p] = c
            out.append(c)
        elif first.dates.isdisjoint(c.dates):
            for i in c.rows:
                c.variants.setdefault(i, "portrait")
            first.absorb(c)
        else:
            c.portraits, c.anchor = set(), None    # contradicting link: keep the rows apart
            for i in c.rows:
                pid[i] = None
            out.append(c)
    return sorted(out, key=lambda c: c.order)


def _check_invariants(clusters: list[Cluster], date: list[str], pid: list[int | None],
                      rid: list[str]) -> None:
    """Hard guarantees of the resolver (a violation is a bug, not a data problem): an
    athlete has one row per date (hence per festival) and one portrait; a portrait
    belongs to one athlete."""
    owner: dict[int, int] = {}
    for n, c in enumerate(clusters):
        days = Counter(date[i] for i in c.rows)
        twice = [rid[i] for i in c.rows if days[date[i]] > 1]
        if twice:
            raise ValueError(f"identity with two rows on one date: {twice[:4]}")
        linked = {pid[i] for i in c.rows} - {None}
        if len(linked) > 1:
            raise ValueError(f"identity with several portraits {sorted(linked)}: "
                             f"{rid[c.rows[0]]}")
        for p in linked:
            if owner.setdefault(p, n) != n:
                raise ValueError(f"portrait {p} split over two identities")


def _latest_mode(values: list[str]) -> str | None:
    """Most frequent value; ties -> the latest occurrence (rows are in date order)."""
    if not values:
        return None
    counts = Counter(values)
    best = max(counts.values())
    return next(v for v in reversed(values) if counts[v] == best)


#: Ids were first published for the data from 2011 on. Seasons added before that (Phase
#: 10) must not rename an athlete, so the id row is the earliest row *from this date on*;
#: only an athlete who has no such row takes his earliest row.
ID_ANCHOR_FROM = "2011-01-01"


def _athlete_ids(clusters: list[Cluster], rid: list[str], keys: list[str],
                 registry: dict[int, dict], date: list[str] | None = None) -> list[str]:
    """Stable ids: ``<name-slug>-p<portrait_id>`` when a portrait is attached (slug of
    the registry name), else ``<name-slug>-<athlete_raw_id>`` of the identity's earliest
    row on or after :data:`ID_ANCHOR_FROM`, or of its earliest row if it has none
    (``<fest_id>-<idx>``, slug of that row's name). Later festivals never change
    either part, nor do earlier seasons added afterwards; the display name (majority
    spelling) is not part of the id."""
    out = []
    for c in clusters:
        portrait = next(iter(sorted(c.portraits)), c.anchor)
        first = min(c.rows)
        if date is not None:
            first = min((i for i in c.rows if date[i] >= ID_ANCHOR_FROM), default=first)
        if portrait is not None:
            name = registry[portrait]["key"] if portrait in registry else keys[first]
            out.append(f"{slugify(name)}-p{portrait}")
        else:
            out.append(f"{slugify(keys[first])}-{rid[first]}")
    if len(set(out)) != len(out):  # pragma: no cover - ids are unique by construction
        raise ValueError("athlete id collision")
    return out
