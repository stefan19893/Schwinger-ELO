"""Positioned text from PDFs: lines of words with x coordinates (PDFium).

The Schlussrangliste PDFs are tables whose columns (residence, cantonal
association code, Schwingklub, status) are separated only by horizontal
space, so plain text extraction cannot tell where a residence ends and a club
starts. :func:`pdf_lines` returns every text line as words with their x
extent; :func:`split_cells` groups words into cells at wide gaps.
"""

from __future__ import annotations

from dataclasses import dataclass

import pypdfium2 as pdfium


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float


@dataclass(frozen=True)
class Line:
    page: int
    y: float           # baseline-ish (bottom of the tallest glyph box), PDF units
    words: tuple[Word, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return self.words[0].x0 if self.words else 0.0


# A gap wider than this (PDF points) between two glyphs starts a new word even
# when the PDF has no space character there (e.g. "77.50S++++" vs. column jumps).
_WORD_GAP = 3.5


# Fragments of one PDFium row whose baselines agree within this many points (fonts of
# different size sit up to ~4 pt apart; list rows are >= 9 pt apart) are the
# same printed row (emitted out of x order); farther apart they are different rows.
_SAME_ROW_TOL = 4.0


def _page_lines(textpage: pdfium.PdfTextPage, page_no: int) -> list[Line]:
    """Lines as PDFium detects them (it emits ``\r\n`` between lines), words
    split at space characters and at gaps wider than :data:`_WORD_GAP`."""
    n = textpage.count_chars()
    text = textpage.get_text_range(0, n) if n else ""
    rows: list[list[tuple[str, float, float, float, int]]] = [[]]
    for i, ch in enumerate(text[:n]):
        if ch == "\n":
            rows.append([])
            continue
        if ch == "\r" or ch == "\xad":  # soft hyphens duplicate a printed '-'
            continue
        if ch == "\ufffe":  # PDFium's marker for a '-' that ends a line ("+o+o-")
            ch = "-"
        left, bottom, right, _top = textpage.get_charbox(i)
        rows[-1].append((ch, left, bottom, right, i))
    lines: list[Line] = []
    for row in rows:
        visible = [c for c in row if not c[0].isspace()]
        if not visible:
            continue
        words: list[Word] = []
        first_char: list[int] = []      # char index of each word's first glyph
        cur, x0, x1, start_i = "", 0.0, 0.0, 0
        for ch, left, _b, right, i in row:
            if ch.isspace() or (cur and (left - x1 > _WORD_GAP or left < x0 - 1)):
                if cur:
                    words.append(Word(cur, x0, x1))
                    first_char.append(start_i)
                cur = ""
                if ch.isspace():
                    continue
            if not cur:
                x0, x1, start_i = left, right, i
            cur += ch
            x1 = max(x1, right)
        if cur:
            words.append(Word(cur, x0, x1))
            first_char.append(start_i)
        y = min(c[2] for c in visible)
        # PDFium's row may hold text that jumps back to the left: either one printed
        # row emitted out of order (cells of a table: same baseline -> merged again,
        # words sorted by x) or two rows printed at almost the same height (split).
        parts: list[tuple[int, int]] = []
        start = 0
        for k in range(1, len(words) + 1):
            if k == len(words) or words[k].x0 < words[k - 1].x0 - 20:
                parts.append((start, k))
                start = k
        if len(parts) == 1:
            lines.append(Line(page_no, y, tuple(words)))
            continue
        groups: list[tuple[float, list[Word]]] = []
        for a, b in parts:
            # loose char boxes depend on the font, not the glyph: a stable baseline
            base = sorted(textpage.get_charbox(first_char[k], loose=True)[1]
                          for k in range(a, b))[(b - a) // 2]
            for g_base, g_words in groups:
                if abs(g_base - base) <= _SAME_ROW_TOL:
                    g_words.extend(words[a:b])
                    break
            else:
                groups.append((base, list(words[a:b])))
        for _base, g_words in groups:
            lines.append(Line(page_no, y, tuple(sorted(g_words, key=lambda w: w.x0))))
    return lines


def pdf_lines(content: bytes) -> list[Line]:
    """All text lines of all pages, top to bottom per page."""
    pdf = pdfium.PdfDocument(content)
    try:
        out: list[Line] = []
        for page_no, page in enumerate(pdf):
            textpage = page.get_textpage()
            out.extend(_page_lines(textpage, page_no))
            textpage.close()
            page.close()
        return out
    finally:
        pdf.close()


def split_cells(words: tuple[Word, ...] | list[Word], gap: float) -> list[list[Word]]:
    """Group consecutive words into cells; a horizontal gap > ``gap`` starts a new cell."""
    cells: list[list[Word]] = []
    for w in words:
        if cells and w.x0 - cells[-1][-1].x1 <= gap:
            cells[-1].append(w)
        else:
            cells.append([w])
    return cells
