"""Parse schlussgang "Statistische Tabelle" sheets into bouts + raw athletes.

A statistic sheet lists every athlete of a festival with the result of each
Gang: ``<sym> <opponent> <grade>`` where ``sym`` is ``+`` (win), ``-``
(gestellt / draw) or ``o``/``0`` (loss). Every bout therefore appears twice
(once per athlete); bouts are built by pairing the two entries and the outcome
is derived from *both* sides (symbols, with grades as a fallback).

Supported layouts (see Phase 2 notes): ``standard`` (one entry per line, ESV
and most others), ``rang`` (NOSV/Heller 2011-2013: ``Rang: N`` lines,
numbered entries), ``blocks`` (2011-2015: symbol/name/grade runs) and
``multicol`` (three athletes per row). ``empty`` / ``garbled`` sheets are
rejected with a reason. Nothing is dropped silently: every entry that does not
end up in a bout, and every suspicious line, becomes a :class:`Reject`.

Youth-mixed sheets: only blocks in the active section are kept ("Aktive",
"Actif" or no section header at all); youth categories are counted.
"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd

# ----------------------------------------------------------------------------- model
OUTCOMES = ("WIN_A", "WIN_B", "DRAW")
GRADE_MIN, GRADE_MAX = 8.25, 10.00


@dataclass
class Entry:
    sym: str                   # '+', '-', 'o'
    opponent: str              # raw opponent text (without grade)
    grade: float | None
    line: int
    gang: int | None = None    # explicit gang number (rang layout) else position
    extra: bool = False        # 'z' line (extra bout, no grade)
    placeholder: bool = False  # grade printed as 0.00 / 0.25 (extra bout or forfeit)
    missing_grade: bool = False  # no grade printed at all ("0 Laimbacher Philipp S***")
    forfeit: bool = False      # 'u' / '> unfall': decided by injury, not a real result
    schlussgang: bool = False  # 's+' marker in block layouts
    ambiguous: bool = False    # multicol: column assignment uncertain


@dataclass
class Block:
    rank: str | None
    name_raw: str
    points: float | None
    line: int
    mark: str = ""             # '*' (Kranz/award) or '°' (withdrawn) before the name
    interim: bool = False      # block taken from an interim sheet (see merge_interim_sheet)
    entries: list[Entry] = field(default_factory=list)
    # printed beside the name in table layouts (grid parser); override the hints that
    # are otherwise read from the name text
    place: str | None = None
    association: str | None = None


@dataclass
class Sheet:
    layout: str
    header: list[str]
    blocks: list[Block]                         # active section only
    youth_blocks: int = 0
    youth_sections: list[str] = field(default_factory=list)
    youth_names: set[str] = field(default_factory=set)  # base name keys in youth sections
    has_active_section: bool | None = None      # None: sheet has no sections at all
    suspicious: list[tuple[int, str, str]] = field(default_factory=list)  # (line, text, reason)
    noise: list[tuple[int, str]] = field(default_factory=list)  # ignored lines (titles, footers…)
    youth_lines: set[int] = field(default_factory=set)  # entry lines skipped in youth sections
    glyph_ids: str | None = None  # text layer decoded from glyph ids ('mac' | 'lossy')

    @property
    def noise_lines(self) -> int:
        return len(self.noise)


@dataclass(frozen=True)
class Reject:
    fest_id: int
    stage: str       # 'festival' | 'athlete' | 'entry' | 'bout' | 'line'
    reason: str
    detail: str = ""
    line: int | None = None


@dataclass
class FestivalParse:
    fest_id: int
    layout: str
    status: str                      # 'ok' | 'partial' | 'failed' | 'header_mismatch'
    athletes: list[dict[str, object]] = field(default_factory=list)
    bouts: list[dict[str, object]] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)
    header_check: str = ""
    youth_blocks: int = 0
    entries_total: int = 0
    gang_count: int | None = None    # Gänge derived from the sheet (festival_gang_count)


# ----------------------------------------------------------------------------- text utils
_GRADE = r"(?:[89]|10)[.,]\d{2}"
_PH = r"0[.,](?:00|25)"  # placeholder grade of an extra bout / forfeit
_NUM = r"\d{1,2}[.,]\d{2}"
GRADE_TOKEN_RE = re.compile(rf"(?<![\d.,]){_GRADE}(?![\d.,])")


# ------------------------------------------------------------- glyph-id text layers
# Some PDFs (Thurgau 2015, Genf 2013, several 2021 sheets) embed TrueType subsets
# without a Unicode map; PDFium then returns glyph ids instead of characters.
# In the standard Macintosh glyph order id = ASCII - 29 (3 = space … 93 = 'z') and
# ids 98+ follow Mac Roman from 0x80 (108 = 'ä', 124 = 'ö', 129 = 'ü', 112 = 'é').
_PLAUSIBLE_NON_ASCII = set("äöüéèêëàâáçïîôûÄÖÜÉ©ß")


def glyph_id_encoding(text: str) -> str | None:
    """'mac' (standard glyph order, exact), 'lossy' (ASCII exact, other glyphs
    unknown) or None (normal text layer)."""
    ctrl = sum(1 for c in text if ord(c) < 0x20 and c not in "\n\r\t\f")
    if ctrl < 20 or ctrl < 0.05 * len(text):
        return None
    ascii_ = "".join(chr(ord(c) + 29) if ord(c) < 98 and c not in "\n\r\t\f " else c
                     for c in text)
    if len(GRADE_TOKEN_RE.findall(ascii_)) < 10:  # another (unknown) glyph order: give up
        return None
    high = {_mac_glyph(c) for c in text if 98 <= ord(c) < 226}
    return "mac" if high <= _PLAUSIBLE_NON_ASCII else "lossy"


def _mac_glyph(c: str) -> str:
    return bytes([ord(c) + 30]).decode("mac_roman")


def decode_glyph_ids(text: str) -> str:
    """Decode a glyph-id text layer (see :func:`glyph_id_encoding`); names lose
    their inner spaces in these PDFs, so "BöschDaniel" -> "Bösch Daniel"."""
    enc = glyph_id_encoding(text)
    if enc is None:
        return text
    out: list[str] = []
    open_paren = False
    for ch in text:
        o = ord(ch)
        if ch == "\f" and open_paren:            # id 12 = ')' (not a page break)
            ch, o = ")", -1
        if o == -1 or ch in "\n\r\t\f ":
            out.append(ch)
        elif o < 98:
            out.append(chr(o + 29))
        elif o < 226:
            out.append(_mac_glyph(ch) if enc == "mac" else "?")
        else:
            out.append(ch)
        if out[-1] in "\n\f":
            open_paren = False
        elif out[-1] in "()":
            open_paren = out[-1] == "("
    return "\n".join(re.sub(r"(?<=[a-zß-ÿ?])(?=[A-ZÀ-Þ])", " ", ln)
                     for ln in "".join(out).split("\n"))


def _unshift(ch: str) -> str:
    if ch.isspace():
        return ch
    try:
        b = ch.encode("mac_roman")[0]
    except UnicodeEncodeError:
        try:   # PDFium returns U+2126 (ohm sign) for Mac Roman 0xBD (Greek omega)
            b = unicodedata.normalize("NFKC", ch).encode("mac_roman")[0]
        except UnicodeEncodeError:
            return "?"
    orig = b - 29 if b < 0x7F else b - 30
    if orig < 0x20:
        return "?"
    return bytes([orig]).decode("mac_roman")


def decode_shifted_text(text: str) -> str | None:
    """Text layers whose character codes are shifted by 29 (30 above 0x7E), read as Mac
    Roman - three ISV sheets of 2006 / 2008 ("pÅÜäìëëê~åÖäáëíÉ" = "Schlussrangliste",
    "=" = space). Returns the decoded text, or None if the text is not of this kind."""
    printable = [c for c in text if not c.isspace()]
    if len(printable) < 200 or text.count("=") < 0.08 * len(printable):
        return None
    decoded = "".join(_unshift(c) for c in text)
    if len(GRADE_TOKEN_RE.findall(decoded)) < 10:
        return None
    return decoded


def normalize_text(text: str) -> list[str]:
    """NFKC (ligatures), unify newlines/whitespace; returns stripped lines (keeps blanks out)."""
    text = unicodedata.normalize("NFKC", decode_glyph_ids(text)).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\f", "\n").replace("\u00a0", " ").replace("\u2019", "'")
    text = text.replace("\u00ad", "").replace("\u2010", "-").replace("\u2013", "-")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    # page footer glued to an entry: "o Häller Nick 8,50 Seite 2/3"; letter rank glued
    # to the name: "12zaBieri Marcel * 36.75" (ESAF 2013 interim sheet)
    return [_GLUED_RANK_RE.sub(r"\1 \2", _GLUED_PAGE_RE.sub(r"\1", ln)) for ln in lines]


_GLUED_PAGE_RE = re.compile(r"(\d[.,]\d{2})\s+Seite\s+\d+\s*/\s*\d+$")
_GLUED_RANK_RE = re.compile(r"^(\d+[a-z]{1,2})([A-ZÄÖÜ][a-zäöüéè]+\s.*\d[.,]\d{2}\s*\*?)$")


def to_float(s: str) -> float:
    return float(s.replace(",", "."))


_ACTIVE_SECTION_RE = re.compile(r"^(aktive?|actifs?|aktivschwinger|aktive schwinger)$", re.I)
_YOUTH_SECTION_RE = re.compile(
    r"^(kat(egorie|\.)?\b.*|jungschwinger\b.*|nachwuchs\b.*|js\s.*|jahrgang\b.*|jg\.?\s.*"
    r"|knaben\b.*|buben\b.*|(19|20)\d{2}(\s*[-/]\s*(19|20)?\d{2})*(\s*(et|und)\b.*)?)$", re.I)


def section_of(line: str, fest_year: int | None = None) -> str | None:
    """'active' / 'youth' if the line is a category header, else None.

    A bare year ("1997", "1999-2000") is a youth birth-year category, unless it
    is the festival's own year (wrapped title "…Interlaken" / "2023")."""
    if GRADE_TOKEN_RE.search(line) or len(line) > 40:
        return None
    if _ACTIVE_SECTION_RE.match(line):
        return "active"
    if _YOUTH_SECTION_RE.match(line):
        m = re.fullmatch(r"((?:19|20)\d{2})", line.strip())
        if m and (fest_year is None or int(m.group(1)) > fest_year - 10):
            return None
        return "youth"
    return None


