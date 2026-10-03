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
3. **Atoms.** Rows of one portrait are one person (hard). Other rows with the same
   signature (club, residence, birth year, Teilverband, printed ordinal) form an
   atom - unless the signature occurs twice on one date (namesakes with identical
   evidence): those rows stay single.
4. **Agglomeration** inside the block, best pair first (:func:`agglomerate`,
   :func:`pair_score`). Hard cannot-links (:func:`cannot_link`): same festival or
   date, different portraits, birth years >= 2 apart, an implausible age (< 14 /
   > 60) at a festival. The score is a prior for "same name" plus / minus evidence;
   two clusters merge while the best score is > 0. Pairs with an established side go
   first. A cluster that fits two persons of the block but can be neither is taken
   apart and its rows are assigned one by one. Assignments with a nearly as good,
   incompatible alternative are flagged ``ambiguous``.
5. **One portrait, several spellings** are merged (hard), then **spelling variants**:
   clusters of near-identical keys (``?`` wildcard, spacing, swapped order, nickname,
   one edit) merge only under the rules of :func:`_variant_allowed` - never when the
   two spellings appear at the same festival or date (proven distinct names such as
   Stucki Simon / Timon), and not when the small side has two possible targets.
6. **Ids** (:func:`_athlete_ids`): ``<name-slug>-p<portrait_id>`` for anchored
   identities, else ``<name-slug>-<fest_id>-<idx>`` of the identity's earliest row.
   Both parts are facts of the source, so later festivals do not reshuffle ids; an id
   changes only when its earliest row moves to another identity or a portrait gets
   attached / detached.

The statistic sheets have no profile slug (spec §4.1 "slug first"): the portrait link
(2023+) and the portrait registry take that role, then name + club and the other
evidence. Nothing is tuned to individual names.

