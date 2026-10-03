"""Parse "Schlussrangliste" PDFs into ranking entries (Phase 3 identity evidence).

One entry per athlete: rank, points, result string (one symbol per Gang:
``+`` win, ``-`` gestellt, ``o`` loss), name, Sennen/Turner marker, Kranz
stars, residence, cantonal association code, Schwingklub and status.

Layouts (``RankingParse.layout``):

* ``esv``     — ESV standard list with a header line
  ``Rang Punkte Resultat Name Vorname Wohnort Schwingklub Status`` (≈2015+).
  Columns are assigned by the header's x positions; an unlabelled code column
  (``LU``, ``ONW``, ``BO`` …) sits just left of the Schwingklub column.
* ``header``  — other lists with a header line (e.g. ``Rang Name Wohnort
  Verband Klub Total Resultate``).
* ``plain``   — no header (most lists before 2015): cells separated by wide
  gaps are classified (name, residence, association code, ``(nr)`` club numbers).

Rows that look like data (rank / points / result tokens) but cannot be parsed
become :class:`Reject` rows; nothing is dropped silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.scraper.pdf_layout import Line, Word, pdf_lines, split_cells

# v2: same-row fragments merged, surname/first-name columns, more status phrases
RANKING_PARSER_VERSION = 2

CELL_GAP = 5.0
_RESULT_RE = re.compile(r"^[+\-oO0]+$")
_LETTER_RESULT_RE = re.compile(r"^(E|K|EK|N|TK|S|U)([+\-oO0]{3,})$")
_NAME_STARS_REST_RE = re.compile(r"^(.*?[a-zà-ÿ])\s*,?\s*([ST]?\s*\*{1,4}),?\s+(\S.*)$")
_POINTS_RE = re.compile(r"^(\d{1,2}[.,]\d{2})(S?)([+\-oO0]*)$")
_RANK_NUM_RE = re.compile(r"^(\d{1,3})\.?([a-z]?)$")
_RANK_LETTER_RE = re.compile(r"^[a-z]$")
_MARKERS = {"*", "S", "s", "\x01", "\uf053", "\u25cf", "\u2022", "\u00b0"}  # \uf053 = Symbol-font S
_STARS_RE = re.compile(r"^,?\s*([ST])?\s*(\*{1,4}),?$|^,?([ST]),?$")
_TRAIL_STARS_RE = re.compile(r"(?:,\s*|\s+)([ST])?\s*(\*{1,4})\s*,?$|,\s*([ST])\s*,?$")
_GLUED_ST_RE = re.compile(r"\s([ST])(\*{1,4}),?$")
_STATUS_PAREN_RE = re.compile(r"\s*\((?:E|K|EK|TK|N|NK)\)")
_YEAR_PAREN_RE = re.compile(r"\s*\((\d{4})\)")
_YEAR_RE = re.compile(r"^(19[4-9]\d|20[0-2]\d)$")
_SPACED_YEAR_RE = re.compile(r"^1 9\d\d$|^2 0[0-2]\d$")
_CLUBNR_RE = re.compile(r"^\((\d{1,3})\)$")
_PAREN_CODE_RE = re.compile(r"^(.*?)\s*\(([A-Z][A-Z/]{1,5})\)$")
_STATUS_WORDS = ("kranz", "neukranz", "eidg", "unfall", "verletzt", "aufgegeben", "m.kranz",
                 "o.kranz", "ausg")
# Cantonal / sub-association codes printed in the lists (also glued to the club
# name in ESV lists: "ONWNidwalden", "AGZofingen").
ASSOC_CODES = frozenset({
    # BKSV Gauverbände
    "BO", "ET", "ML", "OA", "SL", "JB", "BE", "BKSV",
    # ISV
    "LU", "ONW", "OW/NW", "OW", "NW", "SZ", "UR", "ZG", "TI", "ISV",
    # NOSV
    "AP", "AR", "AI", "GL", "GR", "SG", "SH", "TG", "ZH", "NOS", "NOSV",
    # NWSV
    "AG", "BL", "BS", "SO", "NWS", "NWSV",
    # SWSV
    "FR", "GE", "NE", "VD", "VS", "JU", "SWS", "SWSV", "GA",
    "ONSV",
})
_GLUED_CODE_RE = re.compile(r"^(ONSV|ONW|NOSV|NWSV|SWSV|BKSV|ISV|[A-Z]{2})([A-Z][a-zäöüéèàçâ].*)$")


@dataclass
class RankingEntry:
    idx: int
    rank: str | None            # normalised "1a", "12"
    rank_num: int | None
    points: float | None
    result_str: str | None      # '+', '-', 'o' per Gang
    schlussgang: bool
    name_raw: str
    name: str
    sennen_turner: str | None   # 'S' / 'T'
    stars: str | None           # '*'..'****'
    birth_year: int | None
    residence: str | None
    assoc_code: str | None
    club_raw: str | None
    club_nr: int | None         # Bernese lists: "(181)" club number
    status: str | None          # 'Kranz', 'Neukranzer', ...
    page: int
    line_no: int


@dataclass(frozen=True)
class Reject:
    reason: str
    detail: str
    line_no: int | None = None


@dataclass
class RankingParse:
    layout: str
    entries: list[RankingEntry] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)
    data_lines: int = 0       # lines that looked like ranking rows


@dataclass(frozen=True)
class _Header:
    residence_x: float | None
    club_x: float | None
    status_x: float | None
    assoc_x: float | None       # "Verband" column
    name_x: float | None


def _find_header(line: Line) -> _Header | None:
    text = line.text
    if not (re.search(r"Rang", text) and re.search(r"Name|Punkte", text)):
        return None

    def x_of(*keys: str) -> float | None:
        for w in line.words:
            for k in keys:
                pos = w.text.find(k)
                if pos >= 0:
                    # approximate x of the keyword inside a glued word
                    frac = pos / max(len(w.text), 1)
                    return w.x0 + frac * (w.x1 - w.x0)
        return None

    return _Header(residence_x=x_of("Wohnort"), club_x=x_of("Schwingklub", "Klub"),
                   status_x=x_of("Status"), assoc_x=x_of("Verband"), name_x=x_of("Name"))


def normalise_result(raw: str) -> str:
    return raw.replace("O", "o").replace("0", "o")


def _norm_points(raw: str) -> float:
    return float(raw.replace(",", "."))


def _split_name(raw: str) -> tuple[str, str | None, str | None, int | None]:
    """'Wicki Joel, S **' -> ('Wicki Joel', 'S', '**', None); handles '(2005)'."""
    s = raw.strip().strip(",").strip()
    s = _STATUS_PAREN_RE.sub("", s)
    year = None
    m = _YEAR_PAREN_RE.search(s)
    if m:
        year = int(m.group(1))
        s = (s[:m.start()] + s[m.end():]).strip()
    st = stars = None
    m2 = _GLUED_ST_RE.search(s)
    if m2:
        st, stars = m2.group(1), m2.group(2)
        s = s[:m2.start()]
    else:
        m3 = _TRAIL_STARS_RE.search(s)
        if m3:
            st = m3.group(1) or m3.group(3)
            stars = m3.group(2)
            s = s[:m3.start()]
        elif s.endswith("*"):
            stripped = s.rstrip("*")
            stars = s[len(stripped):]
            s = stripped
    s = re.sub(r"\s*,\s*$", "", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r"\s+", " ", s).strip(" ,")
    return s, st, stars, year


def _split_code(text: str) -> tuple[str | None, str]:
    """'ONWNidwalden' -> ('ONW', 'Nidwalden'); 'LU Entlebuch' -> ('LU', 'Entlebuch')."""
    parts = text.split(" ", 1)
    if parts[0] in ASSOC_CODES:
        return parts[0], (parts[1] if len(parts) > 1 else "")
    m = _GLUED_CODE_RE.match(text)
    if m and m.group(1) in ASSOC_CODES:
        return m.group(1), m.group(2)
    return None, text


@dataclass
class _Row:
    rank_num: int | None = None
    rank_letter: str = ""
    continuation: bool = False
    points: float | None = None
    schlussgang: bool = False
    result: str = ""
    rest: list[Word] = field(default_factory=list)


_RANK_PIECE_RE = re.compile(r"^\d{1,3}\.?[a-z]?$|^\.$|^[a-z]$|^\d\.[a-z]$")
_RANK_JOINED_RE = re.compile(r"^(\d{1,3})\.?([a-z]?)$")


def _take_rank(words: list[Word], row: _Row, limit_x: float | None) -> int:
    """Consume leading rank pieces ('1 0. a', '11a', '3.', 'b'); returns #words used."""
    pieces: list[Word] = []
    for w in words:
        if limit_x is not None and w.x0 >= limit_x:
            break
        if not _RANK_PIECE_RE.match(w.text):
            break
        if pieces and w.x0 - pieces[-1].x1 > 12:
            break
        if pieces and _RANK_LETTER_RE.match(pieces[-1].text):
            break  # a letter ends the rank
        pieces.append(w)
    while pieces:
        joined = "".join(p.text for p in pieces)
        m = _RANK_JOINED_RE.match(joined)
        if m:
            row.rank_num = int(m.group(1))
            row.rank_letter = m.group(2)
            return len(pieces)
        if _RANK_LETTER_RE.match(joined):
            row.rank_letter = joined
            row.continuation = True
            return len(pieces)
        pieces.pop()
    return 0