# names -------------------------------------------------------------------------------
# status letters: E/K/EK/TK (Kranz), N (Neukranzer, Heller), S/T (Sennen/Turner)
_STATUS_TAIL_RE = re.compile(r"(?:\s+(?:[ST]?\*+|\*+|EK|TK|E|K|N|[ST]))+$")
_PAREN_RE = re.compile(r"\s*\(([^)]*)\)")


_CODE_TAIL_RE = re.compile(r"(?<=[a-zà-ÿ])\s+[A-Z]{2,3}$")


def code_of(raw: str) -> str | None:
    """Trailing 2-3 letter Gau/club code ("OB", "ET", "NWS") printed after the name."""
    s = re.sub(r"[*°]", " ", raw).strip()
    s = re.sub(rf"\s+(EK|TK)$", "", s)
    m = re.search(r"[a-zà-ÿ]\s+([A-Z]{2,3})$", s)
    return m.group(1) if m else None


def clean_name(raw: str) -> str:
    """Printed name without Kranz stars / status letters; keeps suffixes, (…) and digits."""
    s = raw.replace("*", " * ").strip()
    s = re.sub(r"\s+", " ", s)
    prev = None
    while prev != s:
        prev = s
        s = _STATUS_TAIL_RE.sub("", s).strip()
        s = re.sub(r"\s*\*+\s*", " ", s).strip()  # stars inside, e.g. "Name * (03)"
    s = re.sub(r",\s*[A-Z]{1,2}(?=\s*(\(|$))", "", s).strip()  # ", S" category suffix
    s = _CODE_TAIL_RE.sub("", s).strip()  # "Glarner Matthias OB" (Gau/club code)
    return s.rstrip(" ,")


def status_of(raw: str) -> str:
    """Kranz status as printed: star count ('*'..'***') or letters (E/K/EK/TK)."""
    stars = raw.count("*")
    if stars:
        return "*" * min(stars, 3)
    m = re.search(r"\s(EK|TK|E|K|N)$", raw.strip())
    return m.group(1) if m else ""


def _st_marker(raw: str) -> str | None:
    """'S' (Sennenschwinger) / 'T' (Turnerschwinger) if printed (', S' or 'S**')."""
    m = re.search(r",\s*([ST])\b", raw) or re.search(r"\s([ST])\*+\s*$", raw.strip())
    return m.group(1) if m else None


def name_keys(name: str) -> tuple[str, str]:
    """(full key incl. parentheses, base key without parentheses / ', place')."""
    full = re.sub(r"\s+", " ", name.casefold()).strip()
    base = _PAREN_RE.sub("", full)
    base = re.sub(r",.*$", "", base).strip()
    return full, base


def name_details(name: str) -> dict[str, str | None]:
    """Birth year / association / place hints from parentheses or ', place'."""
    out: dict[str, str | None] = {"birth_year": None, "association": None, "place": None}
    for inner in _PAREN_RE.findall(name):
        inner = inner.strip()
        if re.fullmatch(r"(19|20)?\d{2}", inner):
            y = int(inner)
            out["birth_year"] = str(y if y > 1900 else (2000 + y if y < 40 else 1900 + y))
        elif re.fullmatch(r"[A-Z]{2,4}", inner):
            out["association"] = inner
        else:
            out["place"] = inner
    m = re.search(r",\s*([^,(]{3,})$", name)
    if m and not out["place"]:
        out["place"] = m.group(1).strip()
    return out


# ----------------------------------------------------------------------------- layouts
_SYM = r"(?P<sym>s?[+\-o0])"
BOUT_LINE_RE = re.compile(rf"^(?P<z>z\s+)?{_SYM}\s+(?P<name>\D.*?)"
                          rf"(?:\s+(?:(?P<grade>{_GRADE})|(?P<ph>{_PH})))?$")
# anything that looks like a bout entry (symbol + capitalised name); such a line
# must never be ignored silently
_NAME_START = r"(?:[A-ZÄÖÜÉÈÀÂÇ]|(?:von|van|de|di|da|del|della|la|le|du|dos)\s+[A-ZÄÖÜÉÈÀÂÇ])"
BOUT_LIKE_RE = re.compile(rf"^(?:z\s+)?(?:s?[+\-]\s*|s?[o0u]\s+){_NAME_START}[\w'\-]")
# a printed opponent name: capitalised words, optional stars/status letters,
# "(2001)" / "(SWS)" / "(Bonaduz)", suffix " 1"/" 2"; no other digits
_NAME_LIKE_RE = re.compile(
    rf"^{_NAME_START}[\w'\-.]*(?:\s+(?:[^\W\d][\w'\-.]*\**|[ST]?\*+|\((?:\d{2}|\d{4}|[^\d()]+)\)))+"
    r"(?:\s+[12])?(?:\s+[ST]?\*+)?$")
# Names may contain "(2002)" birth years, "(SWS)" and a " 1"/" 2" suffix.
# rank "1", "1.", "2.a", "26aa", "1 a", bare "b"; mark '*'/'°' may be glued ("3a*");
# points may be glued to stars or name ("*54,25", "Martin54.25")
HEADER_RE = re.compile(
    rf"^(?P<rank>[1-9]\d*\s*\.?\s*[a-z]{{0,2}}\.?|[a-z]{{1,2}})(?P<mark>\s*[*°])?\s+(?:[*°]\s+)?"
    rf"(?P<name>[^\d\s+\-*°].*?)(?:\s+|(?<=[*a-zà-ÿ]))S?(?P<points>{_NUM})"
    rf"(?:\s*(?P<rank2>[a-z]))?(?:\s*\*)?$")
# two-line header: "1 Glarner Matthias, S" + "*** * 78.25" (2015/16 sheets)
RANK_NAME_RE = re.compile(
    r"^(?P<rank>[1-9]\d*\s*\.?\s*[a-z]{0,2})(?P<mark>\s*[*°])?\s+(?:[*°]\s+)?"
    r"(?P<name>[^\d\s+\-*°][^\d]*?)$")
STARS_ONLY_RE = re.compile(r"^[*°](?:\s*[*°])*$")
STARS_POINTS_RE = re.compile(
    rf"^(?:\((?P<birth>(?:19|20)\d{{2}})\)\s*)?(?P<stars>[*°](?:\s*[*°])*)?\s*S?(?P<points>{_NUM})$")
RANK_ONLY_RE = re.compile(r"^(?P<rank>[1-9]\d*\s*\.?\s*[a-z]?|[a-z])\s*(?P<mark>[*°])?$")
NAME_POINTS_RE = re.compile(rf"^(?P<mark>[*°]\s+)?(?P<name>[^\d\s+\-*°].*?)\s+S?(?P<points>{_NUM})$")
# "o 8.75 Hurschler Thomas **" (symbol, grade, opponent)
BOUT_REVERSED_RE = re.compile(rf"^{_SYM}\s+(?P<grade>{_GRADE})\s+(?P<name>\D+)$")
# header without rank: "Schär Ari 54.75" (accepted only if a bout line follows)
NAME_ONLY_HEADER_RE = re.compile(rf"^(?P<name>[A-ZÄÖÜÉÀ][^\d+]*?)\s+(?P<points>{_NUM})$")
SYM_ONLY_RE = re.compile(r"^(?P<s>s)?(?P<sym>[+\-o0])$")
GRADE_ONLY_RE = re.compile(rf"^(?P<grade>{_GRADE})$")
GRADE_OR_PH_ONLY_RE = re.compile(rf"^(?:(?P<grade>{_GRADE})|(?P<ph>{_PH}))$")
POINTS_ONLY_RE = re.compile(rf"^(?P<points>{_NUM})$")
RANG_LINE_RE = re.compile(r"^Rang:\s*(?P<rank>\d+[a-z]?)\b(?P<rest>.*)$")
RANG_ENTRY_RE = re.compile(
    rf"^(?P<gang>[1-8])\s*(?P<sym>[+\-o0u])\s+(?P<name>\D.*?)\s+(?P<grade>{_GRADE})$")
RANG_ENTRY2_RE = re.compile(
    rf"^(?P<gang>[1-8])\s+(?P<name>\D.*?)\s+(?P<sym>[+\-o0])\s+(?P<grade>{_GRADE})$")


def _sym(s: str) -> str:
    s = s.lstrip("s")
    return "o" if s == "0" else s


def detect_layout(lines: list[str]) -> str:
    text = [ln for ln in lines if ln]
    if len("".join(text)) < 50:
        return "empty"
    if any("notenblätterdetails" in ln.casefold() for ln in text[:5]):
        return "notenblatt"
    rang = sum(1 for ln in text if RANG_LINE_RE.match(ln))
    if rang >= 3:
        return "rang"
    sym_only = sum(1 for ln in text if SYM_ONLY_RE.match(ln))
    multi = sum(1 for ln in text if len(GRADE_TOKEN_RE.findall(ln)) >= 2)
    single = sum(1 for ln in text if BOUT_LINE_RE.match(ln)
                 and len(GRADE_TOKEN_RE.findall(ln)) == 1)
    if multi > 10 and multi > single * 0.3:
        return "multicol"
    if sym_only > 20 and sym_only > single:
        return "blocks"
    if single == 0 and not any(ch.isdigit() for ch in "".join(text)):
        return "garbled"
    return "standard"


