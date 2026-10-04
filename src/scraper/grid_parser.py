"""Statistic sheets before 2011: tables read by position (Phase 10).

The old "Statistik" PDFs on schlussgang.ch are newspaper tables: one to four athletes
side by side, each with a header cell (rank, name, total) and one cell per Gang (sign,
opponent, grade). Their text order is unreliable - names of a header row are emitted
after the whole page, grades in a separate run - so this parser works on printed rows
with x coordinates (:func:`src.scraper.pdf_layout.pdf_rows`) and assigns every Gang
cell to the athlete whose header starts above it.

Cell variants found in the 2001-2010 files:

* ``+ Dick Christian 10.00`` / ``O Klarer Rolf T 8,75`` / ``s+ Grab Martin S** 10.00``
* ``+ 10,00 Sonnay Martial`` (grade before the name)
* ``2 6 ZINDEL THOMAS +E 9.75`` ("Notenblätterdetails": ring, start number, name in
  capitals, sign with the opponent's Kranz letter, grade); the header of such a sheet is
  followed by a row ``Pl. 320 Rothenthurm GS ISV`` (start number, residence, canton or
  ``GS`` = guest, club or association)

and header variants ``1 Forrer Arnold 58.75``, ``3a``, ``2 a``, ``1.``, ``b`` (letter
only), ``77,25 Forrer Arnold S`` (total first, ESAF 2001), ``1. Christian Stucki, 58,75
pts``, with status / residence / association between name and total, and one athlete per
row with his Gänge three abreast.

The result is a :class:`~src.scraper.bouts_parser.Sheet`; pairing, checks and rejects are
those of :func:`~src.scraper.bouts_parser.build_festival`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from src.scraper.bouts_parser import (Block, Entry, Sheet, clean_name, name_keys,
                                      similar_names, squash_name, to_float)
from src.scraper.pdf_layout import Row, Word

_GRADE_RE = re.compile(r"^(?:[89]|10)[.,]\d{2}$")
_TOTAL_RE = re.compile(r"^[2-7]\d[.,]\d{1,2}$")
_SIGN_RE = re.compile(r"^s?[+\-oO0]$")
_SIGN_KRANZ_RE = re.compile(r"^[+\-oO0][EKk]$")          # "+E", "oK", "-k"
_RANK_RE = re.compile(r"^[1-9]\d{0,2}\.?(?:[a-z]{1,2}\.?)?$")
_RANK_LETTER_RE = re.compile(r"^[a-zA-Z]{1,2}\.?$")
_STATUS_RE = re.compile(r"^(?:[ST]?\*{1,3}|[STEKk]|EK|TK)$")
_GLUED_NUMBER_RE = re.compile(r"^(\d{1,3})([A-ZÄÖÜÉÈÀ]{2,}.*)$")              # "265WETZEL"
_GLUED_SIGN_RE = re.compile(r"^([A-ZÄÖÜÉÈÀ][A-ZÄÖÜÉÈÀ\-]+)([+o\-][EKk]?)$")   # "ROGERoK"
_INT_RE = re.compile(r"^\d{1,3}$")
_PL_RE = re.compile(r"^Pl\.?$")
_ASSOC_RE = re.compile(r"^(?:ISV|NOSV?|NWSV?|SWSV?|BKSV|BE)$")
_PARTICLES = {"von", "van", "de", "der", "di", "da", "del", "della", "la", "le", "du", "dos",
              "auf", "ab", "zur", "zum", "im", "am"}


def _is_grade(t: str) -> bool:
    return bool(_GRADE_RE.match(t))


def _is_total(t: str) -> bool:
    return bool(_TOTAL_RE.match(t))


def _is_sign(t: str) -> bool:
    return bool(_SIGN_RE.match(t) or _SIGN_KRANZ_RE.match(t))


def _sym(t: str) -> str:
    c = t.lstrip("s")[:1]
    return "o" if c in "oO0" else c


def title_case(name: str) -> str:
    """"VON AH BENJI" -> "von Ah Benji"; mixed-case names are returned unchanged."""
    if not name or name != name.upper():
        return name
    words = []
    for n, w in enumerate(name.split()):
        low = w.lower()
        if low in _PARTICLES and n == 0:
            words.append(low)
        else:
            words.append("-".join(p.capitalize() for p in low.split("-")))
    return " ".join(words)


_YEAR_SUFFIX_RE = re.compile(r"^(.*[^\W\d_]),? ([7-9]\d)$")


def _birth_suffix(name: str) -> str:
    """"Roschi Ruedi 91" (two-digit birth year, printed for the young athletes of some
    Bernese sheets) -> "Roschi Ruedi (91)", the form name_details() reads."""
    m = _YEAR_SUFFIX_RE.match(name)
    return f"{m.group(1)} ({m.group(2)})" if m else name


def _split_stars(words: Sequence[Word]) -> list[Word]:
    """Stars glued to a (cut-off) name become their own token: "Hans-Pete**"."""
    out: list[Word] = []
    for w in words:
        m = re.fullmatch(r"(.*[^\W\d_])(\*{1,3})", w.text)
        if m and len(m.group(1)) > 1:
            out.append(Word(m.group(1), w.x0, w.x1))
            out.append(Word(m.group(2), w.x1, w.x1))
        else:
            out.append(w)
    return out


def _unglue(words: Sequence[Word]) -> list[Word]:
    """Capitals sheets print without space where a column overflows: "265WETZEL",
    "ROGERoK", "ANDIo"."""
    out: list[Word] = []
    for w in words:
        m = re.match(r"^([1-9]\d{0,2}[a-z]{0,2}\.\))(\S.*)$", w.text)      # "4b.)Graber"
        if m:
            out.append(Word(m.group(1), w.x0, w.x0))
            w = Word(m.group(2), w.x0, w.x1)
        m = _GLUED_NUMBER_RE.match(w.text)
        if m:
            out.append(Word(m.group(1), w.x0, w.x0))
            w = Word(m.group(2), w.x0, w.x1)
        m = _GLUED_SIGN_RE.match(w.text)
        if m:
            out.append(Word(m.group(1), w.x0, w.x1))
            out.append(Word(m.group(2), w.x1, w.x1))
        else:
            out.append(w)
    return out


_LOOSE_GRADE_RE = re.compile(r"^(?:[89]|10)(?:[.,]\d)?$")
_LOOSE_TOTAL_RE = re.compile(r"^[3-7]\d$")
_RANK_PAREN_RE = re.compile(r"^[1-9]\d{0,2}[a-z]{0,2}\.\)$")                # "3a.)"


def _join_spaced_numbers(words: Sequence[Word]) -> list[Word]:
    """"58 . 25" (three words) and "54. 75" (two) -> "58.25", "54.75"."""
    out: list[Word] = []
    n = 0
    while n < len(words):
        if n + 2 < len(words) and words[n].text.isdigit() and words[n + 1].text in ".," \
                and re.fullmatch(r"\d{2}", words[n + 2].text):
            out.append(Word(f"{words[n].text}.{words[n + 2].text}", words[n].x0, words[n + 2].x1))
            n += 3
        elif n + 1 < len(words) and re.fullmatch(r"\d{1,2}[.,]", words[n].text) \
                and re.fullmatch(r"\d{2}", words[n + 1].text):          # "54. 75"
            out.append(Word(words[n].text[:-1] + "." + words[n + 1].text,
                            words[n].x0, words[n + 1].x1))
            n += 2
        else:
            out.append(words[n])
            n += 1
    return out


def _pad_numbers(rows: list[tuple[int, list[Word]]]) -> None:
    """Some sheets drop trailing zeros ("+E 9", "-K 8.5", "S 10", total "58"): bring
    grades back to two decimals and whole totals to one (in place)."""
    for _n, ws in rows:
        toks = [w.text for w in ws]
        signs = any(_is_sign(t) for t in toks)
        for i, w in enumerate(ws):
            if not i:
                continue
            last = i + 1 == len(ws)
            if signs and _LOOSE_GRADE_RE.match(w.text) and (
                    _is_sign(toks[i - 1]) or last or _is_sign(toks[i + 1])
                    or _INT_RE.match(toks[i + 1]) and not toks[i - 1][:1].isdigit()):
                ws[i] = Word(f"{to_float(w.text):.2f}", w.x0, w.x1)
            elif not signs and _RANK_RE.match(toks[0]) and _LOOSE_TOTAL_RE.match(w.text) \
                    and not toks[i - 1][:1].isdigit() \
                    and (last or _RANK_RE.match(toks[i + 1])):
                ws[i] = Word(w.text + ".0", w.x0, w.x1)


def _join_wrapped(rows: Sequence[Row]) -> list[tuple[int, list[Word]]]:
    """(row index, words) per printed row; the wrapped rest of an overlong last cell
    ("... 4 139 BAUMGARTNER RAPHAEL +" / "9.75", "... 3 85 SCHENK" / "ROGERoK 8.50") is
    appended to the row it belongs to."""
    out: list[tuple[int, list[Word]]] = []
    for n, row in enumerate(rows):
        words = _join_spaced_numbers(_unglue(row.words))
        toks = [w.text for w in words]
        if out and toks and len(toks) <= 4 and _is_grade(toks[-1]) \
                and not _is_sign(toks[0]) and not _INT_RE.match(toks[0]) \
                and sum(_is_grade(t) for t in toks) == 1:
            prev = [w.text for w in out[-1][1]]
            if any(_is_grade(t) for t in prev) and not _is_grade(prev[-1]) \
                    and not all(_STATUS_RE.match(t) for t in prev[-1:]):
                out[-1][1].extend(words)
                continue
        out.append((n, words))
    _pad_numbers(out)
    for k in range(1, len(out)):
        # a grade that wrapped below its own column: "... 5 27 VOLLENWEIDER SANDRO oK" /
        # "2 231 SCHNEIDER MARKUS + 10.00 8.75" (cells of such sheets end "sign grade")
        prev, cur = out[k - 1][1], out[k][1]
        if not prev or not _is_sign(prev[-1].text) or not _SIGN_KRANZ_RE.match(prev[-1].text) \
                and prev[-1].text not in ("+", "-", "o"):
            continue
        for i, w in enumerate(cur):
            if _is_grade(w.text) and (i == 0 or not _is_sign(cur[i - 1].text)) \
                    and any(_INT_RE.match(x.text) for x in prev[:2]):
                prev.append(w)
                del cur[i]
                break
    return [(n, ws) for n, ws in out if ws]


# ------------------------------------------------------------------------------- cells
_AFTER_TOTAL = ("*", "°", "K", "E", "pts", "pts.", "Couronne", "Kranz", "m.Kranz")
_LEADING_STATUS_RE = re.compile(r"^(?:[STK]|EK|KK|TK|BK|GK)$")


def _split_cells(words: Sequence[Word]) -> list[tuple[str, list[Word]]] | None:
    """Split a printed row into cells: ``("head", words)`` ends with a total,
    ``("bout", words)`` with a grade. Rows mix both where the columns are independent
    (ESAF 2004). None if the row is not made of such cells."""
    toks = [w.text for w in words]
    grades = [n for n, t in enumerate(toks) if _is_grade(t)]
    totals = [n for n, t in enumerate(toks) if _is_total(t)]
    if not grades and not totals:
        if toks and _RANK_PAREN_RE.match(toks[0]):   # "1.) Name, Ort 2.) Name, Ort"
            cells = []
            for w in words:
                if _RANK_PAREN_RE.match(w.text):
                    cells.append(("head", []))
                cells[-1][1].append(w)
            return cells
        return None
    if grades and not any(_is_sign(t) for t in toks):
        return None
    cells: list[tuple[str, list[Word]]] = []
    if len(toks) > 1 and _is_sign(toks[0]) and _is_grade(toks[1]) and not totals:
        # "+ 10,00 Name": a cell starts at each sign that a grade follows
        for n, w in enumerate(words):
            if _is_sign(w.text) and n + 1 < len(toks) and _is_grade(toks[n + 1]):
                cells.append(("bout", []))
            cells[-1][1].append(w)
        return cells
    if totals and totals[0] == 0 and not grades:
        # total first: "77,25 Forrer Arnold S 76,50 Sutter Thomas T"
        for n, w in enumerate(words):
            if n in totals:
                cells.append(("head", []))
            cells[-1][1].append(w)
        return cells
    cur: list[Word] = []
    for n, w in enumerate(words):
        if not cur and cells and cells[-1][0] == "head" and w.text in _AFTER_TOTAL:
            cells[-1][1].append(w)      # award mark / Kranz letter after the total
            continue
        cur.append(w)
        if _is_grade(w.text):
            cells.append(("bout", cur))
            cur = []
        elif _is_total(w.text) and len(cur) > 1:
            cells.append(("head", cur))
            cur = []
    if cur:
        # status / Kranz mark printed after the grade belongs to the cell before
        if cells and all(_STATUS_RE.match(w.text) or w.text in "*°" for w in cur):
            cells[-1][1].extend(cur)
        else:
            return None
    return cells


def _entry(cell: Sequence[Word], line: int) -> Entry | None:
    toks = [w.text for w in cell]
    while toks and (_STATUS_RE.match(toks[-1]) or toks[-1] in "*°") and not _is_grade(toks[-1]) \
            and any(_is_grade(t) for t in toks[:-1]):
        toks.pop()
    if len(toks) >= 3 and _is_sign(toks[0]) and _is_grade(toks[1]):       # sign grade name
        return Entry(_sym(toks[0]), _birth_suffix(" ".join(toks[2:])), to_float(toks[1]),
                     line, schlussgang=toks[0].startswith("s"))
    if len(toks) < 3 or not _is_grade(toks[-1]):
        return None
    grade = to_float(toks[-1])
    if _is_sign(toks[0]):                                                  # sign name grade
        name = " ".join(toks[1:-1])
        if not re.search(r"[^\W\d_]{2}", name):
            return None
        return Entry(_sym(toks[0]), _birth_suffix(name), grade, line,
                     schlussgang=toks[0].startswith("s"))
    if _is_sign(toks[-2]):                                    # [ring] [start no] NAME sign grade
        body = toks[:-2]
        while body and _INT_RE.match(body[0]):
            body = body[1:]
        if not body:
            return None
        return Entry(_sym(toks[-2]), title_case(" ".join(body)), grade, line)
    return None


def _header_block(cell: Sequence[Word], line: int) -> Block | None:
    words = _split_stars(cell)
    toks = [w.text for w in words]
    k = next((n for n, t in enumerate(toks) if _is_total(t)), len(toks))
    total = toks[k] if k < len(toks) else None       # "1.) Name, Ort": no total printed
    after = toks[k + 1:] if k > 0 else []
    body = toks[:k] if k > 0 else toks[1:]
    rank: str | None = None
    if body and _RANK_PAREN_RE.match(body[0]):
        rank = body[0][:-2]
        body = body[1:]
    elif k > 0 and body and _RANK_RE.match(body[0]):
        rank = body[0].replace(".", "")
        body = body[1:]
        if len(body) >= 2 and _RANK_LETTER_RE.match(body[0]):   # "2 a", "16 C"
            rank += body[0].rstrip(".").lower()
            body = body[1:]
    elif k > 0 and len(body) > 2 and _RANK_LETTER_RE.match(body[0]):
        rank = body[0].rstrip(".").lower()   # "b Remy Guillaume": continues the rank above
        body = body[1:]
    body = [t for t in body if t not in ("pts", "pts.")]
    lead: list[str] = []
    lead_place: str | None = None
    while len(body) > 2 and _LEADING_STATUS_RE.match(body[0]):   # "1 a S EK Stucki Christian"
        lead.append(body[0])
        body = body[1:]
    if not body:
        return None
    cut = next((n for n, t in enumerate(body) if n >= 1 and _STATUS_RE.match(t)), None)
    status: list[str] = []
    rest: list[str] = []
    if cut is not None:
        name_toks = body[:cut]
        n = cut
        while n < len(body) and _STATUS_RE.match(body[n]):
            status.append(body[n])
            n += 1
        rest = body[n:]
    else:
        name_toks = body
        if lead and len(name_toks) > 2 and re.fullmatch(r"[A-Z]{2,4}", name_toks[-1]):
            rest = [name_toks[-1]]          # "Stucki Christian Schnottwil SL"
            name_toks = name_toks[:-1]
        if lead and len(name_toks) > 2:     # this layout always prints the residence
            lead_place = name_toks[-1]
            name_toks = name_toks[:-1]
    status += lead
    name = " ".join(name_toks)
    place: str | None = None
    if "," in name:                      # "Grab Martin, Rothenthurm" / "Christian Stucki,"
        name, _, tail = name.partition(",")
        place = tail.strip() or None
    assoc: str | None = None
    if rest and _ASSOC_RE.match(rest[-1]) or (rest and re.fullmatch(r"[A-Z]{2,4}", rest[-1])
                                              and len(rest) > 1):
        assoc = rest[-1]
        rest = rest[:-1]
    while rest and rest[-1] in ("S", "T"):
        status.append(rest.pop())
    if rest:
        place = " ".join(rest)
    name = _birth_suffix(title_case(name.strip()))
    if not re.search(r"[^\W\d_]{2}", name):
        return None
    # status in the forms clean_name / status_of / _st_marker know: "Name S**",
    # "Name **", "Name, S", "Name K"
    stars = next((s.lstrip("ST") for s in status if "*" in s), "")
    st = next((s[0] for s in status if s[0] in "ST"), None)
    kranz = next((s for s in status if s in ("E", "K", "EK", "TK")), None)
    if stars:
        name_raw = f"{name} {st or ''}{stars}"
    elif st:
        name_raw = f"{name}, {st}"
    elif kranz:
        name_raw = f"{name} {kranz}"
    else:
        name_raw = name
    mark = "*" if "*" in after else ""
    block = Block(rank, name_raw, to_float(total) if total else None, line, mark=mark,
                  place=place, association=assoc)
    block.__dict__["_one_decimal"] = bool(total) and len(re.split(r"[.,]", total)[1]) == 1
    if lead_place:
        block.place = lead_place
        block.__dict__["_lead_place"] = True
    return block


# ------------------------------------------------------------------------------- sheet
_COLUMN_TOL = 25.0


def parse_grid(rows: Sequence[Row], fest_year: int | None = None) -> Sheet:
    """Blocks and entries from printed rows (see module docstring)."""
    sheet = Sheet(layout="grid", header=[], blocks=[])
    columns: list[tuple[float, Block]] = []       # (x of the header cell, block)
    start_nr: dict[int, Block] = {}
    entry_nr: list[tuple[Entry, int]] = []
    nr_names: dict[int, list[str]] = {}
    page = -1
    for line, words in _join_wrapped(rows):
        if not words:
            continue
        text = " ".join(w.text for w in words)
        if any(_PL_RE.match(w.text) for w in words):
            # "Pl. 320 Rothenthurm GS ISV": start number, residence, canton / guest, club
            # or association of the athlete whose header starts above it
            rest: list[Word] = []
            i = 0
            while i < len(words):
                if not _PL_RE.match(words[i].text):
                    rest.append(words[i])
                    i += 1
                    continue
                x = words[i].x0
                i += 1
                c: list[str] = []
                while i < len(words) and not _PL_RE.match(words[i].text) and not (
                        c and i + 1 < len(words) and _INT_RE.match(words[i].text)
                        and _INT_RE.match(words[i + 1].text)):
                    c.append(words[i].text)
                    i += 1
                col = [b for cx, b in columns if cx <= x + _COLUMN_TOL]
                if not col:
                    continue
                block = col[-1]
                if c and _INT_RE.match(c[0]):
                    start_nr[int(c[0])] = block
                    c = c[1:]
                if len(c) >= 2 and _ASSOC_RE.match(c[-1]):
                    block.association = c[-1]
                cut = next((n for n, t in enumerate(c)
                            if n >= 1 and re.fullmatch(r"[A-Z]{2}|GST", t)), len(c))
                if c[:cut]:
                    block.place = " ".join(c[:cut])
            words = rest
            if not words:
                continue
        split = _split_cells(words)
        if split:
            heads = [(_header_block(c, line), c[0].x0) for kind, c in split if kind == "head"]
            if any(b is None for b, _x in heads):
                split = None
        if split:
            all_heads = all(kind == "head" for kind, _c in split)
            if all_heads and rows[line].page != page and len(split) > 1:
                columns = []            # a new page that starts with a header row
            page = rows[line].page
            bad = False
            for kind, cell in split:
                x = cell[0].x0
                if kind == "head":
                    block = _header_block(cell, line)
                    assert block is not None
                    columns = [(cx, b) for cx, b in columns if abs(cx - x) > _COLUMN_TOL]
                    columns.append((x, block))
                    columns.sort(key=lambda c: c[0])
                    sheet.blocks.append(block)
                    continue
                entry = _entry(cell, line)
                col = [b for cx, b in columns if cx <= x + _COLUMN_TOL]
                if entry is None or not col:
                    bad = True
                    continue
                col[-1].entries.append(entry)
                nums = [w.text for w in cell[:2] if _INT_RE.match(w.text)]
                if len(nums) == 2 and not _is_sign(cell[0].text):
                    entry_nr.append((entry, int(nums[1])))
                    nr_names.setdefault(int(nums[1]), []).append(entry.opponent)
            if bad:
                sheet.suspicious.append((line, text, "unparsed_line_with_grade"))
            continue
        if not sheet.blocks:
            if len(sheet.header) < 8:
                sheet.header.append(text)
        elif any(_is_grade(w.text) for w in words):
            sheet.suspicious.append((line, text, "unparsed_line_with_grade"))
        else:
            sheet.noise.append((line, text))
    # start numbers identify the opponent exactly where the sheet prints them; the name
    # printed in the Gang cells is the better one (headers glue the rank letter to it:
    # "4 GTHÜRIG GUIDO", or cut it off)
    for nr, block in start_nr.items():
        names = nr_names.get(nr)
        if not names:
            continue
        best = max(sorted(set(names)), key=names.count)
        old = clean_name(block.name_raw)
        if squash_name(best) != squash_name(old):
            if block.rank and squash_name(old)[1:] == squash_name(best):
                block.rank += squash_name(old)[0]
            block.name_raw = best + block.name_raw[len(old):]
    for entry, nr in entry_nr:
        if nr in start_nr:
            entry.opponent = clean_name(start_nr[nr].name_raw)
    _fix_totals(sheet)
    _residence_names(sheet)
    reconcile_block_names(sheet)
    _fill_letter_ranks(sheet)
    return sheet


def _residence_names(sheet: Sheet) -> None:
    """Sheets that print "S EK Stucki Christian Schnottwil SL" add the residence to an
    opponent only where two athletes share a name ("Schmid Reto Frutigen"): such an
    opponent becomes "Schmid Reto (Frutigen)", and so does the header of that athlete,
    the form the pairing knows for namesakes."""
    tagged = {squash_name(f"{clean_name(b.name_raw)} {b.place}"): b
              for b in sheet.blocks if b.__dict__.pop("_lead_place", False) and b.place}
    if not tagged:
        return
    used: set[int] = set()
    for b in sheet.blocks:
        for e in b.entries:
            hit = tagged.get(squash_name(clean_name(e.opponent)))
            if hit is not None:
                e.opponent = f"{clean_name(hit.name_raw)} ({hit.place})"
                used.add(id(hit))
    for b in tagged.values():
        if id(b) in used:
            name = clean_name(b.name_raw)
            b.name_raw = f"{name} ({b.place})" + b.name_raw[len(name):]


def _fix_totals(sheet: Sheet) -> None:
    """Totals printed with one decimal are cut off ("58.2" for 58.25): take the sum of
    the grades when it agrees with the printed digits."""
    for b in sheet.blocks:
        if not b.__dict__.pop("_one_decimal", False) or b.points is None:
            continue
        total = round(sum(e.grade for e in b.entries if e.grade is not None), 2)
        if int(total * 10 + 1e-6) == int(b.points * 10 + 1e-6):
            b.points = total


def reconcile_block_names(sheet: Sheet) -> None:
    """A header name that no Gang cell mentions, while exactly one unprinted opponent
    name is its other spelling: the opponent's spelling wins (headers are cut off -
    "Pellet Hans-Pete" - or carry the residence without a separator - "Remy Guillaume
    Riaz"; first name first - "Christian Stucki")."""
    block_keys = {squash_name(name_keys(clean_name(b.name_raw))[1]) for b in sheet.blocks}
    opponents: dict[str, str] = {}
    for b in sheet.blocks:
        for e in b.entries:
            name = clean_name(e.opponent)
            opponents.setdefault(squash_name(name_keys(name)[1]), name)
    free = {k: v for k, v in opponents.items() if k not in block_keys}
    for b in sheet.blocks:
        name = clean_name(b.name_raw)
        base = name_keys(name)[1]
        if squash_name(base) in opponents:
            continue
        words = base.split()
        cands = []
        for key, opp in free.items():
            ow = name_keys(opp)[1].split()
            if len(ow) >= 2 and len(words) > len(ow) and words[:len(ow)] == ow:
                cands.append((opp, " ".join(name.split()[len(ow):])))   # name + residence
            elif similar_names(base, name_keys(opp)[1]) and len(key) >= 6:
                cands.append((opp, ""))
        if len(cands) != 1:
            continue
        opp, tail = cands[0]
        suffix = b.name_raw[len(name):] if b.name_raw.startswith(name) else ""
        b.name_raw = opp + suffix
        if tail and not b.place:
            b.place = tail


def _fill_letter_ranks(sheet: Sheet) -> None:
    """"b" continues the numbered rank before it; sheets set in columns list the ranks
    down each column, so the nearest numbered rank in reading order may be wrong - a
    letter-only rank is therefore completed only from the points: the numbered rank
    with the same total."""
    by_points: dict[float, str] = {}
    for b in sheet.blocks:
        m = re.match(r"\d+", b.rank or "")
        if m and b.points is not None:
            by_points.setdefault(b.points, m.group(0))
    for b in sheet.blocks:
        if b.rank and b.rank.isalpha():
            num = by_points.get(b.points) if b.points is not None else None
            b.rank = (num + b.rank) if num else None