def _merge_split_points(words: list[Word]) -> list[Word]:
    """'1' + '7.00' printed with a gap -> '17.00'; '57.0' + '0' -> '57.00'."""
    out: list[Word] = []
    for w in words:
        if out and w.x0 - out[-1].x1 < 5 and (
                re.match(r"^\d$", out[-1].text) and re.match(r"^\d[.,]\d{2}", w.text)
                and w.x0 - out[-1].x1 < 4
                or re.match(r"^\d{2}[.,]\d$", out[-1].text) and re.match(r"^\d$", w.text)):
            prev = out.pop()
            out.append(Word(prev.text + w.text, prev.x0, w.x1))
        else:
            out.append(w)
    return out


def _scan(line: Line, name_x: float | None) -> _Row:
    """Pull rank, markers, points and result symbols out of a line; the
    remaining words (name, residence, club ...) go to ``rest``."""
    row = _Row()
    words = _merge_split_points(list(line.words))
    limit_x = (name_x - 2) if name_x is not None else None
    i = _take_rank(words, row, limit_x)
    for w in words[i:]:
        t = w.text
        gm = _LETTER_RESULT_RE.match(t)
        if gm and not row.result:
            # 'E+-++o+' / 'S++-++o': status letter or Schlussgang marker glued to the result
            row.schlussgang = row.schlussgang or gm.group(1) == "S"
            t = gm.group(2)
        pm = _POINTS_RE.match(t)
        if pm and row.points is None and 5 <= _norm_points(pm.group(1)) <= 90:
            row.points = _norm_points(pm.group(1))
            row.schlussgang = row.schlussgang or bool(pm.group(2))
            row.result += pm.group(3)
            continue
        if _RESULT_RE.match(t) and not (t in {"o", "O", "0"} and not row.result
                                         and row.rest and _next_is_text(words, w)):
            row.result += t
            continue
        if t in _MARKERS and not row.rest:
            if t in {"S", "\x01", "\uf053", "s"}:
                row.schlussgang = True
            continue
        if t in {"S", "\uf053"} and row.rest and not row.result and _next_is_result(words, w):
            row.schlussgang = True  # 'S' printed just before the result string
            continue
        if t in {"S", "\uf053"} and row.result and row.points is not None and w is words[-1]:
            row.schlussgang = True  # 'S' printed after the result string
            continue
        if t == "*" and row.points is not None and not row.rest:
            continue
        row.rest.append(w)
    return row