class _Collector:
    """Shared state for the line-based layouts (sections, blocks, suspicious lines)."""

    def __init__(self, layout: str, fest_year: int | None = None) -> None:
        self.fest_year = fest_year
        self.sheet = Sheet(layout=layout, header=[], blocks=[])
        self.section = "active"
        self.current: Block | None = None
        self.seen_section = False

    def set_section(self, kind: str, line: str) -> None:
        self.seen_section = True
        self.section = kind
        self.current = None
        if kind == "youth":
            self.sheet.youth_sections.append(line)
        else:
            self.sheet.has_active_section = True

    def new_block(self, block: Block) -> None:
        if self.section == "youth":
            self.sheet.youth_blocks += 1
            self.sheet.youth_names.add(name_keys(clean_name(block.name_raw))[1])
            self.current = Block(block.rank, block.name_raw, block.points, block.line)
            self.current_is_youth = True
        else:
            self.sheet.blocks.append(block)
            self.current = block
            self.current_is_youth = False

    def add_entry(self, entry: Entry, text: str) -> None:
        if self.current is None:
            self.suspicious(entry.line, text, "entry_without_athlete")
            return
        if getattr(self, "current_is_youth", False):
            self.sheet.youth_lines.add(entry.line)
        else:
            self.current.entries.append(entry)

    def suspicious(self, line: int, text: str, reason: str) -> None:
        if self.section == "youth":  # youth categories are skipped on purpose
            self.sheet.youth_lines.add(line)
            return
        self.sheet.suspicious.append((line, text, reason))

    def other(self, i: int, ln: str) -> None:
        if not self.sheet.blocks and self.current is None and not self.seen_section \
                and not BOUT_LIKE_RE.match(ln):
            if len(self.sheet.header) < 8:
                self.sheet.header.append(ln)
            return
        self.unparsed(i, ln)

    def unparsed(self, i: int, ln: str) -> None:
        """A line no layout rule consumed: bout-like and graded lines become
        rejects; the rest (titles, footers, 'ausgeschieden'/'Unfall' fillers) is noise."""
        if BOUT_LIKE_RE.match(ln) and len(GRADE_TOKEN_RE.findall(ln)) <= 1:
            self.suspicious(i, ln, "unparsed_bout_line")
        elif GRADE_TOKEN_RE.search(ln) and not _is_footer(ln):
            self.suspicious(i, ln, "unparsed_line_with_grade")
        elif self.section != "youth":
            self.sheet.noise.append((i, ln))

    def finish(self) -> Sheet:
        if self.seen_section and self.sheet.has_active_section is None:
            self.sheet.has_active_section = False
        return self.sheet


_FOOTER_RE = re.compile(r"(quelle|seite|©|kranzschwinger|\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})", re.I)


def _is_footer(ln: str) -> bool:
    return bool(_FOOTER_RE.search(ln))


# first line of a broken header: a rank, optionally a mark and name text, no numbers
_HEADER_START_RE = re.compile(
    r"^(?:[1-9]\d*\s*\.?\s*[a-z]{0,2}\.?|[a-z]{1,2})(?:\s*[*°])?(?:\s+\D*)?$")


def _bout_line_at(lines: list[str], k: int) -> bool:
    """Is line ``k`` a bout entry (graded, placeholder, 'z', wrapped or no-grade)?"""
    m = BOUT_LINE_RE.match(lines[k]) if 0 <= k < len(lines) else None
    return bool(m and (m.group("grade") or m.group("ph") or m.group("z")
                       or _NAME_LIKE_RE.match(m.group("name").strip())))


def _join_header(lines: list[str], i: int, max_lines: int = 5) -> tuple[re.Match[str], int] | None:
    """Header broken over several lines ("10" / "e" / "Name, S" / "*" / "56.25").

    Joins up to ``max_lines`` non-bout lines; accepted only if the joined text is
    a header and the next non-empty line is a graded bout line. Returns the
    match and the index of the first line after the header."""
    if not _HEADER_START_RE.match(lines[i]) or BOUT_LINE_RE.match(lines[i]) \
            and not re.match(r"^[1-9]", lines[i]):
        return None
    parts: list[str] = []
    j = i
    while j < len(lines) and len(parts) < max_lines:
        ln = lines[j]
        j += 1
        if not ln:
            continue
        if _bout_line_at(lines, j - 1) or section_of(ln) or \
                (parts and re.search(r"\d", ln) and not STARS_POINTS_RE.match(ln)
                 and not HEADER_RE.match(" ".join(parts + [ln]))):
            return None
        parts.append(ln)
        if len(parts) < 2:
            continue
        h = HEADER_RE.match(" ".join(parts))
        if h:
            k = j
            while k < len(lines) and not lines[k]:
                k += 1
            if _bout_line_at(lines, k):
                return h, j
    return None


def _bout_entry(m: re.Match[str], lines: list[str], i: int,
                injury: bool = False) -> tuple[Entry, int] | None:
    """Entry for a matched bout line plus the index of the next unread line.

    Handles graded lines, placeholder grades (0.00 / 0.25), 'z' lines, lines whose
    grade wrapped onto the next line ("0 Urfer Simon *" / "8.75") and lines with
    no grade at all ("0 Laimbacher Philipp S***"). None if it is not a bout line."""
    sym, name = _sym(m.group("sym")), m.group("name").strip()
    sg = m.group("sym").startswith("s")
    if m.group("grade") or m.group("ph") or m.group("z"):
        g = m.group("grade") or m.group("ph")
        return Entry(sym, name, to_float(g) if g else None, i, extra=bool(m.group("z")),
                     placeholder=bool(m.group("ph")), forfeit=injury, schlussgang=sg), i + 1
    if not _NAME_LIKE_RE.match(name):
        return None
    j = i + 1
    while j < len(lines) and not lines[j]:
        j += 1
    w = GRADE_OR_PH_ONLY_RE.match(lines[j]) if j < len(lines) else None
    if w:  # wrapped: the grade is on the next line
        return Entry(sym, name, to_float(w.group(0)), i, placeholder=bool(w.group("ph")),
                     forfeit=injury, schlussgang=sg), j + 1
    return Entry(sym, name, None, i, missing_grade=True, forfeit=injury, schlussgang=sg), i + 1


def parse_standard(lines: list[str], fest_year: int | None = None) -> Sheet:
    c = _Collector("standard", fest_year)
    last_rank_num: str | None = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln:
            i += 1
            continue
        sec = section_of(ln, c.fest_year)
        if sec:
            c.set_section(sec, ln)
            i += 1
            continue
        injury = bool(_INJURY_NOTE_RE.search(ln))
        ln = _INJURY_NOTE_RE.sub("", ln)
        m = BOUT_LINE_RE.match(ln)
        if m and len(GRADE_TOKEN_RE.findall(ln)) <= 1:
            got = _bout_entry(m, lines, i, injury)
            if got is not None:
                c.add_entry(got[0], ln)
                i = got[1]
                continue
        rv = BOUT_REVERSED_RE.match(ln)
        if rv:
            c.add_entry(Entry(_sym(rv.group("sym")), rv.group("name").strip(),
                              to_float(rv.group("grade")), i, forfeit=injury,
                              schlussgang=rv.group("sym").startswith("s")), ln)
            i += 1
            continue
        h = HEADER_RE.match(ln)
        if h and not (h.group("rank") in ("o",) and to_float(h.group("points")) <= 10):
            rank = re.sub(r"\s+", "", h.group("rank")) + (h.group("rank2") or "")
            if rank.isalpha() and last_rank_num:      # "b Kündig Edi" continues "2.a"
                rank = last_rank_num + rank
            last_rank_num = re.match(r"\d*", rank).group(0) or last_rank_num
            c.new_block(Block(rank.rstrip("."), h.group("name").strip(),
                              to_float(h.group("points")), i,
                              mark=(h.group("mark") or "").strip() or
                              ("*" if re.match(r"^\S+\s+\*\s", ln) else "")))
            i += 1
            continue
        rn = RANK_NAME_RE.match(ln)
        if rn and i + 1 < len(lines) and not BOUT_LINE_RE.match(ln):
            # "4 Schuler Christian, S" [+ "***"] + "77.00"  or  + "*** * 78.25"
            j, star_lines = i + 1, []
            while j < len(lines) - 1 and not lines[j] and j < i + 4:  # blank lines
                j += 1
            while j < min(i + 6, len(lines)) and STARS_ONLY_RE.match(lines[j]):
                star_lines.append(lines[j])
                j += 1
            sp = STARS_POINTS_RE.match(lines[j]) if j < len(lines) else None
            if sp:
                groups = [g for g in " ".join(star_lines + [sp.group("stars") or ""]).split() if g]
                rank = re.sub(r"\s+", "", rn.group("rank")).rstrip(".")
                last_rank_num = re.match(r"\d*", rank).group(0) or last_rank_num
                # first star group = Kranz status, a further lone '*' = award mark
                name_raw = rn.group("name").strip() \
                    + (f" ({sp.group('birth')})" if sp.group("birth") else "") \
                    + (" " + groups[0] if groups else "")
                mark = "*" if len(groups) > 1 else (rn.group("mark") or "").strip()
                c.new_block(Block(rank, name_raw, to_float(sp.group("points")), i, mark=mark))
                i = j + 1
                continue
        nh = NAME_ONLY_HEADER_RE.match(ln)
        if nh and i + 1 < len(lines) and to_float(nh.group("points")) > 10:
            if _bout_line_at(lines, i + 1):
                c.new_block(Block(None, nh.group("name").strip(), to_float(nh.group("points")), i))
                i += 1
                continue
        joined = _join_header(lines, i)
        if joined:
            h, j = joined
            rank = re.sub(r"\s+", "", h.group("rank")) + (h.group("rank2") or "")
            if rank.isalpha() and last_rank_num:
                rank = last_rank_num + rank
            last_rank_num = re.match(r"\d*", rank).group(0) or last_rank_num
            c.new_block(Block(rank.rstrip("."), h.group("name").strip(),
                              to_float(h.group("points")), i,
                              mark=(h.group("mark") or "").strip()))
            i = j
            continue
        r = RANK_ONLY_RE.match(ln)
        if r and i + 1 < len(lines):
            nxt = NAME_POINTS_RE.match(lines[i + 1])
            if nxt:
                rank = re.sub(r"\s+", "", r.group("rank")).rstrip(".")
                if rank.isalpha() and last_rank_num:  # "g" continues "12a"
                    rank = last_rank_num + rank
                last_rank_num = re.match(r"\d*", rank).group(0) or last_rank_num
                mark = (r.group("mark") or nxt.group("mark") or "").strip()
                c.new_block(Block(rank, nxt.group("name").strip(),
                                  to_float(nxt.group("points")), i, mark=mark))
                i += 2
                continue
        c.other(i, ln)
        i += 1
    return c.finish()