Every raw row gets ``evidence`` (the strongest evidence it shares with the rest of
its identity) and a ``confidence``; every athlete lists why its name block was split
and which uncertainty flags apply (see :data:`CONFIDENCE`).
"""

from __future__ import annotations

import heapq
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pandas as pd

from src.pipeline import names
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
MIN_SHARED_OPPONENTS = 3
MIN_AGE, MAX_AGE = 14, 60  #: plausible age of an active athlete at a festival
YOUNG_AGE = 16             #: below this age a link needs positive evidence (0.3 % of rows)
W_YOUNG = -2.0
AMBIGUITY_MARGIN = 0.75    #: a cannot-linked alternative this close makes a merge ambiguous
VARIANT_MAX_SPAN_GAP = 2   #: seasons between two spellings of one person
VARIANT_BIG_SIDE = 10      #: both spellings with >= this many rows need club / birth year
ESTABLISHED_TOKEN_KEYS = 3  #: a name token used by >= this many keys is a real name, not a typo
FRAGMENT_ROWS, FRAGMENT_NEXT_TO = 2, 10  #: flag `fragment`: <= 2 rows inside a >= 10-row namesake's career
TV_MIN_SHARE = 0.25        #: a Teilverband counts for a cluster from this share of its rows
STRONG_TV_SOURCES = frozenset({"code", "club", "portrait"})

#: per-row confidence by linking evidence: (name is unique, name has namesakes)
CONFIDENCE: dict[str, tuple[float, float]] = {
    "portrait": (1.0, 1.0), "club": (0.95, 0.95), "birth_year": (0.95, 0.95),
    "residence": (0.9, 0.9), "teilverband": (0.9, 0.7), "name": (0.85, 0.4),
    "not_a_name": (0.0, 0.0),
}
CONFIDENCE_AMBIGUOUS = 0.4   #: cap for rows whose assignment had a close alternative
CONFIDENCE_VARIANT = 0.9     #: factor for rows merged across spellings

_PORTRAIT_TV = {"Innerschweiz": "ISV", "Nordostschweiz": "NOSV", "Bern": "BKSV",
                "Suedwestschweiz": "SWSV", "Südwestschweiz": "SWSV", "Nordwestschweiz": "NWSV"}
_CANTON_RE = re.compile(r"\s+[A-Z]{2}$")
_ESV_ORDINAL_RE = re.compile(r"\((\d)\)")


def _residence_key(value: object) -> str | None:
    """'Oberriet SG' / 'Oberriet' -> 'oberriet' (canton code dropped, folded)."""
    if not isinstance(value, str):
        return None
    k = names.name_key(_CANTON_RE.sub("", value.strip()))
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


def cannot_link(a: Cluster, b: Cluster) -> str | None:
    """Reason why two clusters cannot be one person, or None."""
    pa, pb = a.portraits | ({a.anchor} - {None}), b.portraits | ({b.anchor} - {None})
    if pa and pb:
        if pa != pb:
            return "portrait"
    if not a.dates.isdisjoint(b.dates):
        return "same_date"
    ba, bb = a.birth(), b.birth()
    if ba is not None and bb is not None and abs(ba - bb) >= 2:
        return "birth_year"
    for birth, years in ((ba, b.years), (bb, a.years)):
        if birth is not None and years and (
                min(years) - birth < MIN_AGE or max(years) - birth > MAX_AGE):
            return "age"
    return None


def pair_score(a: Cluster, b: Cluster) -> tuple[float, list[str]]:
    """Evidence score for "a and b are one person" (callers check :func:`cannot_link`).

    Returns ``(score, reasons)``: reasons name the positive evidence (``club``,
    ``birth_year``, ``residence``, ``teilverband``, ``ordinal``, ``opponents``) and,
    prefixed with ``-``, the evidence against."""
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
    elif a.tvs and b.tvs:
        s += W_TV_DISJOINT
        why.append("-teilverband")
    if a.ordinals & b.ordinals:
        s += W_ORDINAL
        why.append("ordinal")
    elif a.ordinals and b.ordinals:
        s += W_ORDINAL_DISJOINT
        why.append("-ordinal")
    if a.opponents and b.opponents:
        shared = len(a.opponents & b.opponents)
        share = shared / min(len(a.opponents), len(b.opponents))
        if shared >= MIN_SHARED_OPPONENTS and share >= 0.3:
            s += W_OPPONENTS
            why.append("opponents")
        s += 0.3 * share                       # tie-break only
    for birth, years in ((ba, b.years), (bb, a.years)):
        if birth is not None and years and min(years) - birth < YOUNG_AGE and ba != bb:
            s += W_YOUNG
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


def agglomerate(clusters: list[Cluster]) -> list[Cluster]:
    """Greedy best-pair-first merging; deterministic (ties by cluster order).

    Pairs with a core side (see :func:`_is_core`) go first: rows without identifying
    evidence are attached to the established persons of the block before they are
    merged with each other - otherwise two such rows of different namesakes can
    form a cluster that fits none of them."""
    live: dict[int, Cluster] = dict(enumerate(clusters))
    version = {i: 0 for i in live}
    heap: list[tuple[int, float, int, int, int, int, int, int]] = []

    def push(i: int, j: int) -> None:
        a, b = live[i], live[j]
        if cannot_link(a, b) is None:
            s, _ = pair_score(a, b)
            if s > 0:
                lo, hi = sorted((a.order, b.order))
                tier = 0 if _is_core(a) or _is_core(b) else 1
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
        # a merge is ambiguous when one side had a nearly as good, incompatible alternative
        for x, y in ((a, b), (b, a)):
            for k, c in live.items():
                if c is x or c is y or cannot_link(x, c) is not None:
                    continue
                if cannot_link(y, c) is not None and pair_score(x, c)[0] >= -neg - AMBIGUITY_MARGIN:
                    small = x if len(x.rows) <= len(y.rows) else y
                    small.ambiguous.update(small.rows)
                    break
        a.absorb(b)
        del live[j]
        version[i] += 1
        for k in sorted(live):
            if k != i:
                push(*sorted((i, k)))
    return sorted(live.values(), key=lambda c: c.order)


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
        esv_clubs: set[str] | None = None
        if portraits is not None and len(portraits):
            esv_clubs = set(portraits["club_name"].dropna())

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
        club = [_str(v) if u else None for v, u in zip(raw["club"], usable)]
        tv_src = raw["sub_assoc_source"].tolist()
        tv = [_str(v) if u and s in STRONG_TV_SOURCES else None
              for v, u, s in zip(raw["sub_association"], usable, tv_src)]
        res = [_residence_key(v) if u else None for v, u in zip(raw["residence"], usable)]
        birth: list[int | None] = []
        for v, c, y, u in zip(raw["birth_year"], cleaned, year, usable):
            b = _int(v) if _int(v) is not None else c[1].birth_year
            birth.append(b if u and b is not None and MIN_AGE <= y - b <= MAX_AGE else None)
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
            c = Cluster(order=i, key=keys[i], rows=[i], dates={date[i]}, years={year[i]})
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
            linked: set[int] = set()
            sigs: dict[int, tuple] = {}
            seen: dict[tuple, set[str]] = defaultdict(set)
            shared: set[tuple] = set()         # signatures used by two people at once
            for i in blocks[k]:
                if pid[i] is not None:
                    sigs[i] = ("p", pid[i])
                    linked.add(pid[i])
                elif i not in explode and (club[i] or res[i] or birth[i] is not None):
                    sigs[i] = sg = ("s", club[i], res[i], birth[i], tv[i], ordinal[i])
                    if date[i] in seen[sg]:
                        shared.add(sg)
                    seen[sg].add(date[i])
                else:
                    sigs[i] = ("r", i)
            for i in blocks[k]:
                c = row_cluster(i)
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

        clusters: list[Cluster] = []
        splits: dict[str, Counter[str]] = {}
        n_exploded = 0
        for k in sorted(blocks):
            done = agglomerate(build_block(k))
            # a cluster that fits two persons of the block but can be neither mixes their
            # rows (namesakes sharing club / village): assign its rows one by one instead
            torn = frozenset(
                i for u in done if len(u.rows) > 1 and not u.portraits and u.anchor is None
                and sum(1 for v in done if v is not u and cannot_link(u, v) is not None
                        and pair_score(u, v)[0] > PRIOR_SAME_NAME) >= 2
                for i in u.rows)
            if torn:
                n_exploded += len(torn)
                done = agglomerate(build_block(k, torn))
            real = [c for c in done if c.rows]
            if len(real) > 1:
                splits[k] = Counter()
                for x, a in enumerate(real):
                    for b in real[x + 1:]:
                        why = cannot_link(a, b) or ",".join(
                            w for w in pair_score(a, b)[1] if w.startswith("-")) or "score"
                        splits[k][why] += 1
            clusters += done

        clusters = _merge_same_portrait(clusters)
        clusters = self._merge_variants(clusters, blocks, date)

        # ---- output
        real = [c for c in clusters if c.rows]
        for c in real:
            c.key = min(c.keys, key=lambda k: (-c.keys[k], k))
        per_key = Counter(c.key for c in real)
        ids = _athlete_ids(real, rid, keys, registry)
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
                    club_n[str(info["club"])] += 1
            cbirth = c.birth()
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
            seasons = sorted(c.years)
            gap = max((b - a - 1 for a, b in zip(seasons, seasons[1:])), default=0)
            if gap >= GAP_SEASONS:
                notes.append(f"career_gap={gap}")
            if multi and len(c.rows) <= FRAGMENT_ROWS and c.key in long_careers and any(
                    lo - 1 <= min(c.years) and max(c.years) <= hi + 1
                    for lo, hi in long_careers[c.key]):
                notes.append("fragment")
            spell = Counter(display[i] for i in c.rows if keys[i] == c.key)
            full = min(spell, key=lambda s: (-spell[s], -sum(ord(ch) > 127 for ch in s), s))
            last_club = [club[i] for i in c.rows if club[i]]
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
        tokens: Counter[str] = Counter()
        for k in blocks:
            tokens.update(set(k.split()))
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
            established = diff is not None and all(
                tokens[t] >= ESTABLISHED_TOKEN_KEYS for t in diff)
            s, why = pair_score(a, b)
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
        if club:
            person["clubs"].add(club)
        if city:
            person["cities"].add(city)
        if tv:
            person["tvs"].add(tv)
    return people, canon


def _merge_same_portrait(clusters: list[Cluster]) -> list[Cluster]:
    """One portrait = one person, also across name keys (a portrait whose rows are
    printed under another spelling than the registry's)."""
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
            out.append(c)
    return sorted(out, key=lambda c: c.order)


def _latest_mode(values: list[str]) -> str | None:
    """Most frequent value; ties -> the latest occurrence (rows are in date order)."""
    if not values:
        return None
    counts = Counter(values)
    best = max(counts.values())
    return next(v for v in reversed(values) if counts[v] == best)


def _athlete_ids(clusters: list[Cluster], rid: list[str], keys: list[str],
                 registry: dict[int, dict]) -> list[str]:
    """Stable ids: ``<name-slug>-p<portrait_id>`` when a portrait is attached (slug of
    the registry name), else ``<name-slug>-<athlete_raw_id>`` of the identity's earliest
    row (``<fest_id>-<idx>``, slug of that row's name). Later festivals never change
    either part; the display name (majority spelling) is not part of the id."""
    out = []
    for c in clusters:
        portrait = next(iter(sorted(c.portraits)), c.anchor)
        first = min(c.rows)
        if portrait is not None:
            name = registry[portrait]["key"] if portrait in registry else keys[first]
            out.append(f"{slugify(name)}-p{portrait}")
        else:
            out.append(f"{slugify(keys[first])}-{rid[first]}")
    if len(set(out)) != len(out):  # pragma: no cover - ids are unique by construction
        raise ValueError("athlete id collision")
    return out