def _next_is_text(words: list[Word], w: Word) -> bool:
    k = words.index(w)
    return k + 1 < len(words) and not _RESULT_RE.match(words[k + 1].text)


def _next_is_result(words: list[Word], w: Word) -> bool:
    k = words.index(w)
    return k + 1 < len(words) and bool(_RESULT_RE.match(words[k + 1].text))


def _assign_header(rest: list[Word], h: _Header) -> dict[str, str]:
    """Columns by the header's x positions."""
    cols: dict[str, list[str]] = {"name": [], "residence": [], "code": [], "club": [],
                                  "status": [], "assoc": []}
    for w in rest:
        x = w.x0
        if h.status_x is not None and x >= h.status_x - 3:
            cols["status"].append(w.text)
        elif h.club_x is not None and x >= h.club_x - 3:
            cols["club"].append(w.text)
        elif h.assoc_x is not None and x >= h.assoc_x - 3:
            cols["assoc"].append(w.text)
        elif h.club_x is not None and h.residence_x is not None and x >= h.club_x - 28 \
                and x > h.residence_x + 40 and (w.text in ASSOC_CODES or _GLUED_CODE_RE.match(w.text)):
            cols["code"].append(w.text)
        elif h.residence_x is not None and x >= h.residence_x - 6:
            cols["residence"].append(w.text)
        else:
            cols["name"].append(w.text)
    return {k: " ".join(v) for k, v in cols.items()}