def parse_blocks(lines: list[str], fest_year: int | None = None) -> Sheet:
    """Header, then N symbol lines, N opponent lines, N grade lines, '*'."""
    c = _Collector("blocks", fest_year)
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if not ln or ln == "*":
            i += 1
            continue
        sec = section_of(ln, c.fest_year)
        if sec:
            c.set_section(sec, ln)
            i += 1
            continue
        h = HEADER_RE.match(ln)
        if h:
            c.new_block(Block(re.sub(r"\s+", "", h.group("rank")).rstrip("."),
                              h.group("name").strip(), to_float(h.group("points")), i,
                              mark=(h.group("mark") or "").strip()))
            j = i + 1
            syms: list[tuple[int, re.Match[str]]] = []
            while j < n and SYM_ONLY_RE.match(lines[j]):
                syms.append((j, SYM_ONLY_RE.match(lines[j])))  # type: ignore[arg-type]
                j += 1
            # the symbol column is padded to the full Gang count; the number of
            # bouts is the number of opponent lines before the grade run
            names: list[str] = []
            while j < n and lines[j] and not GRADE_ONLY_RE.match(lines[j]) \
                    and not HEADER_RE.match(lines[j]) and lines[j] != "*":
                names.append(lines[j])
                j += 1
            k = len(names)
            grades = lines[j:j + k]
            if k and len(syms) >= k and len(grades) == k \
                    and all(GRADE_ONLY_RE.match(g) for g in grades):
                for (li, sm), nm, gr in zip(syms[:k], names, grades):
                    c.add_entry(Entry(_sym(sm.group("sym")), nm, to_float(gr), li,
                                      schlussgang=bool(sm.group("s"))), nm)
                i = j + k
                # further entries printed as normal lines after the runs, e.g. the
                # Schlussgang loser's "0 Laimbacher Philipp S***" (no grade)
                while i < n and (bm := BOUT_LINE_RE.match(lines[i])):
                    got = _bout_entry(bm, lines, i)
                    if got is None:
                        break
                    c.add_entry(got[0], lines[i])
                    i = got[1]
            else:
                if syms or names:
                    c.suspicious(i, ln, "block_structure_broken")
                i = j
            continue
        c.other(i, ln)
        i += 1
    return c.finish()


def parse_rang(lines: list[str], fest_year: int | None = None) -> Sheet:
    """NOSV/Heller layout: points line, 'Name, Place STATUS', 'Rang: N …', numbered entries."""
    c = _Collector("rang", fest_year)
    n = len(lines)
    i = 0
    while i < n:
        ln = lines[i]
        if not ln or ln == "*":
            i += 1
            continue
        rm = RANG_LINE_RE.match(ln)
        if rm:
            # the two preceding non-empty lines are points + name
            prev = [x for x in lines[max(0, i - 4):i] if x and x != "*"]
            pts = POINTS_ONLY_RE.match(prev[-2]) if len(prev) >= 2 else None
            name = prev[-1] if prev else ""
            if pts and name and not POINTS_ONLY_RE.match(name):
                if c.sheet.header and c.sheet.header[-2:] == [prev[-2], name]:
                    c.sheet.header = c.sheet.header[:-2]
                c.new_block(Block(rm.group("rank"), name, to_float(pts.group("points")), i,
                                  mark="*" if "*" in rm.group("rest") else ""))
            else:
                c.current = None
                c.suspicious(i, ln, "rang_header_incomplete")
            rest = re.sub(r"^\s*(Kranz)?\s*\*?\s*", "", rm.group("rest"))
            if rest:
                _rang_entry(c, rest, i)
            i += 1
            continue
        if _rang_entry(c, ln, i):
            i += 1
            continue
        if POINTS_ONLY_RE.match(ln):  # start of next block, name + Rang follow
            i += 1
            continue
        sec = section_of(ln, c.fest_year)
        if sec:
            c.set_section(sec, ln)
            i += 1
            continue
        # name line of the next block (consumed when its Rang line arrives)
        if i + 1 < n and RANG_LINE_RE.match(lines[i + 1]):
            i += 1
            continue
        c.other(i, ln)
        i += 1
    return c.finish()


def _rang_entry(c: _Collector, text: str, i: int) -> bool:
    m = RANG_ENTRY_RE.match(text) or RANG_ENTRY2_RE.match(text)
    if not m:
        return False
    sym = m.group("sym")
    c.add_entry(Entry(_sym(sym), m.group("name").strip(), to_float(m.group("grade")),
                      i, gang=int(m.group("gang")), forfeit=sym == "u"), text)
    return True


_MC_TAIL = r"(?:\s+(?P<tail>SK|EK|TK|K|E|U)(?=\s|$))?"
# number preceded by whitespace or glued to an upper-case code ("ET58.00")
_MC_SEP = r"(?:\s+|(?<=[A-Z]))"
# name text may contain a " 1"/" 2" suffix or a 2-digit birth year in the middle
# ("Wicki Jonas S 94 (ISV)", "Röthlisberger Marcel, 94 ET")
_MC_NAME = r"[^\d\s+][^+\d]*?(?:(?:\s\d|,?\s\d{2})(?=\s)[^+\d]*?)?"
# rank "1", "1a", "1 a", "1.", "4. c"; an award '*' may follow the points
_MC_HEADER_CELL = re.compile(
    rf"(?P<rank>[1-9]\d*\.?(?:\s?[a-z](?=\s))?)\s+(?P<name>{_MC_NAME})"
    rf"{_MC_SEP}(?P<points>{_NUM})(?=\s|$){_MC_TAIL}(?P<award>\s+\*(?=\s|$))?")
_MC_BOUT_CELL = re.compile(
    rf"(?P<sym>s?[+\-o0])\s+(?P<name>{_MC_NAME}){_MC_SEP}(?P<grade>{_GRADE})"
    rf"(?=\s|$){_MC_TAIL}")


def _cells(pattern: re.Pattern[str], line: str) -> list[re.Match[str]] | None:
    """All cells if the line consists of cells only (whitespace between), else None."""
    pos, out = 0, []
    for m in pattern.finditer(line):
        if line[pos:m.start()].strip():
            return None
        out.append(m)
        pos = m.end()
    if not out or line[pos:].strip():
        return None
    return out


_INJURY_NOTE_RE = re.compile(r"\s*>\s*unfall\b.*$", re.I)


# header row whose last cell wrapped after its rank: "... 57.75 3 a" / "Moser Michael ... 57.50"
_DANGLING_RANK_RE = re.compile(rf"{_NUM}(?:\s+\S+)?\s+[1-9]\d*\.?(?:\s?[a-z])?$")


def parse_multicol(lines: list[str], fest_year: int | None = None) -> Sheet:
    """Rows with up to ~3 athletes side by side; columns assigned by order in the row."""
    c = _Collector("multicol", fest_year)
    spreadsheet = any(re.fullmatch(r"A B C( [A-Z])+", ln) for ln in lines)
    merged: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        if not ln:
            continue
        if spreadsheet:
            ln = re.sub(r"^\d+\s+(?=\S)", "", ln)  # Excel row numbers
        if merged and (re.fullmatch(r"\([A-Z]{2,4}\)|[EK]|" + _GRADE, ln)) \
                and not GRADE_TOKEN_RE.search(merged[-1][1].split("  ")[-1][-6:]):
            merged[-1] = (merged[-1][0], merged[-1][1] + " " + ln)  # wrapped cell
            continue
        if merged and _DANGLING_RANK_RE.search(merged[-1][1]) and not ln[:1].isdigit():
            merged[-1] = (merged[-1][0], merged[-1][1] + " " + ln)  # "... 3 a" / "Name 57.50"
            continue
        merged.append((i, ln))
    columns: list[Block | None] = []
    for i, ln in merged:
        injury = bool(_INJURY_NOTE_RE.search(ln))
        ln = _INJURY_NOTE_RE.sub("", ln)
        sec = section_of(ln, c.fest_year)
        if sec:
            c.set_section(sec, ln)
            columns = []
            continue
        heads = _cells(_MC_HEADER_CELL, ln)
        bouts = _cells(_MC_BOUT_CELL, ln)
        if heads and not bouts:
            columns = []
            for h in heads:
                c.new_block(Block(re.sub(r"[\s.]", "", h.group("rank")), h.group("name").strip(),
                                  to_float(h.group("points")), i,
                                  mark="°" if h.group("tail") == "U" else
                                  "*" if h.group("award") else ""))
                columns.append(c.current if not getattr(c, "current_is_youth", False) else None)
            continue
        if bouts and columns:
            ambiguous = len(bouts) != len(columns)
            for n_cell, (col, b) in enumerate(zip(columns, bouts)):
                if col is None:
                    c.sheet.youth_lines.add(i)
                    continue
                col.entries.append(Entry(_sym(b.group("sym")), b.group("name").strip(),
                                         to_float(b.group("grade")), i, ambiguous=ambiguous,
                                         forfeit=injury and n_cell == len(bouts) - 1,
                                         schlussgang=b.group("sym").startswith("s")))
            if len(bouts) > len(columns):
                c.suspicious(i, ln, "more_cells_than_columns")
            continue
        c.other(i, ln)
    return c.finish()


