"""Name cleaning and matching helpers for identity resolution (Phase 3).

Pure functions only: no database or network access. Three layers:

* :func:`clean_raw_name` turns a printed sheet name (``athletes_raw.name_raw`` or an
  opponent string) into a clean display name plus the extras glued to it (birth
  year, within-sheet suffix, Kranz stars, S/T marker, association, club, place ...).
* :func:`name_key` maps a clean name to a canonical comparison key: case, accents,
  umlauts (``ä``/``ae``/``a`` all fold to ``a``), hyphens and apostrophes are unified.
* :class:`NameIndex` / :func:`fuzzy_candidates` / :func:`name_similarity` find
  near-identical keys (typos, missing letters, swapped name order, ``?`` glyphs from
  lossily decoded sheets) for blocking. They only *propose* candidates; the resolver
  must confirm a merge with evidence (club, region, career span, opponents).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable

__all__ = [
    "NameExtras", "clean_raw_name", "name_key", "key_tokens", "compact_key",
    "has_wildcard", "wildcard_match", "edit_distance", "name_similarity",
    "NameIndex", "fuzzy_candidates", "FIRST_NAME_ALIASES", "first_name_canonical",
]


@dataclass
class NameExtras:
    """Everything that was printed around the name, plus quality flags."""

    birth_year: int | None = None
    suffix: int | None = None          # " 1" / " 2": disambiguates within ONE sheet only
    stars: int = 0                     # Kranz stars (never use for identity)
    status: str | None = None          # E / K / EK / TK / N / F (Kranz / Neukranzer letters)
    ordinal: str | None = None         # roman "I"/"II": kept across sheets (two namesakes)
    st_marker: str | None = None       # 'S' Sennenschwinger / 'T' Turnerschwinger
    association: str | None = None     # "(SWS)", "(BE)", "(NOS)" ... (may be truncated)
    code: str | None = None            # trailing Gau/club code "OB", "ET", "NWS"
    club: str | None = None            # "SK Siehen" -> "Siehen"
    place: str | None = None           # "(Bonaduz)" / ", Oberarth"
    generation: str | None = None      # 'jun' / 'sen'
    number: int | None = None          # unexplained trailing/leading number
    flags: list[str] = field(default_factory=list)
    # flags: 'glued_bout_line' (points / bout entries glued to the name -> rescued),
    # 'title_prefix' (sheet title glued in front), 'rank_prefix', 'truncated',
    # 'glued_words' (camel case split), 'surname_only', 'not_a_name', 'wildcard',
    # 'mojibake' (UTF-8 read as Latin-1, repaired), 'implausible_birth_year' (age
    # outside 12-60 at fest_year: youth rows leaking into active sheets)


_WS_RE = re.compile(r"\s+")
_POINTS_RE = re.compile(r"\s\d{1,2}[.,]\d{2}\b")
_PAREN_RE = re.compile(r"\s*\(([^()]*)\)")
_OPEN_PAREN_RE = re.compile(r"\s*\(([^()]*)$")
_YEAR_RE = re.compile(r"(?:19|20)?\d{2}")
_ASSOC_RE = re.compile(r"[A-Z]{1,4}")
_CLUB_RE = re.compile(r"\s+(?:SK|SC|Schwingklub|Schwingclub)\s+(.+)$", re.I)
_GEN_RE = re.compile(r"\s*\b(jun|jr|sen|sr)(?:\.|\b)|\s*\b(Jun|Jr|Sen|Sr)\.")
# trailing tokens: stars, status letters, S/T marker glued to stars ("S**")
_TAIL_TOKEN_RE = re.compile(r"\s+([ST]?\*+|\*+|EK|TK|E|K|N|S|T)$|(?<=\*)\s*(F)$")
_ROMAN_RE = re.compile(r"\s+(I{1,3})$")
_CODE_RE = re.compile(r"(?<=[a-zà-ÿ?])\s+([A-Z]{2,3})$")
_TRAIL_NUM_RE = re.compile(r"\s+(\d{1,4})$")
_CAMEL_RE = re.compile(r"(?<=[a-zß-ÿ])(?=[A-ZÀ-Þ])")
_LETTER_RE = re.compile(r"[^\W\d_]")
# title glued in front: "... Roggwil 1 Forrer Arnold" -> after the last rank number
_TITLE_RANK_RE = re.compile(r"^(?:\S+\s+){2,}\d{1,3}[a-z]?\s+(?=[A-ZÄÖÜ][^\s\d]+\s+[A-ZÄÖÜ])")
_LEAD_RANK_RE = re.compile(r"^(?:[a-z]\s+)?(?:\d{1,3}\s+)?(?=[A-ZÄÖÜÉÈ])")


def _year_from(s: str, fest_year: int | None, strict: bool = True) -> int | None:
    """'2004' / '04' / '94' -> birth year. ``strict``: None unless the age at
    ``fest_year`` is plausible for an active athlete (12-60); used for bare numbers."""
    if not _YEAR_RE.fullmatch(s):
        return None
    y = int(s)
    if y < 100:
        ref = (fest_year or 2026) % 100
        y += 2000 if y <= ref else 1900
    if strict and fest_year is not None and not (fest_year - 60 <= y <= fest_year - 12):
        return None
    if not 1940 <= y <= 2030:
        return None
    return y


# French sheets: B = berger (Sennenschwinger), G = gymnaste (Turnerschwinger)
_ST_FRENCH = {"B": "S", "G": "T"}
_MOJIBAKE_RE = re.compile(r"[\u00c3\u00c2](?:1\u20444|1\u20442|3\u20444|[\u0080-\u00bf\u0152-\u2122])")
_FRACTIONS = {"1\u20444": "\u00bc", "1\u20442": "\u00bd", "3\u20444": "\u00be"}


def _fix_mojibake(s: str) -> str:
    """'ZÃ¼rcher' / 'ZÃ1⁄4rcher' (UTF-8 read as Latin-1, then NFKC) -> 'Zürcher'."""
    def repl(m: re.Match[str]) -> str:
        frag = m.group(0)
        frag = frag[0] + _FRACTIONS.get(frag[1:], frag[1:])
        try:
            return frag.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return m.group(0)
    return _MOJIBAKE_RE.sub(repl, s)


def _paren(inner: str, ex: NameExtras, fest_year: int | None) -> None:
    inner = inner.strip()
    if not inner:
        return
    y = _year_from(inner, fest_year, strict=False)
    if y is not None:
        ex.birth_year = y
    elif inner in ("1", "2", "3"):
        ex.suffix = int(inner)
    elif inner in ("E", "K", "EK", "TK", "N"):
        ex.status = inner
    elif _ASSOC_RE.fullmatch(inner):
        ex.association = inner
    elif ex.place is None:
        ex.place = inner


def _smart_case(token: str) -> str:
    """'NYDEGGER' -> 'Nydegger', 'AMEZ-DROZ' -> 'Amez-Droz'; leaves mixed case alone."""
    if len(token) > 1 and token.isupper() and _LETTER_RE.search(token):
        return "-".join(p.capitalize() for p in token.split("-"))
    return token


def clean_raw_name(raw: str, fest_year: int | None = None) -> tuple[str, NameExtras]:
    """Split a printed name into ``(clean_name, extras)``.

    ``clean_name`` keeps the printed order ("Last First"), original casing (all-caps
    surnames are title-cased), umlauts, accents, hyphens and ``?`` glyphs. It drops
    stars, status letters, the S/T marker, birth year, suffix, association, club,
    place and ``jun./sen.``; those go into ``extras``. Idempotent on clean names.
    ``fest_year`` (optional) makes two-digit birth years unambiguous / plausible.
    """
    ex = NameExtras()
    s = _fix_mojibake(raw)
    if s != raw:
        ex.flags.append("mojibake")
    s = unicodedata.normalize("NFKC", s).replace("\u00a0", " ").replace("\u2019", "'")
    s = re.sub(r"[\ue000-\uf8ff]", " ", s)  # private-use glyphs ('\uf04e')
    s = re.sub(r"(?<=\s)[+°]+(?=\s|$)", " ", s)  # stray bout symbol / withdrawn mark
    s = re.sub(r"^[a-z](?=[A-ZÄÖÜ][a-zäöü])", "", s)  # glued mark: 'mNietlispach'
    s = _WS_RE.sub(" ", s).strip()
    if "?" in s:
        ex.flags.append("wildcard")

    # bout entries / points glued to the name: keep the part before the points
    m = _POINTS_RE.search(s)
    if m:
        s = s[:m.start()].strip()
        ex.flags.append("glued_bout_line")
    m = _TITLE_RANK_RE.match(s)
    if m and len(s.split()) >= 5:
        s = s[m.end():]
        ex.flags.append("title_prefix")
    m = _LEAD_RANK_RE.match(s)
    if m and m.end() > 0:
        lead = s[:m.end()].split()
        if lead and lead[-1].isdigit():
            ex.number = int(lead[-1])
        s = s[m.end():]
        ex.flags.append("rank_prefix")

    # parentheses: "(2004)", "(SWS)", "(Bonaduz)"; truncated "(SW" at the end
    for inner in _PAREN_RE.findall(s):
        _paren(inner, ex, fest_year)
    s = _PAREN_RE.sub(" ", s)
    m = _OPEN_PAREN_RE.search(s)
    if m:
        _paren(m.group(1), ex, fest_year)
        s = s[:m.start()]
        ex.flags.append("truncated")

    # ", S" / ", T*" marker and ", Place"
    if "," in s:
        head, _, tail = s.partition(",")
        s = head
        for part in (p.strip() for p in tail.split(",")):
            if "*" in part:
                ex.stars = max(ex.stars, max(len(x) for x in re.findall(r"\*+", part)))
                part = _WS_RE.sub(" ", part.replace("*", " ")).strip()
            mm = re.match(r"^([STBG])\b\s*(.*)$", part)
            if mm:
                ex.st_marker = _ST_FRENCH.get(mm.group(1), mm.group(1))
                part = mm.group(2).strip()
            ms = re.search(r"(?:^|\s+)(EK|TK|E|K|N)$", part)
            if ms:
                ex.status = ms.group(1)
                part = part[:ms.start()].strip()
            if part in ("1", "2", "3"):
                ex.suffix = int(part)
            elif part and _year_from(part, fest_year, strict=False) is not None:
                ex.birth_year = _year_from(part, fest_year, strict=False)
            elif part and _LETTER_RE.search(part) and ex.place is None:
                ex.place = part

    m = _CLUB_RE.search(s)
    if m:
        ex.club = m.group(1).strip(" *")
        s = s[:m.start()]

    s = re.sub(r"\s*(\*+)\s*", r" \1 ", s)  # 'Name**' / '*F' -> separate star runs
    s = re.sub(r"(?<=\w)-\s+(?=\w)", "-", s)  # 'Jean- Pierre'
    s = _WS_RE.sub(" ", s).strip()
    m = _GEN_RE.search(s)
    if m:
        ex.generation = "jun" if (m.group(1) or m.group(2)).lower() in ("jun", "jr") else "sen"
        s = _WS_RE.sub(" ", s[:m.start()] + " " + s[m.end():]).strip()

    # peel trailing tokens until stable: stars, status, marker, code, year/suffix
    prev = None
    while prev != s:
        prev = s
        m = _TAIL_TOKEN_RE.search(s)
        if m and len(s[:m.start()].split()) >= 1:
            tok = m.group(1) or m.group(2)
            if "*" in tok:
                ex.stars = max(ex.stars, tok.count("*"))
                if tok[0] in "ST":
                    ex.st_marker = tok[0]
            elif tok in ("S", "T"):
                ex.st_marker = tok
            else:
                ex.status = tok
            s = s[:m.start()].strip()
            continue
        m = _ROMAN_RE.search(s)
        if m and len(s[:m.start()].split()) >= 2:
            ex.ordinal = m.group(1)
            s = s[:m.start()].strip()
            continue
        m = _TRAIL_NUM_RE.search(s)
        if m:
            num = m.group(1)
            y = _year_from(num, fest_year) if len(num) in (2, 4) else None
            if y is not None:
                ex.birth_year = y
            elif num in ("1", "2", "3"):
                ex.suffix = int(num)
            else:
                ex.number = int(num)
            s = s[:m.start()].strip()
            continue
        m = _CODE_RE.search(s)
        if m and len(s.split()) >= 3:
            ex.code = m.group(1)
            s = s[:m.start()].strip()
            continue
    s = s.replace("*", "").strip()

    if _CAMEL_RE.search(s):
        ex.flags.append("glued_words")
        s = _CAMEL_RE.sub(" ", s)
    s = _WS_RE.sub(" ", " ".join(_smart_case(t) for t in s.split())).strip(" ,.-")

    if ex.birth_year is not None and fest_year is not None and not (
            fest_year - 60 <= ex.birth_year <= fest_year - 12):
        ex.flags.append("implausible_birth_year")
    letters = _LETTER_RE.findall(s.replace("?", "x"))
    if len(letters) < 2 or re.search(r"\d", s):
        ex.flags.append("not_a_name")
    elif len(s.split()) == 1:
        ex.flags.append("surname_only")
    return s, ex


# ----------------------------------------------------------------------------- keys
# 'e' after a/o/u is an umlaut digraph, except in the diphthongs au/eu/ou ('Bauer')
_FOLD_DIGRAPH_RE = re.compile(r"(?<=[aou])(?<![aeo]u)e")
_SPECIAL = str.maketrans({"ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d"})


@lru_cache(maxsize=200_000)
def name_key(name: str) -> str:
    """Canonical comparison key for a (clean) name.

    casefold; ß→ss; accents and umlauts stripped (``ä``→``a``, ``é``→``e``); the
    digraphs ``ae``/``oe``/``ue`` fold to ``a``/``o``/``u`` so ``Müller``,
    ``Mueller`` and ``Muller`` share a key; hyphens, dots and commas become spaces;
    apostrophes vanish (``Z'Rotz`` → ``zrotz``); ``?`` (unknown glyph) is kept;
    all other non-letters are dropped. Token order is kept ("last first").
    """
    s = name.casefold().translate(_SPECIAL)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("'", "").replace("`", "")
    s = re.sub(r"[-.,/_]", " ", s)
    s = re.sub(r"[^a-z? ]", "", s)
    s = _FOLD_DIGRAPH_RE.sub("", s)
    return _WS_RE.sub(" ", s).strip()


def key_tokens(key: str) -> list[str]:
    return key.split()


def compact_key(key: str) -> str:
    """Key without spaces: 'auf der maur alex' ~ 'aufdermaur alex' after compaction."""
    return key.replace(" ", "")


def has_wildcard(key: str) -> bool:
    return "?" in key


@lru_cache(maxsize=4096)
def _wildcard_re(key: str) -> re.Pattern[str]:
    # '?' replaced one non-ASCII glyph: an umlaut/accented letter (1 letter after
    # folding) or a ligature ff/fl/fi/ffi (2-3 letters)
    parts = [re.escape(p) for p in key.split("?")]
    return re.compile("[a-z]{1,3}".join(parts))


def wildcard_match(pattern_key: str, key: str) -> bool:
    """True if ``key`` matches ``pattern_key`` with each ``?`` = 1–3 letters."""
    if "?" not in pattern_key:
        return pattern_key == key
    return _wildcard_re(pattern_key).fullmatch(key) is not None


def edit_distance(a: str, b: str, limit: int = 3) -> int:
    """Optimal-string-alignment distance (insert/delete/substitute/adjacent swap).

    Returns ``limit + 1`` early once the distance is known to exceed ``limit``."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return min(prev[-1], limit + 1)


# Common Swiss-German short forms; used only to *propose* candidates.
FIRST_NAME_ALIASES: dict[str, str] = {
    "michi": "michael", "andi": "andreas", "ruedi": "rudolf", "rudi": "rudolf",
    "sami": "samuel", "dani": "daniel", "beni": "benjamin", "chrigel": "christian",
    "chrigu": "christian", "koebi": "jakob", "kobi": "jakob", "seppi": "josef",
    "sepp": "josef", "toni": "anton", "hausi": "hans", "hansueli": "hans ulrich",
    "ueli": "ulrich", "uli": "ulrich", "fredi": "alfred", "werni": "werner",
    "matthi": "matthias", "nik": "niklaus", "niki": "niklaus",
    "klaus": "niklaus", "stefi": "stefan", "christof": "christoph", "philip": "philipp",
    "mike": "michael",
}


_ALIASES_FOLDED: dict[str, str] = {}


def first_name_canonical(token: str) -> str:
    """Folded first-name token mapped through :data:`FIRST_NAME_ALIASES`."""
    if not _ALIASES_FOLDED:
        _ALIASES_FOLDED.update({name_key(k): name_key(v) for k, v in FIRST_NAME_ALIASES.items()})
    t = name_key(token)
    return _ALIASES_FOLDED.get(t, t)


def _split(key: str) -> tuple[str, str]:
    """(surname, given names) of a 'last first' key; the last token is the given name.

    Multi-token surnames ('auf der maur alex', 'von buren stephan') stay together;
    double given names are mis-split ('kenel franz toni' -> 'kenel franz', 'toni'),
    which is harmless because both sides are split the same way."""
    toks = key.split()
    if len(toks) < 2:
        return key, ""
    return " ".join(toks[:-1]), toks[-1]


def name_similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] between two *keys*; 1.0 = same key.

    Scores (highest applicable): identical 1.0; wildcard match 0.95; identical after
    removing spaces (``vonlanthen`` / ``von lanthen``, ``franz toni`` / ``franztoni``)
    0.95; same tokens in another order (``first last``) 0.9; first-name alias 0.85;
    one edit in the whole key 0.85 (two edits 0.7 for keys of ≥ 12 letters);
    otherwise a normalised edit-distance score capped below 0.7.
    """
    if a == b:
        return 1.0
    if "?" in a or "?" in b:
        if wildcard_match(a, b) or wildcard_match(b, a):
            return 0.95
        if "?" in a and "?" in b and len(a) == len(b) and all(
                x == y or "?" in (x, y) for x, y in zip(a, b)):
            return 0.9
    ca, cb = compact_key(a), compact_key(b)
    if ca == cb:
        return 0.95
    ta, tb = a.split(), b.split()
    if len(ta) >= 2 and sorted(ta) == sorted(tb):
        return 0.9
    (la, fa), (lb, fb) = _split(a), _split(b)
    if la == lb and fa and fb and first_name_canonical(fa) == first_name_canonical(fb):
        return 0.85
    longest = max(len(ca), len(cb)) or 1
    d = edit_distance(ca, cb, limit=longest)
    if d == 1:
        return 0.85
    if d == 2 and min(len(ca), len(cb)) >= 12:
        return 0.7
    return round(min(0.69, max(0.0, 1.0 - d / longest)), 4)


# ---------------------------------------------------------------------------- index
def _deletes(s: str) -> set[str]:
    return {s[:i] + s[i + 1:] for i in range(len(s))} | {s}


class NameIndex:
    """Blocking index over keys: finds keys within one edit (on the compact key),
    with swapped token order, sharing a compact form, matching a ``?`` wildcard or
    a first-name alias. Built once in O(n·L); each query is ~O(L) dictionary lookups
    (plus a linear scan only for the rare wildcard keys)."""

    def __init__(self, keys: Iterable[str]) -> None:
        self.keys: list[str] = sorted(set(keys))
        self._by_delete: dict[str, set[str]] = {}
        self._by_sorted: dict[str, set[str]] = {}
        self._by_alias: dict[str, set[str]] = {}
        self._wild: list[str] = []
        for k in self.keys:
            if "?" in k:
                self._wild.append(k)
                continue
            for d in _deletes(compact_key(k)):
                self._by_delete.setdefault(d, set()).add(k)
            self._by_sorted.setdefault(" ".join(sorted(k.split())), set()).add(k)
            last, first = _split(k)
            if first:
                self._by_alias.setdefault(f"{last}|{first_name_canonical(first)}", set()).add(k)

    def candidates(self, key: str, min_score: float = 0.85) -> list[tuple[str, float]]:
        """Other keys similar to ``key`` (score ≥ ``min_score``), best first."""
        found: set[str] = set()
        if "?" in key:
            found.update(k for k in self.keys if k != key and (
                wildcard_match(key, k) or ("?" in k and name_similarity(key, k) >= min_score)))
        else:
            for d in _deletes(compact_key(key)):
                found.update(self._by_delete.get(d, ()))
            found.update(self._by_sorted.get(" ".join(sorted(key.split())), ()))
            last, first = _split(key)
            if first:
                found.update(self._by_alias.get(f"{last}|{first_name_canonical(first)}", ()))
            found.update(k for k in self._wild if wildcard_match(k, key))
        found.discard(key)
        scored = [(k, name_similarity(key, k)) for k in found]
        return sorted(((k, s) for k, s in scored if s >= min_score), key=lambda t: (-t[1], t[0]))


def fuzzy_candidates(key: str, keys: Iterable[str], min_score: float = 0.85) -> list[tuple[str, float]]:
    """Convenience wrapper: build a :class:`NameIndex` over ``keys`` and query it.

    For many queries build the index once and call :meth:`NameIndex.candidates`."""
    return NameIndex(keys).candidates(key, min_score)