_STATUS_LETTERS = frozenset({"E", "K", "KK", "EK", "TK", "N", "NK", "EN", "Kranz", "m.Kranz"})
_JG_RE = re.compile(r"^(?:(E|K|KK|EK|TK|N|NK)\s*/\s*)?Jg\.\s*(\d{2})\s*(.*)$")
# Teilverband codes also printed right after the residence ("... im Kandertal BKSV");
# cantonal codes are not split off there: they belong to place names ("Ollon VD").
_RESIDENCE_TAIL_CODES = frozenset({"BKSV", "ISV", "NOS", "NOSV", "NWS", "NWSV", "SWS", "SWSV",
                                   "ONW", "GST", "ARLS"})
_ONE_NAME_RE = re.compile(r"^[A-ZÄÖÜÉÈ][A-Za-zÀ-ÿ'\-]+$")
_FIRST_NAME_CELL_RE = re.compile(r"^[A-ZÄÖÜÉÈ][a-zà-ÿ\-]+(?: [A-ZÄÖÜÉÈ][a-zà-ÿ\-]+)?(?:\s*,?\s*[ST]?\s*\**)?$")


def _assign_plain(rest: list[Word]) -> dict[str, str]:
    """Cells: name (+stars), [birth year], residence, [code / (nr) / (CODE)], [club]."""
    out = {"name": "", "residence": "", "code": "", "club": "", "status": "", "clubnr": "",
           "year": ""}
    cells = [" ".join(w.text for w in c) for c in split_cells(rest, CELL_GAP)]
    # Bernese lists print surname and first name as two columns ("Wenger" | "Kilian")
    if len(cells) > 1 and _ONE_NAME_RE.match(cells[0]) and _FIRST_NAME_CELL_RE.match(cells[1]) \
            and cells[1] not in ASSOC_CODES and cells[1] not in _STATUS_LETTERS:
        cells = [f"{cells[0]} {cells[1]}"] + cells[2:]
    # A star cell split off the name ("Gisler Bruno" | "S***") belongs to the name.
    merged: list[str] = []
    for c in cells:
        if merged and len(merged) == 1 and _STARS_RE.match(c.replace(" ", "")):
            merged[0] = f"{merged[0]} {c}"
        else:
            merged.append(c)
    if not merged:
        return out
    # "Mahrer Jürg** Hellikon": stars glued to the name, residence after them
    m = _NAME_STARS_REST_RE.match(merged[0])
    if m and "," not in m.group(3):
        merged = [f"{m.group(1)} {m.group(2)}", m.group(3)] + merged[1:]
    # "Schuler Christian, S ***, Rothenthurm": residence glued to the name cell
    parts = [p.strip() for p in merged[0].split(",")]
    name_parts = [parts[0]]
    spill: list[str] = []
    for p in parts[1:]:
        if not p:
            continue
        if _STARS_RE.match(p.replace(" ", "")) and not spill:
            name_parts.append(p)
        else:
            spill.append(p)
    merged = [", ".join(name_parts)] + ([", ".join(spill)] if spill else []) + merged[1:]
    out["name"] = merged[0]
    for c in merged[1:]:
        tokens = c.split()
        if _YEAR_RE.match(c) or _SPACED_YEAR_RE.match(c):
            out["year"] = c.replace(" ", "")
            continue
        if any(t.lower().startswith(_STATUS_WORDS) for t in tokens) and len(tokens) <= 2:
            out["status"] = c
            continue
        m = _CLUBNR_RE.match(tokens[0]) if tokens else None
        if m:
            out["clubnr"] = m.group(1)
            if len(tokens) > 1:
                out["code"] = " ".join(tokens[1:])
            continue
        if c in ASSOC_CODES:
            out["code"] = c
            continue
        if c in _STATUS_LETTERS and not out["residence"]:  # Kranz status column "EK", "KK"
            out["status"] = c
            continue
        jm = _JG_RE.match(c)
        if jm and not out["residence"]:  # "K / Jg. 95Waadtland", "Jg. 97"
            out["status"] = out["status"] or (jm.group(1) or "")
            yy = int(jm.group(2))
            out["year"] = str(1900 + yy if yy > 30 else 2000 + yy)
            c = jm.group(3).strip()
            if not c:
                continue
        pm = _PAREN_CODE_RE.match(c)
        if pm and not out["residence"]:
            out["residence"] = pm.group(1)
            out["code"] = pm.group(2)
            continue
        if not out["residence"]:
            out["residence"] = c
        elif not out["club"]:
            out["club"] = c
        else:
            out["club"] += " " + c
    return out