NB_NAME_RE = re.compile(
    rf"^(?P<rank>[1-9]\d*(?:\s?[a-z](?=\s))?)\s+(?P<mark>[*°]\s+)?(?P<name>[^\d\s+\-*°][^\d]*?)"
    rf"(?:\s+(?P<next>{_NUM}))?$")


def parse_notenblatt(lines: list[str], fest_year: int | None = None) -> Sheet:
    """AG/BL "Notenblätterdetails" (2011-2014): text order is points(k),
    entries(k), then 'rank name(k)' with points(k+1) glued to the end."""
    c = _Collector("notenblatt", fest_year)
    pending: Block | None = None

    def orphan(block: Block | None) -> None:
        """Entries of a block whose name line never came are rejected one by one."""
        if block is not None and block.entries and not block.name_raw:
            for e in block.entries:
                c.suspicious(e.line, f"{e.sym} {e.opponent} {e.grade}", "block_without_name")
    for i, ln in enumerate(lines):
        if not ln:
            continue
        sec = section_of(ln, c.fest_year)
        if sec:
            c.set_section(sec, ln)
            orphan(pending)
            pending = None
            continue
        if POINTS_ONLY_RE.match(ln) and not GRADE_ONLY_RE.match(ln) or \
                (POINTS_ONLY_RE.match(ln) and (pending is None or pending.entries)):
            orphan(pending)
            pending = Block(None, "", to_float(ln), i)
            c.current = pending
            c.current_is_youth = False
            continue
        m = BOUT_LINE_RE.match(ln)
        if m and (m.group("grade") or m.group("ph")) and len(GRADE_TOKEN_RE.findall(ln)) <= 1:
            g = m.group("grade") or m.group("ph")
            entry = Entry(_sym(m.group("sym")), m.group("name").strip(), to_float(g), i,
                          placeholder=bool(m.group("ph")))
            if pending is None:
                c.suspicious(i, ln, "entry_without_athlete")
            elif c.section != "youth":
                pending.entries.append(entry)
            else:
                c.sheet.youth_lines.add(i)
            continue
        nm = NB_NAME_RE.match(ln)
        if nm and pending is not None and not pending.name_raw:
            pending.rank = nm.group("rank").replace(" ", "")
            pending.name_raw = nm.group("name").strip()
            pending.mark = (nm.group("mark") or "").strip()
            if c.section == "youth":
                c.sheet.youth_blocks += 1
                c.sheet.youth_names.add(name_keys(clean_name(pending.name_raw))[1])
            else:
                c.sheet.blocks.append(pending)
            pending = Block(None, "", to_float(nm.group("next")), i) if nm.group("next") else None
            c.current = pending
            continue
        if not c.sheet.blocks and pending is None:
            c.other(i, ln)
        else:
            c.unparsed(i, ln)
    orphan(pending)
    return c.finish()


PARSERS = {"standard": parse_standard, "notenblatt": parse_notenblatt, "blocks": parse_blocks, "rang": parse_rang,
           "multicol": parse_multicol}


def parse_sheet(text: str, fest_year: int | None = None) -> Sheet:
    lines = normalize_text(text)
    layout = detect_layout(lines)
    if layout in PARSERS:
        sheet = PARSERS[layout](lines, fest_year)
    else:
        sheet = Sheet(layout=layout, header=[ln for ln in lines if ln][:8], blocks=[])
    sheet.glyph_ids = glyph_id_encoding(text)
    return sheet


# ----------------------------------------------------------------------------- header check
_MONTHS = {
    "januar": 1, "janvier": 1, "februar": 2, "février": 2, "fevrier": 2, "märz": 3, "maerz": 3,
    "mars": 3, "april": 4, "avril": 4, "mai": 5, "juni": 6, "juin": 6, "juli": 7,
    "juillet": 7, "august": 8, "août": 8, "aout": 8, "september": 9, "septembre": 9,
    "oktober": 10, "octobre": 10, "november": 11, "novembre": 11, "dezember": 12,
    "décembre": 12, "decembre": 12,
}
_NUM_DATE_RE = re.compile(r"(\d{1,2})\.(?:\s*-\s*(\d{1,2})\.)?\s*(\d{1,2})\.(\d{4}|\d{2})\b")
_WORD_DATE_RE = re.compile(r"(\d{1,2})\.?\s*(?:-\s*\d{1,2}\.?\s*)?([A-Za-zäéû]+)\s+(\d{4})")


# a date followed by a clock time is the print timestamp ("01.01.2000 - 01:41",
# "…Schwingerverband 10.04.2022 17:10 Seite 1/2"), not the festival date
_PRINT_TIME_RE = re.compile(r"\s*-?\s*\d{1,2}:\d{2}")
# sheets are sometimes printed / dated a few days off (Bolligen 2019: 15.04 vs 13.04)
HEADER_DATE_TOLERANCE_DAYS = 3


def header_dates(lines: Iterable[str]) -> list[_dt.date]:
    """Festival dates printed in the sheet header (print timestamps ignored)."""
    out: list[_dt.date] = []
    for ln in lines:
        for m in _NUM_DATE_RE.finditer(ln):
            if _PRINT_TIME_RE.match(ln, m.end()):
                continue
            y = int(m.group(4))
            y = y + 2000 if y < 100 else y
            for d in filter(None, (m.group(1), m.group(2))):
                try:
                    out.append(_dt.date(y, int(m.group(3)), int(d)))
                except ValueError:
                    pass
        for m in _WORD_DATE_RE.finditer(ln):
            mon = _MONTHS.get(m.group(2).casefold())
            if mon:
                try:
                    out.append(_dt.date(int(m.group(3)), mon, int(m.group(1))))
                except ValueError:
                    pass
    return out


_GENERIC_WORDS = {"schwinget", "schwingfest", "schwingertag", "kantonalschwingfest", "statistische",
                  "tabelle", "regional", "regionalfest", "frühjahrsschwinget", "herbstschwinget",
                  "hallenschwinget", "abendschwinget", "rangschwinget", "schwing", "älplerfest"}


_PRINT_DATE_RE = re.compile(r"\bDatum:\s*\d{1,2}\.\d{1,2}\.\d{2,4}")


def verify_header(header: list[str], fest_date: str, fest_name: str, *,
                  hand_set: bool = False) -> tuple[str, str]:
    """('ok' | 'mismatch' | 'unverified', detail): does the sheet belong to this festival?

    ``hand_set`` (sheets before 2011, typed by hand from a template): "Datum: 12.05.2005"
    is the day the table was made, and a date with the festival's day and month but
    another year ("26. Juni 2002" on the 2005 sheet, "15 juillet 2207") is a misprint
    when the header also names the festival."""
    target = _dt.date.fromisoformat(fest_date)
    if hand_set:
        header = [_PRINT_DATE_RE.sub(" ", ln) for ln in header]
    dates = header_dates(header)
    words = {w.casefold() for w in re.findall(r"[A-Za-zÀ-ÿ]{4,}", fest_name)} - _GENERIC_WORDS
    head = " ".join(header).casefold()
    if dates:
        if any(abs((d - target).days) <= HEADER_DATE_TOLERANCE_DAYS for d in dates):
            return "ok", "date"
        if hand_set and any((d.month, d.day) == (target.month, target.day) for d in dates) \
                and any(w in head for w in words):
            return "ok", "date (year misprinted) + name"
        return "mismatch", f"sheet dates {sorted({d.isoformat() for d in dates})} != {fest_date}"
    if words and any(w in head for w in words):
        return "ok", "name"
    return "unverified", "no date or name token in sheet header"


# ----------------------------------------------------------------------------- bouts
def _outcome(sa: str, sb: str, ga: float, gb: float) -> tuple[str | None, list[str]]:
    pair = (sa, sb)
    if pair == ("+", "o"):
        return "WIN_A", []
    if pair == ("o", "+"):
        return "WIN_B", []
    if pair == ("-", "-"):
        return "DRAW", []
    # symbols disagree: fall back to grades when they are unambiguous
    if ga >= 9.25 and gb <= 8.75:
        return "WIN_A", ["symbol_conflict_resolved_by_grades"]
    if gb >= 9.25 and ga <= 8.75:
        return "WIN_B", ["symbol_conflict_resolved_by_grades"]
    return None, []


def _grade_ok(g: float | None) -> bool:
    return g is not None and GRADE_MIN <= g <= GRADE_MAX and (g * 4) == int(g * 4)


def _soft_flags(outcome: str, ga: float, gb: float) -> list[str]:
    win, lose = (ga, gb) if outcome == "WIN_A" else (gb, ga)
    if outcome == "DRAW":
        return [] if 8.5 <= ga <= 9.0 and 8.5 <= gb <= 9.0 else ["unusual_draw_grades"]
    return [] if win >= 9.25 and lose <= 8.75 else ["unusual_win_grades"]


def festival_gang_count(blocks: list[Block], max_gang: int) -> int:
    """The festival's real number of Gänge, derived from the sheet.

    The largest L such that at least 10 % of the athletes (min. 2) have L or more
    regularly graded entries, clamped to ``max_gang`` (8 at the ESAF, else 6).
    Placeholder / no-grade / 'z' entries are ignored, so an extra bout
    (Zusatzgang) never raises the count; 5-Gang festivals are recognised.
    Falls back to ``max_gang`` when the sheet gives no evidence."""
    lens = [sum(1 for e in b.entries if not (e.placeholder or e.missing_grade or e.extra))
            for b in blocks]
    need = max(2.0, 0.1 * len(lens))
    best = 0
    for n in range(1, max_gang + 1):
        if sum(1 for x in lens if x >= n) >= need:
            best = n
    return best or max_gang


