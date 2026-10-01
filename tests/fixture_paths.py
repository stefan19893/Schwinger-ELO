"""Locate statistic-sheet fixtures.

Sheets used by the ``--sample`` dataset live only in ``fixtures/sample/statistic/``
(the sample must be self-contained); all other parser fixtures live in
``fixtures/statistic/``. Tests resolve a file from either place, so every sheet
is stored exactly once.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
STATISTIC = FIXTURES / "statistic"
SAMPLE_STATISTIC = FIXTURES / "sample" / "statistic"


def sheet_file(name: str) -> Path:
    """Path of a fixture sheet (``<fest_id>.txt`` / ``.pdf``) in either directory."""
    for d in (STATISTIC, SAMPLE_STATISTIC):
        if (d / name).is_file():
            return d / name
    raise FileNotFoundError(f"fixture {name} not in {STATISTIC} or {SAMPLE_STATISTIC}")