_GUEST_CLUB_RE = re.compile(r"^Gast(?:\s+([A-Z]{2,4}))?$")
_STATUS_TOKENS = frozenset({"kranz", "neukranzer", "m.kranz", "o.kranz", "ausg", "unfall",
                            "verletzt", "eidg", "aufgegeben"})
_STATUS_PHRASE_RE = re.compile(
    r"(?:(?<=[a-zäöüéè])|\s+|^)((?:\d\.\s*)?(?:nicht im Ausstich|Teilverbandskranz|"
    r"Kantonalkranz|Bergkranz|Neukranzer|Kranz|Unfall(?: \d\. Gang)?|verletzt|Auszeichnung|"
    r"(?:0 )?Accident[ée]?|nouveau c[ou]{1,2}ronn[eé]e?))\s*$", re.I)


def _strip_status(club: str) -> tuple[str, str | None]:
    """'Zofingen m.Kranz' -> ('Zofingen', 'm.Kranz'); 'LaufentalKranz' -> ('Laufental',
    'Kranz'); 'nicht im Ausstich' -> ('', 'nicht im Ausstich')."""
    m = _STATUS_PHRASE_RE.search(club)
    if m:
        rest, found = _strip_status(club[:m.start()].strip())
        return rest, " ".join(x for x in (found, m.group(1).strip()) if x)
    tokens = club.split()
    k = len(tokens)
    while k > 0 and tokens[k - 1].lower().rstrip(".") in _STATUS_TOKENS:
        k -= 1
    if k == len(tokens):
        return club, None
    return " ".join(tokens[:k]), " ".join(tokens[k:])


def _strip_residence_tail(residence: str) -> tuple[str, str | None, str | None]:
    """'Stein S EK Kranz' -> ('Stein', 'S', 'EK Kranz'): unlabelled S/T and Kranz
    status columns that some header layouts print between residence and points."""
    tokens = residence.split()
    st = None
    tail: list[str] = []
    while len(tokens) > 1 and (tokens[-1] in _STATUS_LETTERS or tokens[-1] in {"S", "T"}):
        t = tokens.pop()
        if t in {"S", "T"}:
            st = st or t
        else:
            tail.insert(0, t)
    return " ".join(tokens), st, (" ".join(tail) or None)


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", s).strip(" ,")
    return s or None


def _segments(line: Line) -> list[Line]:
    """Split two-column pages: a second entry starts after a wide gap with a
    rank (or continuation letter) right after a result / status word."""
    words = list(line.words)
    cuts = [0]
    for k in range(1, len(words)):
        prev, w = words[k - 1], words[k]
        if w.x0 - prev.x1 < 15 or not _RANK_PIECE_RE.match(w.text):
            continue
        prev_ok = bool(_RESULT_RE.match(prev.text)) or prev.text.lower().startswith(_STATUS_WORDS)
        ahead = words[k + 1:k + 4]
        if prev_ok and any(_POINTS_RE.match(a.text) for a in ahead) or (
                prev_ok and _RANK_LETTER_RE.match(w.text) and len(ahead) >= 2
                and re.match(r"^[A-ZÄÖÜ]", ahead[0].text)):
            cuts.append(k)
    if len(cuts) == 1:
        return [line]
    cuts.append(len(words))
    return [Line(line.page, line.y, tuple(words[a:b])) for a, b in zip(cuts, cuts[1:])]


def _is_two_col(lines: list[Line], page: int) -> bool:
    return sum(1 for ln in lines if ln.page == page and len(_segments(ln)) > 1) >= 3