def _assign_gaenge(pending: list[dict[str, object]], complete: list[bool]) -> None:
    """Gang numbers from list positions (sets ``gang_nr`` and flags).

    Athletes who miss a Gang have shorter lists, so a position is only a lower
    bound for the real Gang: the position from a *complete* list (at least as
    many regular entries as the festival has Gänge, see
    :func:`festival_gang_count`) is trusted, otherwise the larger position wins
    (``gang_inferred``). Complete lists occasionally disagree (entries not
    strictly in Gang order on the sheet) -> ``gang_uncertain``. An extra bout
    takes the position from the opponent's list (its own entry is surplus).
    Gang numbers only order bouts within a festival.
    """
    for p in pending:
        pa, pb = p["pos_a"], p["pos_b"]
        flags: list[str] = p["flags"]  # type: ignore[assignment]
        if pa is None or pb is None:  # extra bout: the surplus side has no position
            other = pb if pa is None else pa
            n = int(p["gang_count"])  # type: ignore[call-overload]
            p["gang_nr"] = n if other is None else min(int(other), n)  # type: ignore[call-overload]
            if other is None or int(other) > n:  # type: ignore[call-overload]
                flags.append(f"gang_inferred:{other}/{n}")
            continue
        pa, pb = int(pa), int(pb)  # type: ignore[call-overload]
        if pa == pb:
            p["gang_nr"] = pa
            continue
        a_full = complete[int(p["a"])]  # type: ignore[call-overload]
        b_full = complete[int(p["b"])]  # type: ignore[call-overload]
        if a_full and b_full:
            p["gang_nr"] = max(pa, pb)
            flags.append(f"gang_uncertain:{pa}/{pb}")
        elif a_full or b_full:
            p["gang_nr"] = pa if a_full else pb
            flags.append(f"gang_inferred:{pa}/{pb}")
        else:
            p["gang_nr"] = max(pa, pb)
            flags.append(f"gang_inferred:{pa}/{pb}")


def _special(e: Entry) -> bool:
    """Entry without a real grade: placeholder 0.00/0.25, no grade, or a 'z' line."""
    return e.placeholder or e.missing_grade or e.extra


_COMPLEMENTARY = {("+", "o"): "WIN_A", ("o", "+"): "WIN_B", ("-", "-"): "DRAW"}


def _is_one_sided_schlussgang(e: Entry, pos: int | None, owner: Block, opp: Block,
                              owner_last: int, gang_count: int) -> bool:
    """Schlussgang whose loser's line the sheet omits entirely (user decision
    2026-10-01): a graded win of the festival winner (rank 1) in his final Gang,
    against an athlete whose own list is complete but never mentions him. The
    caller also requires that the opponent resolved to exactly one athlete."""
    rank = re.match(r"\d*", owner.rank or "")
    return (e.sym == "+" and not _special(e) and not e.forfeit and _grade_ok(e.grade)
            and pos is not None and pos == owner_last == gang_count
            and rank is not None and rank.group(0) == "1"
            and len(opp.entries) >= gang_count)


def merge_interim_sheet(sheet: Sheet, interim: Sheet) -> int:
    """Add athletes that only an interim sheet lists ("Statistik nach 4 Gängen":
    athletes eliminated before the final sheet's cut). Athletes already on the
    final sheet are skipped, so every bout is still built from its two entries
    exactly once. Returns the number of added blocks."""
    known = {name_keys(clean_name(b.name_raw))[1] for b in sheet.blocks}
    added = 0
    for b in interim.blocks:
        if name_keys(clean_name(b.name_raw))[1] in known:
            continue
        b.rank, b.interim = None, True
        sheet.blocks.append(b)
        added += 1
    return added


def squash_name(base: str) -> str:
    """Name key that ignores case, accents, hyphens, dots and spaces
    ("Pellet Hans-Peter" = "Pellet Hanspeter")."""
    s = unicodedata.normalize("NFKD", base.casefold())
    return "".join(ch for ch in s if ch.isalnum())


def _edit_distance_le(a: str, b: str, limit: int) -> bool:
    """Levenshtein distance <= ``limit`` (small strings, early exit)."""
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


