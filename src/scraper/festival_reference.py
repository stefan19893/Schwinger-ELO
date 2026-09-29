"""Reference list of Swiss Kranzfeste (user-supplied) used to validate categories.

``reference/schwingfeste_schweiz.json`` lists the festivals of each tier:
eidgenössische Feste, the six Bergkranzfeste, the five Teilverbandsfeste and
the Kantonal-/Gauverbandsfeste per Teilverband. Festival names on
schlussgang.ch vary ("Innerschweizer Schwingfest Seedorf 2025" vs
"Innerschweizerisches Schwing- und Älplerfest (ISAF)"), so matching is done on
a *stem* of the leading word(s): lower-cased, adjective endings stripped
("innerschweizerisches" -> "innerschweiz", "Stoos-Schwinget" -> "stoos").

The crawler only uses this to *check* its tid/name based mapping (warnings),
never to override it.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

REFERENCE_PATH = Path(__file__).parent / "reference" / "schwingfeste_schweiz.json"

# Kranzfest categories (Regional = no Kranz, not checked).
_GROUPS = {
    "bergkranzfeste": "Bergkranz",
    "teilverbandsfeste": "Teilverband",
}

# Not in the reference file, but real Kranzfeste (documented deviations):
SUPPLEMENT: dict[str, str] = {
    # BKSV has six Gauverbände; the reference lists only five Gau festivals.
    "bern-jurass": "Gauverband",
    # schlussgang spells the Basel-Landschaft cantonal festival this way.
    "basellandschaftlich": "Kantonal",
}

_GENERIC = {"schwinget", "schwingfest", "schwingertag", "kantonal", "kantonales",
            "kantonalschwingfest", "und", "schwing", ""}
_ENDING_RE = re.compile(r"(isches|ischer|isch|es|er)$")


def _stem_word(word: str) -> str:
    w = word.lower().strip(" .,()")
    prev = None
    while prev != w and len(w) > 4:
        prev, w = w, _ENDING_RE.sub("", w)
    return w


def stems(name: str) -> list[str]:
    """Candidate stems of a festival name: full first token, then its hyphen parts."""
    tokens = re.sub(r"\(.*?\)", " ", name).split()
    if not tokens:
        return []
    first = tokens[0].lower().rstrip("-")
    out = [_stem_word(first)] if "-" in first else []
    out += [_stem_word(p) for p in first.split("-")]
    return [s for s in out if s and s not in _GENERIC]


@functools.lru_cache(maxsize=1)
def reference_stems() -> dict[str, str]:
    """stem -> expected spec category, built from the reference JSON (+ supplement)."""
    data = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["schwingfeste_schweiz"]
    out: dict[str, str] = {}

    def add(name: str, category: str) -> None:
        for s in stems(name):
            out.setdefault(s, category)

    for t in data["eidgenoessische_feste"]["turniere"]:
        add(t["name"], "ESAF")
    for group, category in _GROUPS.items():
        for t in data[group]["turniere"]:
            add(t["fest"], category)
    for tv, names in data["kantonal_und_gauverbandsfeste"]["unterteilung_nach_teilverband"].items():
        # Only BKSV has Gauverbände; all other listed festivals are cantonal.
        for n in names:
            add(n, "Gauverband" if tv == "BKSV" else "Kantonal")
    for stem, category in SUPPLEMENT.items():
        out[stem] = category
    return out


def reference_category(name: str) -> str | None:
    """Expected category of a Kranzfest name per the reference, None if unknown."""
    ref = reference_stems()
    for s in stems(name):
        if s in ref:
            return ref[s]
    return None