def parse_ranking_lines(lines: list[Line], max_gaenge: int = 8) -> RankingParse:
    header: _Header | None = None
    layout = "plain"
    res = RankingParse(layout)
    prev: RankingEntry | None = None
    segs = [(no, seg) for no, ln in enumerate(lines) for seg in _segments(ln)]
    # two-column pages: read the left column first, then the right one, per page
    segs.sort(key=lambda t: (t[1].page, t[1].x0 > 280 and _is_two_col(lines, t[1].page), t[0]))
    for line_no, line in segs:
        h = _find_header(line)
        if h is not None:
            header = h
            layout = "esv" if (h.residence_x and h.club_x and h.status_x) else "header"
            continue
        row = _scan(line, header.name_x if header else None)
        has_rank = row.rank_num is not None or row.continuation
        looks_data = (has_rank and (row.points is not None or len(row.result) >= 3)) or (
            row.points is not None and len(row.result) >= 3)
        if not looks_data:
            if has_rank and row.rest and (row.points is not None or row.result):
                res.rejects.append(Reject("unparsed_row", line.text, line_no))
            continue
        res.data_lines += 1
        if header is not None and layout != "plain":
            cols = _assign_header(row.rest, header)
            clubnr = year = ""
        else:
            cols = _assign_plain(row.rest)
            clubnr, year = cols["clubnr"], cols["year"]
        name, st, stars, byear = _split_name(cols["name"])
        if year:
            byear = int(year)
        if not name or not re.search(r"[A-Za-zÀ-ÿ]{2}", name) or len(name.split()) < 2:
            res.rejects.append(Reject("no_name", line.text, line_no))
            continue
        result = normalise_result(row.result) if row.result else None
        if result is not None and not 1 <= len(result) <= max_gaenge + 1:
            res.rejects.append(Reject("bad_result_length", f"{result!r}: {line.text}", line_no))
            result = None
        # continuation rows ("b Bieri ...") inherit rank number and points
        rank_num = row.rank_num
        points = row.points
        if row.continuation:
            if prev is None:
                res.rejects.append(Reject("continuation_without_rank", line.text, line_no))
                continue
            rank_num = prev.rank_num
            if points is None:
                points = prev.points
        elif points is None and prev is not None and rank_num == prev.rank_num:
            points = prev.points
        rank = f"{rank_num}{row.rank_letter}" if rank_num is not None else None
        code = cols.get("code") or ""
        club = cols.get("club") or ""
        if club and not code:
            c2, club = _split_code(club)
            code = c2 or ""
        elif code and " " in code and code.split(" ", 1)[0] in ASSOC_CODES:
            code, extra = code.split(" ", 1)
            club = f"{extra} {club}".strip()
        elif code and code not in ASSOC_CODES:
            c2, rest_club = _split_code(code)
            if c2:
                code, club = c2, f"{rest_club} {club}".strip()
        gm = _GUEST_CLUB_RE.match(club)
        if gm:  # Bernese lists: "Gast VD" / "Gast" in the club column
            code, club = code or (gm.group(1) or ""), ""
        assoc = cols.get("assoc") or ""
        if assoc and not code:
            code = assoc
        status = _clean(cols.get("status"))
        club, tail_status = _strip_status(club)
        status = status or tail_status
        residence, res_st, res_status = _strip_residence_tail(cols.get("residence") or "")
        head, _, last = residence.rpartition(" ")
        if head and not code and last in _RESIDENCE_TAIL_CODES:
            residence, code = head, last
        st = st or res_st
        status = status or res_status
        entry = RankingEntry(
            idx=len(res.entries), rank=rank, rank_num=rank_num, points=points,
            result_str=result, schlussgang=row.schlussgang, name_raw=line.text,
            name=name, sennen_turner=st, stars=stars, birth_year=byear,
            residence=_clean(residence), assoc_code=_clean(code),
            club_raw=_clean(club), club_nr=int(clubnr) if clubnr else None,
            status=status, page=line.page, line_no=line_no)
        res.entries.append(entry)
        prev = entry
    res.layout = layout
    _drop_repeated_rows(res)
    return res


def _drop_repeated_rows(res: RankingParse) -> None:
    """Some PDFs repeat their pages (Le Mouret 2012: 3 pages x 60). Identical rows
    (rank, name, points, result) are kept once; the count is recorded as a reject."""
    seen: set[tuple[object, ...]] = set()
    kept: list[RankingEntry] = []
    for e in res.entries:
        k = (e.rank, e.name, e.points, e.result_str, e.residence)
        if k in seen:
            continue
        seen.add(k)
        kept.append(e)
    dropped = len(res.entries) - len(kept)
    if dropped:
        for i, e in enumerate(kept):
            e.idx = i
        res.entries = kept
        res.rejects.append(Reject("repeated_rows_removed", f"{dropped} identical rows"))


def parse_ranking_pdf(content: bytes, max_gaenge: int = 8) -> RankingParse:
    return parse_ranking_lines(pdf_lines(content), max_gaenge=max_gaenge)