def similar_names(a: str, b: str, max_edit: int = 2) -> bool:
    """Two printed spellings that may be one athlete on a hand-set sheet: equal apart
    from hyphens / spaces / accents, the same words in another order ("Edi Philipp"),
    one cut off ("Pellet Hans-Pete"), or one or two letters apart ("Habheer" /
    "Halbheer"). Only ever used together with a second piece of evidence."""
    sa, sb = squash_name(a), squash_name(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    if sorted(a.casefold().replace("-", " ").split()) == sorted(b.casefold().replace("-", " ").split()):
        return True
    short, long_ = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(short) >= 8 and long_.startswith(short):
        return True
    return len(short) >= 8 and _edit_distance_le(sa, sb, max_edit)


# One-sided bouts against athletes a sheet does not print (old sheets, see
# ``build_festival(unlisted=True)``): the sign must fit the printed grade.
_UNLISTED_GRADES = {"+": (9.5, 10.0), "-": (8.5, 9.25), "o": (8.25, 9.0)}
UNLISTED_FLAG = "unlisted"


def build_festival(sheet: Sheet, fest_id: int, *, max_gang: int = 6,
                   unlisted: bool = False) -> FestivalParse:
    """Pair entries into bouts; everything that cannot be paired becomes a Reject.

    ``unlisted`` (sheets before 2011, which print only the first ranks or only the
    athletes who wrestled every Gang): an opponent the sheet has no block for becomes a
    name-only athlete (flag ``unlisted``) and the entry a bout seen from one side
    (flags ``one_sided,unlisted_opponent``: outcome from the sign, the opponent's grade
    unknown). Hand-set spellings are tolerated where the bout is printed from both
    sides (:func:`similar_names` plus the mirror entry)."""
    res = FestivalParse(fest_id=fest_id, layout=sheet.layout, status="ok",
                        youth_blocks=sheet.youth_blocks)
    rej = res.rejects.append
    for line, text, reason in sheet.suspicious:
        rej(Reject(fest_id, "line", reason, text, line))
    if sheet.glyph_ids:  # informational, like duplicate_name_in_sheet
        rej(Reject(fest_id, "athlete", f"glyph_ids_decoded_{sheet.glyph_ids}",
                   "text layer decoded from TrueType glyph ids" +
                   ("; non-ASCII letters unknown ('?') - names need fuzzy matching"
                    if sheet.glyph_ids == "lossy" else "")))
    if sheet.layout in ("empty", "garbled"):
        rej(Reject(fest_id, "festival", f"layout_{sheet.layout}", "no usable text layer"))
        res.status = "failed"
        return res
    if sheet.has_active_section is False:
        rej(Reject(fest_id, "festival", "no_active_section",
                   f"youth sections only: {sheet.youth_sections[:5]}"))
        res.status = "failed"
        return res
    blocks = sheet.blocks
    if not blocks:
        rej(Reject(fest_id, "festival", "no_athletes_found", f"layout {sheet.layout}"))
        res.status = "failed"
        return res

    # athletes ------------------------------------------------------------------------
    ids: list[str] = []
    by_full: dict[str, list[int]] = defaultdict(list)
    by_base: dict[str, list[int]] = defaultdict(list)
    for idx, b in enumerate(blocks):
        name = clean_name(b.name_raw)
        full, base = name_keys(name)
        aid = f"{fest_id}-{idx:03d}"
        ids.append(aid)
        by_full[full].append(idx)
        by_base[base].append(idx)
        details = name_details(name)
        grades = [e.grade for e in b.entries if e.grade is not None and not e.extra]
        res.athletes.append({
            "athlete_raw_id": aid, "fest_id": fest_id, "idx": idx, "rank": b.rank,
            "name_raw": b.name_raw, "name": name, "name_key": full, "name_base_key": base,
            "status": status_of(b.name_raw), "mark": b.mark or None,
            "sennen_turner": _st_marker(b.name_raw),
            "withdrawn": b.mark == "°", "points": b.points, "points_mismatch": False,
            "flags": "interim_sheet" if b.interim else "",
            "n_entries": len(b.entries), "grade_sum": round(sum(grades), 2) if grades else 0.0,
            **details,
        })
        if res.athletes[-1]["association"] is None:
            res.athletes[-1]["association"] = code_of(b.name_raw)
        if b.place:
            res.athletes[-1]["place"] = b.place
        if b.association:
            res.athletes[-1]["association"] = b.association
    for key, idxs in by_full.items():
        if len(idxs) > 1:
            rej(Reject(fest_id, "athlete", "duplicate_name_in_sheet",
                       f"{key!r} printed {len(idxs)}x (entries against it are ambiguous)"))

    base_of = [name_keys(clean_name(b.name_raw))[1] for b in blocks]
    opp_bases = [{name_keys(clean_name(e.opponent))[1] for e in b.entries} for b in blocks]

    def resolve(opp: str, me: int) -> tuple[int | None, bool]:
        """(athlete index, was_ambiguous). Same-name athletes are told apart by
        which of them lists ``me`` as an opponent (mirror entry)."""
        full, base = name_keys(clean_name(opp))
        cands = by_full.get(full) or ([] if by_full.get(full) else by_base.get(base, []))
        if len(cands) == 1:
            return cands[0], False
        if len(cands) > 1:
            mirrored = [c for c in cands if base_of[me] in opp_bases[c]]
            if len(mirrored) == 1:
                return mirrored[0], True
        if not cands and unlisted:
            # another spelling of a printed athlete who lists ``me`` in turn
            near = [c for c in range(len(blocks)) if c != me and similar_names(base, base_of[c])
                    and any(similar_names(base_of[me], ob) for ob in opp_bases[c])]
            if len(near) == 1:
                return near[0], True
        return None, len(cands) > 1

    # athletes the sheet does not print (``unlisted``): squashed name -> athlete index
    phantom_idx: dict[str, int] = {}
    phantom_names: list[str] = []
    phantom_bouts: Counter[tuple[int, int]] = Counter()

    def phantom(opp: str) -> int:
        name = clean_name(opp)
        key = squash_name(name_keys(name)[1])
        if key not in phantom_idx:
            phantom_idx[key] = len(blocks) + len(phantom_names)
            phantom_names.append(opp)
        return phantom_idx[key]

    # extra bouts (Zusatzgang): with an odd field one athlete fights one bout more
    # than the festival has Gänge. His line for it shows 0.00 / 0.25 / no grade
    # ('z' on youth sheets); the opponent's line is normal. A special entry is an
    # extra-bout candidate if it is a 'z' line or its athlete has more entries
    # than Gänge; special entries in a regular Gang are forfeits / injuries.
    gang_count = res.gang_count = festival_gang_count(blocks, max_gang)
    # more than one surplus entry: blocks merged by a wrapped name / page footer, the
    # list is not one athlete's -> no extra-bout logic, athlete flagged
    overflow = [len(b.entries) > gang_count + 1 for b in blocks]
    for idx, over in enumerate(overflow):
        if over:
            fl = res.athletes[idx]["flags"]
            res.athletes[idx]["flags"] = ",".join(filter(None, [str(fl), "entries_overflow"]))
    surplus = [len(b.entries) > gang_count and not overflow[i] for i, b in enumerate(blocks)]
    last_pos = [sum(1 for e in b.entries if not (_special(e) and (e.extra or surplus[i])))
                for i, b in enumerate(blocks)]

    # one-sided bouts (``unlisted``) are only taken from a block that proves itself: no
    # surplus entries and a printed total that is the sum of its grades (a header the
    # table reader missed leaves the next athlete's Gänge in the block above)
    sound = [not overflow[i] and (b.points is None or abs(
        b.points - sum(e.grade for e in b.entries if e.grade is not None and not e.extra)) <= 0.001)
        for i, b in enumerate(blocks)]

    def extra_cand(e: Entry, who: int) -> bool:
        return _special(e) and (e.extra or surplus[who])

    # entries -> (a, b) occurrences -------------------------------------------------------
    occ: dict[tuple[int, int], list[tuple[int | None, Entry, int, str]]] = defaultdict(list)
    pending_unlisted: list[dict[str, object]] = []
    exact: set[int] = set()  # id() of entries whose opponent resolved to exactly one athlete
    for idx, b in enumerate(blocks):
        pos = 0
        for e in b.entries:
            res.entries_total += 1
            if extra_cand(e, idx):
                epos: int | None = None   # surplus entry: Gang comes from the opponent
                label = "extra"
            else:
                pos += 1
                epos = e.gang or pos
                label = f"G{epos}"
            opp, ambiguous_name = resolve(e.opponent, idx)
            if opp is None and unlisted and not ambiguous_name:
                obase = name_keys(clean_name(e.opponent))[1]
                lo, hi = _UNLISTED_GRADES[e.sym]
                detail = f"{b.name_raw} {label}: {e.sym} {e.opponent} {e.grade}"
                if any(similar_names(obase, base_of[c], 1) for c in range(len(blocks))
                       if c != idx):
                    # a printed athlete under another spelling who does not list this bout
                    rej(Reject(fest_id, "entry", "unmatched_entry",
                               detail + " (similar printed name, no mirror entry)", e.line))
                elif squash_name(obase) == squash_name(base_of[idx]):
                    rej(Reject(fest_id, "entry", "self_bout", detail, e.line))
                elif epos is None or _special(e) or e.forfeit or len(squash_name(obase)) < 4:
                    rej(Reject(fest_id, "entry", "opponent_not_found", detail, e.line))
                elif not sound[idx]:
                    rej(Reject(fest_id, "entry", "block_unsound",
                               detail + " (total is not the sum of the grades, or surplus "
                               "entries; no mirror entry)", e.line))
                elif not (_grade_ok(e.grade) and lo <= float(e.grade or 0) <= hi):
                    rej(Reject(fest_id, "entry", "inconsistent_outcome",
                               detail + " (sign and grade disagree, no mirror entry)", e.line))
                else:
                    ph = phantom(e.opponent)
                    k = phantom_bouts[(idx, ph)]
                    phantom_bouts[(idx, ph)] += 1
                    pending_unlisted.append({
                        "fest_id": fest_id, "a": idx, "b": ph, "pos_a": epos, "pos_b": epos,
                        "k": k, "athlete_a_id": ids[idx], "athlete_b_id": f"{fest_id}-{ph:03d}",
                        "gang_count": gang_count,
                        "outcome": {"+": "WIN_A", "o": "WIN_B", "-": "DRAW"}[e.sym],
                        "grade_a": e.grade, "grade_b": None, "schlussgang": e.schlussgang,
                        "flags": ["one_sided", "unlisted_opponent"], "line": e.line,
                    })
                continue
            if opp is None:
                in_youth = name_keys(clean_name(e.opponent))[1] in sheet.youth_names
                reason = ("ambiguous_opponent" if ambiguous_name else
                          "opponent_in_youth_section" if in_youth else "opponent_not_found")
                rej(Reject(fest_id, "entry", reason,
                           f"{b.name_raw} {label}: {e.sym} {e.opponent} {e.grade}", e.line))
                continue
            if opp == idx:
                rej(Reject(fest_id, "entry", "self_bout",
                           f"{b.name_raw} {label}: {e.opponent}", e.line))
                continue
            if not ambiguous_name:
                exact.add(id(e))
            occ[(idx, opp)].append((epos, e, idx, label))

    seen: set[tuple[int, int]] = set()
    pending: list[dict[str, object]] = []
    for (i, j), mine in sorted(occ.items()):
        if (i, j) in seen:
            continue
        seen.add((i, j))
        seen.add((j, i))
        theirs = occ.get((j, i), [])
        a_side, b_side = (mine, theirs) if i < j else (theirs, mine)
        a, b = min(i, j), max(i, j)
        for k in range(max(len(a_side), len(b_side))):
            if k >= len(a_side) or k >= len(b_side):
                epos, e, who, label = a_side[k] if k < len(a_side) else b_side[k]
                other = b if who == a else a
                if (not (b_side if who == a else a_side)          # loser lists no line at all
                        and _is_one_sided_schlussgang(e, epos, blocks[who], blocks[other],
                                                      last_pos[who], gang_count)
                        and id(e) in exact):
                    win_a = who == a
                    pending.append({
                        "fest_id": fest_id, "a": a, "b": b, "pos_a": epos, "pos_b": epos,
                        "k": k, "athlete_a_id": ids[a], "athlete_b_id": ids[b],
                        "gang_count": gang_count, "outcome": "WIN_A" if win_a else "WIN_B",
                        "grade_a": e.grade if win_a else None,
                        "grade_b": None if win_a else e.grade,
                        "schlussgang": e.schlussgang, "flags": ["one_sided"], "line": e.line,
                    })
                    continue
                rej(Reject(fest_id, "entry", "unmatched_entry",
                           f"{blocks[who].name_raw} {label}: {e.sym} {e.opponent} {e.grade} "
                           f"(no mirror entry)", e.line))
                continue
            (ga_n, ea, _, la), (gb_n, eb, _, lb) = a_side[k], b_side[k]
            desc = (f"{blocks[a].name_raw} {la} {ea.sym}{ea.grade} vs "
                    f"{blocks[b].name_raw} {lb} {eb.sym}{eb.grade}")
            flags: list[str] = []
            grade_a: float | None = ea.grade
            grade_b: float | None = eb.grade
            if ea.forfeit or eb.forfeit:
                rej(Reject(fest_id, "bout", "forfeit_injury", desc, ea.line))
                continue
            if any(_special(e) and overflow[who] for e, who in ((ea, a), (eb, b))):
                rej(Reject(fest_id, "bout", "entries_overflow", desc, ea.line))
                continue
            if _special(ea) or _special(eb):
                regular_special = [e for e, who in ((ea, a), (eb, b))
                                   if _special(e) and not extra_cand(e, who)]
                if any(e.placeholder or e.extra for e in regular_special):
                    # 0.00 / 0.25 in a regular Gang: not fought / injury
                    rej(Reject(fest_id, "bout", "forfeit_injury", desc, ea.line))
                    continue
                outcome = _COMPLEMENTARY.get((ea.sym, eb.sym))
                others_ok = all(_grade_ok(e.grade) for e in (ea, eb) if not _special(e))
                if outcome is None or not others_ok:
                    rej(Reject(fest_id, "bout", "inconsistent_outcome" if outcome is None
                               else "grade_out_of_range", desc, ea.line))
                    continue
                if regular_special:  # no grade printed in a regular Gang
                    flags.append("grade_missing")
                if ga_n is None or gb_n is None:
                    flags.append("extra_bout")
                grade_a = None if _special(ea) else ea.grade
                grade_b = None if _special(eb) else eb.grade
            else:
                if not (_grade_ok(ea.grade) and _grade_ok(eb.grade)):
                    rej(Reject(fest_id, "bout", "grade_out_of_range", desc, ea.line))
                    continue
                assert ea.grade is not None and eb.grade is not None
                outcome, oflags = _outcome(ea.sym, eb.sym, ea.grade, eb.grade)
                if outcome is None:
                    rej(Reject(fest_id, "bout", "inconsistent_outcome", desc, ea.line))
                    continue
                flags += oflags + _soft_flags(outcome, ea.grade, eb.grade)
                # a normally graded surplus bout beyond the Gang count (one side
                # lists it after its regular Gänge): extra bout with both grades
                for side, mine_pos, other_pos, who in (("a", ga_n, gb_n, a), ("b", gb_n, ga_n, b)):
                    if (surplus[who] and mine_pos is not None and other_pos is not None
                            and mine_pos > gang_count >= other_pos and ea.gang is None):
                        flags.append("extra_bout")
                        if side == "a":
                            ga_n = None
                        else:
                            gb_n = None
                        break
            if ea.gang is not None and eb.gang is not None and ea.gang != eb.gang:
                flags.append(f"gang_mismatch:{ea.gang}/{eb.gang}")
            if ea.ambiguous or eb.ambiguous:
                flags.append("column_ambiguous")
            pending.append({
                "fest_id": fest_id, "a": a, "b": b, "pos_a": ga_n, "pos_b": gb_n, "k": k,
                "athlete_a_id": ids[a], "athlete_b_id": ids[b], "gang_count": gang_count,
                "outcome": outcome, "grade_a": grade_a, "grade_b": grade_b,
                "schlussgang": bool(ea.schlussgang or eb.schlussgang),
                "flags": flags, "line": ea.line,
            })
    for ph, raw in enumerate(phantom_names, start=len(blocks)):
        name = clean_name(raw)
        full, base = name_keys(name)
        res.athletes.append({
            "athlete_raw_id": f"{fest_id}-{ph:03d}", "fest_id": fest_id, "idx": ph, "rank": None,
            "name_raw": raw, "name": name, "name_key": full, "name_base_key": base,
            "status": status_of(raw), "mark": None, "sennen_turner": _st_marker(raw),
            "withdrawn": False, "points": None, "points_mismatch": False,
            "flags": UNLISTED_FLAG, "n_entries": 0, "grade_sum": 0.0, **name_details(name),
        })
        if res.athletes[-1]["association"] is None:
            res.athletes[-1]["association"] = code_of(raw)
    pending.extend(pending_unlisted)
    names_of = [b.name_raw for b in blocks] + phantom_names
    _assign_gaenge(pending, [len(b.entries) >= gang_count for b in blocks])
    # Schlussgang: only known where the sheet marks it ('s+' / 's-' / 'so' entries in
    # block layouts); everywhere else it is unknown (None), never a misleading 0
    marked = any(e.schlussgang for b in blocks for e in b.entries)
    for p in pending:
        gang = int(p["gang_nr"])  # type: ignore[call-overload]
        if gang > max_gang:
            rej(Reject(fest_id, "bout", "gang_out_of_range",
                       f"G{gang} > {max_gang}: {names_of[p['a']]} vs "
                       f"{names_of[p['b']]}", p["line"]))
            continue
        res.bouts.append({
            "bout_id": f"{fest_id}-{gang}-{p['a']:03d}-{p['b']:03d}-{p['k']}",
            "fest_id": fest_id, "gang_nr": gang,
            "athlete_a_id": p["athlete_a_id"], "athlete_b_id": p["athlete_b_id"],
            "outcome": p["outcome"], "grade_a": p["grade_a"], "grade_b": p["grade_b"],
            "schlussgang": p["schlussgang"] if marked else None, "flags": ",".join(p["flags"]),
        })
    if marked and not any(b["schlussgang"] for b in res.bouts):
        for bt in res.bouts:  # marker on the sheet, but the marked bout was not imported
            bt["schlussgang"] = None
    n_entry_rejects = sum(1 for r in res.rejects if r.stage in ("entry", "bout"))
    if not res.bouts:
        res.status = "failed"
        rej(Reject(fest_id, "festival", "no_bouts", f"layout {sheet.layout}"))
    elif n_entry_rejects or any(r.stage == "line" for r in res.rejects):
        res.status = "partial"
    return res


def max_gaenge(category: str | None, eidg_type: str | None) -> int:
    """Gänge per festival: 8 at the ESAF itself, otherwise 6 (spec §4.1)."""
    return 8 if category == "ESAF" and eidg_type == "ESAF" else 6


def paired_entries(res: FestivalParse) -> int:
    """Sheet entries that ended up in bouts (a ``one_sided`` bout uses one entry)."""
    return 2 * len(res.bouts) - sum("one_sided" in str(b["flags"]).split(",") for b in res.bouts)


# ``unlisted`` sheets: at least this share of the printed athletes must have a total
# that is the sum of their grades, else the columns were not read correctly.
MIN_CONSISTENT_BLOCKS = 0.5


def consistent_blocks(res: FestivalParse) -> tuple[int, int]:
    """(printed athletes whose total equals the sum of their grades, printed athletes
    with a total and entries) - the structural check of a table read by position."""
    listed = [a for a in res.athletes if UNLISTED_FLAG not in str(a["flags"]).split(",")
              and a["points"] is not None and a["n_entries"]]
    good = sum(1 for a in listed
               if abs(float(a["points"]) - float(a["grade_sum"])) <= 0.001)  # type: ignore[arg-type]
    return good, len(listed)


def validate_festival(res: FestivalParse, *, min_pair_rate: float = 0.5,
                      unlisted: bool = False) -> FestivalParse:
    """Festival-level validation on top of the pairing checks (in place).

    * pair rate below ``min_pair_rate`` -> structurally unreliable sheet: all
      bouts are withdrawn (``low_pair_rate``) and the festival fails;
    * an athlete with two bouts in the same Gang -> ``gang_collision`` flag;
    * printed points != sum of grades -> athlete flag ``points_mismatch``;
    * grades / outcomes / Gang range are enforced in :func:`build_festival`.
    """
    if not res.entries_total or not res.bouts:
        return res
    if unlisted:
        # a one-sided bout has no mirror entry to contradict a misread column, so the
        # sheet must prove its structure: totals = sums of the grades
        good, listed = consistent_blocks(res)
        if listed and good / listed < MIN_CONSISTENT_BLOCKS:
            res.rejects.append(Reject(res.fest_id, "festival", "structure_unreliable",
                                      f"total = sum of grades for {good} of {listed} printed "
                                      f"athletes; {len(res.bouts)} bouts not imported"))
            res.bouts = []
            res.status = "failed"
            return res
    rate = paired_entries(res) / res.entries_total
    if rate < min_pair_rate:
        res.rejects.append(Reject(res.fest_id, "festival", "low_pair_rate",
                                  f"{rate:.0%} of {res.entries_total} entries paired; "
                                  f"{len(res.bouts)} bouts not imported"))
        res.bouts = []
        res.status = "failed"
        return res
    # extra bouts share a Gang with a regular bout of the surplus athlete by
    # construction; they are left out so the flag keeps meaning "ordering conflict"
    regular = [b for b in res.bouts if "extra_bout" not in str(b["flags"]).split(",")]
    slots: Counter[tuple[object, object]] = Counter()
    for b in regular:
        slots[(b["athlete_a_id"], b["gang_nr"])] += 1
        slots[(b["athlete_b_id"], b["gang_nr"])] += 1
    for b in regular:
        if slots[(b["athlete_a_id"], b["gang_nr"])] > 1 or slots[(b["athlete_b_id"], b["gang_nr"])] > 1:
            b["flags"] = ",".join(filter(None, [b["flags"], "gang_collision"]))
    for a in res.athletes:
        pts, got = a["points"], a["grade_sum"]
        a["points_mismatch"] = bool(pts is not None and a["n_entries"]
                                    and abs(float(pts) - float(got)) > 0.001)  # type: ignore[arg-type]
    return res


def parse_festival(text: str, fest_id: int, fest_date: str, fest_name: str, *,
                   max_gang: int = 6, min_pair_rate: float = 0.5,
                   interim_text: str | None = None, unlisted: bool = False) -> FestivalParse:
    """Full pipeline for one sheet: layout -> blocks -> header check -> bouts -> validation.

    ``interim_text``: an interim statistic sheet of the same festival whose extra
    athletes are merged in (see :func:`merge_interim_sheet`). ``unlisted``: see
    :func:`build_festival`."""
    return festival_from_sheet(parse_sheet(text, int(fest_date[:4])), fest_id, fest_date,
                               fest_name, max_gang=max_gang, min_pair_rate=min_pair_rate,
                               interim_text=interim_text, unlisted=unlisted)


def festival_from_sheet(sheet: Sheet, fest_id: int, fest_date: str, fest_name: str, *,
                        max_gang: int = 6, min_pair_rate: float = 0.5,
                        interim_text: str | None = None,
                        unlisted: bool = False) -> FestivalParse:
    """Header check -> bouts -> validation for an already read sheet."""
    check, detail = verify_header(sheet.header, fest_date, fest_name, hand_set=unlisted)
    if check == "mismatch":
        res = FestivalParse(fest_id=fest_id, layout=sheet.layout, status="header_mismatch",
                            header_check=check)
        res.rejects.append(Reject(fest_id, "festival", "header_mismatch", detail))
        return res
    interim_note: Reject | None = None
    if interim_text is not None:
        interim = parse_sheet(interim_text, int(fest_date[:4]))
        i_check, i_detail = verify_header(interim.header, fest_date, fest_name)
        if i_check == "mismatch":
            interim_note = Reject(fest_id, "festival", "interim_sheet_header_mismatch", i_detail)
        else:
            n = merge_interim_sheet(sheet, interim)
            interim_note = Reject(fest_id, "athlete", "interim_sheet_merged",
                                  f"{n} athletes added from the interim sheet")
    res = build_festival(sheet, fest_id, max_gang=max_gang, unlisted=unlisted)
    if interim_note:
        res.rejects.append(interim_note)
    res.header_check = check if check == "ok" else f"{check}: {detail}"
    return validate_festival(res, min_pair_rate=min_pair_rate, unlisted=unlisted)


# ----------------------------------------------------------------------------- frames
BOUT_COLUMNS = ["bout_id", "fest_id", "gang_nr", "athlete_a_id", "athlete_b_id", "outcome",
                "grade_a", "grade_b", "schlussgang", "flags"]
ATHLETE_COLUMNS = ["athlete_raw_id", "fest_id", "idx", "rank", "name_raw", "name", "name_key",
                   "name_base_key", "status", "mark", "sennen_turner", "withdrawn", "points",
                   "points_mismatch", "n_entries",
                   "grade_sum", "birth_year", "association", "place", "flags"]


def to_frames(parses: Iterable[FestivalParse]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(bouts, athletes_raw, rejects) DataFrames (spec Bout / raw Athlete schemas)."""
    parses = list(parses)
    bouts = pd.DataFrame([b for p in parses for b in p.bouts], columns=BOUT_COLUMNS)
    athletes = pd.DataFrame([a for p in parses for a in p.athletes], columns=ATHLETE_COLUMNS)
    rejects = pd.DataFrame([r.__dict__ for p in parses for r in p.rejects],
                           columns=["fest_id", "stage", "reason", "detail", "line"])
    return bouts, athletes, rejects


def summarize(parses: Iterable[FestivalParse]) -> Counter[str]:
    return Counter(p.status for p in parses)
