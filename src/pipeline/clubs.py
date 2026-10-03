"""Schwingklub normalisation and club → sub-association (Teilverband) mapping.

Pure functions plus a small registry; no database or network access.

* :func:`club_key` maps a printed club name to a canonical comparison key:
  case, umlauts/accents, punctuation, the generic words of club names
  ("Schwingklub", "SK", "Club des lutteurs", "u. Umgebung", "& Environs" …) and a
  leading cantonal code ("VD Aigle") are removed, so "Schwingklub Rapperswil und
  Umgebung", "Rapperswil u. Umgebung" and "SK Rapperswil" share the key
  ``rapperswil``. Bilingual names ("Kerzers/Chiètres") get their parts as aliases.
* :data:`CODE_TO_SUB` / :func:`sub_association_for_code` map the cantonal / Gau
  codes printed in the ranking lists ("LU", "ONW", "BO", "NOS" …) to the five
  Teilverbände BKSV, ISV, NOSV, NWSV, SWSV.
* :func:`sub_association_for_festival` derives the organising Teilverband of a
  festival from schlussgang's association term or, for Kranzfeste, the name
  (cantonal names as in ``src/scraper/reference/schwingfeste_schweiz.json``).
* :class:`ClubRegistry` collects observations (club as printed + any
  sub-association evidence) and builds canonical clubs: display name = most
  frequent spelling, sub-association = weighted vote (code and portrait evidence
  weigh 3, the festival's own Teilverband 1). Short abbreviations ("Sol", "OTT")
  resolve to a canonical club only when the prefix is unique within the
  sub-association; otherwise they stay unresolved (never guessed).
  With ``min_obs`` > 1 only *established* names become canonical clubs (an ESV
  club id from the portraits, or at least ``min_obs`` observations and as many
  Teilverband votes); the long tail of parsing debris ("Villars-le-Terroir
  Lausanne" = residence glued to the club) resolves to an established club only
  when it ends with exactly one club's name, else to nothing.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

SUB_ASSOCIATIONS = ("BKSV", "ISV", "NOSV", "NWSV", "SWSV")

# Cantonal associations / Bernese Gaue / Teilverband abbreviations -> Teilverband.
CODE_TO_SUB: dict[str, str] = {
    # BKSV: Gauverbände Oberland, Emmental, Mittelland, Oberaargau, Seeland, Bern-Jura
    "BO": "BKSV", "ET": "BKSV", "EM": "BKSV", "ML": "BKSV", "OA": "BKSV", "SL": "BKSV",
    "JB": "BKSV",
    "BE": "BKSV", "BKSV": "BKSV",
    # ISV: Luzern, Ob-/Nidwalden, Schwyz, Uri, Zug, Tessin
    "LU": "ISV", "ONW": "ISV", "ONSV": "ISV", "OW/NW": "ISV", "OW": "ISV", "NW": "ISV",
    "SZ": "ISV", "UR": "ISV", "ZG": "ISV", "TI": "ISV", "ISV": "ISV",
    # NOSV: Appenzell, Glarus, Graubünden, St. Gallen, Schaffhausen, Thurgau, Zürich
    "AP": "NOSV", "AR": "NOSV", "AI": "NOSV", "GL": "NOSV", "GR": "NOSV", "SG": "NOSV",
    "SH": "NOSV", "TG": "NOSV", "ZH": "NOSV", "NOS": "NOSV", "NOSV": "NOSV",
    # NWSV: Aargau, Basel-Land, Basel-Stadt, Solothurn
    "AG": "NWSV", "BL": "NWSV", "BS": "NWSV", "SO": "NWSV", "NWS": "NWSV", "NWSV": "NWSV",
    # SWSV (Association romande de lutte suisse): FR, GE, NE, VD, VS, JU
    "FR": "SWSV", "GE": "SWSV", "NE": "SWSV", "VD": "SWSV", "VS": "SWSV", "JU": "SWSV",
    "SWS": "SWSV", "SWSV": "SWSV", "ARLS": "SWSV", "SWSV/FR": "SWSV",
}
# Codes that are *not* an association: guests ('GA' in Bernese lists, 'GST').
GUEST_CODES = frozenset({"GA", "GST"})
# Codes also printed *after* a club or residence ("La Gruyère ARLS", "Langnau EM");
# cantonal codes are excluded here because they are part of place names ("Ollon VD").
_TRAILING_CODES = frozenset({"ARLS", "SWS", "SWSV", "ISV", "NOS", "NOSV", "NWS", "NWSV", "BKSV",
                             "EM", "GST"})

# schlussgang association terms (festivals.association, portraits.association_name)
# and the ESV long names.
_ASSOC_NAME_TO_SUB: dict[str, str] = {
    "innerschweiz": "ISV", "innerschweizer schwingerverband": "ISV",
    "bern": "BKSV", "bernisch kantonaler schwingerverband": "BKSV",
    "nordostschweiz": "NOSV", "nordostschweizerischer schwingerverband": "NOSV",
    "nordostschweizer schwingerverband": "NOSV",
    "nordwestschweiz": "NWSV", "nordwestschweizerischer schwingerverband": "NWSV",
    "sudwestschweiz": "SWSV", "suedwestschweiz": "SWSV",
    "association romande de lutte suisse": "SWSV", "sudwestschweizer schwingerverband": "SWSV",
}

# Festival name keywords -> organising Teilverband (Kranzfeste). Order matters:
# "Bern-Jurassisch" before "Jura", "Nordwest/Nordost" before generic words.
_FESTIVAL_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"bern-jura|bernisch|berner|emmental|oberl[äa]nd|seel[äa]nd|mittell[äa]nd|oberaargau", "BKSV"),
    (r"innerschweiz|luzern|schwyz|\burner\b|\buri\b|zuger|\bzug\b|nidwald|obwald|tessin|ticin|rigi|stoos",
     "ISV"),
    (r"nordost|z[üu]rch|st\.? ?gall|thurgau|glarn|b[üu]ndn|appenzell|schaffhaus|schw[äa]galp",
     "NOSV"),
    (r"nordwest|aargau|solothurn|basel|baselbiet|weissenstein", "NWSV"),
    (r"s[üu]dwest|romand|freiburg|fribourg|waadt|vaud|wallis|valais|neuenburg|neuch[âa]tel"
     r"|genf|gen[èe]v|jura|schwarzsee", "SWSV"),
)
_FESTIVAL_RES = tuple((re.compile(rx, re.I), sub) for rx, sub in _FESTIVAL_KEYWORDS)
_KRANZ_CATEGORIES = frozenset({"Teilverband", "Kantonal", "Gauverband", "Bergkranz"})

# Words that carry no club identity.
_PREFIX_WORDS = frozenset({
    "schwingklub", "schwingerklub", "schwingclub", "schwingklubs", "sk", "skl",
    "schwingerverband", "schwingverband", "schwingerverein", "schwingverein", "schwingersektion",
    "sektion", "club", "lutteurs", "lutte", "cl", "societe", "verein", "des", "de", "du",
    "la", "le", "les", "d", "l", "am", "an", "der", "im", "a", "bezirk",
})
# Kranz-status letters and remarks that end up in the club column of some lists.
_NOT_CLUBS = frozenset({"k", "kk", "ek", "tk", "nk", "c", "n i", "gast", "gast ga"})  # "n i" = N.I.A
_CONNECTOR_WORDS = frozenset({"u", "und", "et", "umgebung", "umg", "environs", "env",
                              "region", "regio", "ug"})
# Other names of one club (old name, name of the club's own "Verband", language):
# key as printed -> key of the club. Only pairs that are the same organisation.
_KEY_ALIASES: dict[str, str] = {
    "zurzach": "zurzibiet",            # Schwingklub Zurzach = today's SK Zurzibiet
    "mythenverband": "mythen",         # Mythenverband = Schwingklub am Mythen
    "schangnau siehen": "siehen",      # Schwingklub Siehen (Schangnau / Eggiwil)
    "weite wartau": "wartau",
    "ticino": "tessin",
    "basel": "basel stadt",
}


def _fold(s: str) -> str:
    s = s.casefold().replace("ß", "ss")
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"(?<=[a-z])([aou])e", r"\1", s)  # Muemliswil = Mümliswil


def strip_code(raw: str) -> tuple[str | None, str]:
    """('VD', 'Aigle') for 'VD Aigle', ('ARLS', 'La Gruyère') for 'La Gruyère ARLS';
    trailing 2-digit birth years ('OTT 98') dropped."""
    s = re.sub(r"\s+\d{2}$", "", raw.strip())
    parts = s.split(" ", 1)
    if len(parts) == 2 and (parts[0] in CODE_TO_SUB or parts[0] in GUEST_CODES):
        return parts[0], parts[1]
    head, _, last = s.rpartition(" ")
    if head and last.strip("()") in _TRAILING_CODES:
        return last.strip("()"), head
    return None, s


def club_key(raw: str | None) -> str | None:
    """Canonical comparison key of a club name (None if nothing club-like is left)."""
    if not raw or not raw.strip() or code_in_club(raw) or " / " in raw:
        return None  # ' / ': two cells merged by the PDF, not a bilingual name
    _, s = strip_code(raw)
    s = _fold(s)
    s = s.replace("'", " ")
    s = re.sub(r"[&+/,.()\-:;]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    tokens = [t for t in s.split() if t not in _PREFIX_WORDS and t not in _CONNECTOR_WORDS]
    tokens = [t for t in tokens if not t.isdigit()]
    key = " ".join(tokens)
    key = _KEY_ALIASES.get(key, key)
    return key if len(re.sub(r"[^a-z]", "", key)) >= 2 and key not in _NOT_CLUBS else None


def is_region_code(name: str | None) -> bool:
    """'TO', 'RO', 'RA', 'ST': the NOSV lists print the regional association in the
    club column. Such a label (an upper-case abbreviation of <= 3 letters) is evidence
    for the Teilverband and for telling namesakes apart, but it is not a Schwingklub."""
    return bool(name) and len(name) <= 3 and name.isalpha() and name.isupper()  # type: ignore[arg-type]


def code_in_club(raw: str | None) -> str | None:
    """A cantonal / Teilverband code printed in the club column ('(SWS)', 'ARLS')."""
    if not raw:
        return None
    s = raw.strip().strip("()").strip().upper()
    if s in CODE_TO_SUB and (raw.strip().startswith("(") or len(s) >= 3):
        return s
    return None


def club_aliases(raw: str) -> list[str]:
    """Extra keys of a bilingual name: 'Kerzers/Chiètres' -> ['kerzers', 'chietres']."""
    _, s = strip_code(raw)
    if "/" not in s:
        return []
    return [k for k in (club_key(p) for p in s.split("/")) if k]


def sub_association_for_code(code: str | None) -> str | None:
    if not code:
        return None
    c = code.strip().upper()
    if c in CODE_TO_SUB:
        return CODE_TO_SUB[c]
    first = c.split()[0] if c.split() else c
    return CODE_TO_SUB.get(first)


def sub_association_for_name(name: str | None) -> str | None:
    """Teilverband of a schlussgang association term or an ESV association name."""
    if not name:
        return None
    key = re.sub(r"[^a-z ]", " ", _fold(name)).split()
    return _ASSOC_NAME_TO_SUB.get(" ".join(key))


def sub_association_for_festival(name: str, category: str | None,
                                 association: str | None = None) -> str | None:
    """Organising Teilverband of a festival: schlussgang's association term first,
    else (Kranzfeste only) the cantonal / regional keyword in the name. ESAF-tier
    festivals and the Brünig (ISV + BKSV) have none."""
    sub = sub_association_for_name(association)
    if sub:
        return sub
    if category not in _KRANZ_CATEGORIES or re.search(r"br[üu]nig", name, re.I):
        return None
    for rx, s in _FESTIVAL_RES:
        if rx.search(name):
            return s
    return None


# --------------------------------------------------------------------------- registry
@dataclass
class Club:
    key: str
    name: str                       # display name (most frequent spelling)
    sub_association: str | None
    n: int                          # observations
    votes: dict[str, float] = field(default_factory=dict)
    conflict: bool = False          # runner-up sub-association has ≥ 25 % of the votes
    aliases: list[str] = field(default_factory=list)
    esv_id: int | None = None


@dataclass
class _Obs:
    spellings: Counter[str] = field(default_factory=Counter)
    votes: Counter[str] = field(default_factory=Counter)
    n: int = 0
    esv_ids: Counter[int] = field(default_factory=Counter)
    esv_spellings: Counter[str] = field(default_factory=Counter)  # as named by the ESV term


def _top(o: _Obs) -> str | None:
    return o.votes.most_common(1)[0][0] if o.votes else None


def _is_short(key: str) -> bool:
    """Keys that may be abbreviations ('sol', 'mum', 'ott'): one token, ≤ 3 letters."""
    return " " not in key and len(key) <= 3


class ClubRegistry:
    """Collects club observations and resolves raw club names to canonical clubs."""

    CODE_WEIGHT = 3.0
    PORTRAIT_WEIGHT = 3.0
    FESTIVAL_WEIGHT = 1.0

    def __init__(self, min_obs: int = 1) -> None:
        self.min_obs = min_obs
        self._obs: dict[str, _Obs] = defaultdict(_Obs)
        self._alias_of: dict[str, set[str]] = defaultdict(set)
        self._folded: dict[str, str] = {}     # abbreviation key -> canonical key
        self.debris: Counter[str] = Counter()  # keys that are not established clubs (after build)
        self._clubs: dict[str, Club] | None = None

    def add(self, raw: str | None, *, code: str | None = None, festival_sub: str | None = None,
            portrait_sub: str | None = None, esv_id: int | None = None, count: int = 1) -> None:
        key = club_key(raw)
        if key is None or raw is None:
            return
        self._clubs = None
        _, shown = strip_code(raw)
        o = self._obs[key]
        o.n += count
        o.spellings[shown.strip()] += count
        sub = sub_association_for_code(code)
        if sub:
            o.votes[sub] += self.CODE_WEIGHT * count
        if portrait_sub:
            o.votes[portrait_sub] += self.PORTRAIT_WEIGHT * count
        if festival_sub:
            o.votes[festival_sub] += self.FESTIVAL_WEIGHT * count
        if esv_id is not None:
            o.esv_ids[esv_id] += count
            o.esv_spellings[shown.strip()] += count
        for alias in club_aliases(raw):
            self._alias_of[alias].add(key)

    def build(self) -> dict[str, Club]:
        if self._clubs is not None:
            return self._clubs
        merged: dict[str, _Obs] = {}
        # 1. a plain key that is one half of a bilingual club ('kerzers' of
        #    'Kerzers/Chiètres') joins it if that club is unique, at least half as
        #    frequent and from the same Teilverband (guards against merged cells
        #    like 'Fribourg/Rothenburg')
        self._folded = {}
        for key, o in self._obs.items():
            target = key
            parents = [t for t in self._alias_of.get(key, ()) if t in self._obs and t != key]
            if len(parents) == 1:
                par = self._obs[parents[0]]
                if par.n * 2 >= o.n and _top(par) in (None, _top(o)) or _top(o) is None \
                        and par.n * 2 >= o.n:
                    target = parents[0]
                    self._folded[key] = target
            self._merge(merged.setdefault(target, _Obs()), o)
        # 2. short keys ('sol') fold into the unique longer club they abbreviate
        #    ('solothurn') when its Teilverband is compatible and it is clearly the
        #    established name (≥ 3x the observations); otherwise they stay as they are
        for key in [k for k in merged if _is_short(k)]:
            short = merged[key]
            sub = short.votes.most_common(1)[0][0] if short.votes else None
            targets = [k for k, o in merged.items() if k != key and k.replace(" ", "").startswith(key)
                       and (sub is None or not o.votes or o.votes.most_common(1)[0][0] == sub)]
            if len(targets) == 1 and merged[targets[0]].n >= 3 * short.n:
                self._merge(merged[targets[0]], short)
                self._folded[key] = targets[0]
                del merged[key]
        # 3. a name without ESV id that is the beginning of exactly one ESV club of the
        #    same Teilverband is that club, truncated by a narrow column
        #    ('Mümliswil-Rami', 'Dorneck-Thierst')
        for key in [k for k, o in merged.items() if not o.esv_ids and len(k) >= 6]:
            sub = _top(merged[key])
            targets = [k for k, o in merged.items() if o.esv_ids and k != key
                       and k.startswith(key) and sub in (None, _top(o))]
            if len(targets) == 1:
                self._merge(merged[targets[0]], merged.pop(key))
                self._folded[key] = targets[0]
        # 4. only established names are clubs (see module docstring)
        self._clubs = {key: self._club(key, o) for key, o in merged.items()
                       if o.esv_ids or self.min_obs <= 1
                       or (o.n >= self.min_obs and sum(o.votes.values()) >= self.min_obs)}
        self.debris = Counter({key: o.n for key, o in merged.items() if key not in self._clubs})
        return self._clubs

    def _by_suffix(self, key: str) -> Club | None:
        """The established club whose name ends ``key`` ('villars terroir lausanne'
        -> 'lausanne': residence glued to the club); the longest match wins."""
        assert self._clubs is not None
        tokens = key.split()
        for k in range(1, len(tokens)):
            club = self._clubs.get(" ".join(tokens[k:]))
            if club is not None:
                return club
        return None

    @staticmethod
    def _merge(into: _Obs, o: _Obs) -> None:
        into.n += o.n
        into.spellings.update(o.spellings)
        into.votes.update(o.votes)
        into.esv_ids.update(o.esv_ids)
        into.esv_spellings.update(o.esv_spellings)

    @staticmethod
    def _club(key: str, o: _Obs) -> Club:
        top = o.votes.most_common(2)
        sub = top[0][0] if top else None
        total = sum(o.votes.values())
        conflict = len(top) > 1 and top[1][1] >= 0.25 * total
        # the ESV's own name if the club has one (an alias such as 'Zurzach' may be
        # printed more often), else the most frequent spelling; ties -> the one with
        # diacritics, then longest
        name = max((o.esv_spellings or o.spellings).items(),
                   key=lambda t: (t[1], t[0] != _fold(t[0]), len(t[0]), t[0]))[0]
        esv = o.esv_ids.most_common(1)[0][0] if o.esv_ids else None
        return Club(key, name, sub, o.n, dict(o.votes), conflict, [], esv)

    def resolve(self, raw: str | None, *, code: str | None = None,
                festival_sub: str | None = None) -> Club | None:
        """Canonical club of a printed club name, or None (unknown / ambiguous)."""
        key = club_key(raw)
        if key is None:
            return None
        clubs = self.build()
        key = self._folded.get(key, key)
        if key in clubs:
            return clubs[key]
        parents = [t for t in self._alias_of.get(key, ()) if t in clubs]
        if len(parents) == 1:
            return clubs[parents[0]]
        if self.min_obs > 1:
            club = self._by_suffix(key)
            if club is not None:
                return club
        if _is_short(key):  # unseen abbreviation: unique prefix within the Teilverband
            sub = sub_association_for_code(code) or festival_sub
            cands = [c for k, c in clubs.items() if k.replace(" ", "").startswith(key)
                     and (sub is None or c.sub_association == sub)]
            if len(cands) == 1:
                return cands[0]
        return None

    def short_clubs(self) -> list[Club]:
        """Canonical clubs with an abbreviation-like key that did not fold (review list)."""
        return sorted((c for k, c in self.build().items() if _is_short(k)), key=lambda c: -c.n)


def pseudo_club_sub(raw: str | None) -> str | None:
    """Teilverband when the 'club' column holds an association code ('ARLS', '(SWS)')."""
    return sub_association_for_code(code_in_club(raw))


def vote_summary(clubs: Iterable[Club]) -> Counter[str]:
    return Counter(c.sub_association or "unknown" for c in clubs)
