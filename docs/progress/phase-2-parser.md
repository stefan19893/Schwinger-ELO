# Phase 2 — Bout parser (Milestone 2)
**Agent:** data-engineer

## Goal
Extract every bout (Gang) with outcome and grades, plus raw athlete metadata.

## Exit criteria
- `src/scraper/bouts_parser.py` produces DataFrames matching the `Bout` and `Athlete` schemas.
- Outcome (`WIN_A`/`WIN_B`/`DRAW`) and grades (8.25–10.00) validated; invalid rows logged, not silently dropped.
- Handles 6-Gang festivals and 8-Gang ESAF.
- `python -m src.cli parse` wired up.
- Offline sample dataset in `tests/fixtures/sample/` (a few festivals, incl. one ESAF) usable via `--sample`.
- Fixture-based tests incl. an ESAF sheet and a draw-heavy sheet.

## Tasks
- [ ] 1. Save Notenblatt fixtures (normal festival, ESAF, edge cases)
- [ ] 2. Implement parser for one festival sheet + tests
- [ ] 3. Validation rules (grade range, symmetric bouts, gang count) + tests
- [ ] 4. Batch-parse all crawled festivals into SQLite `bouts` / `athletes_raw`
- [ ] 5. Wire `cli parse`; create `tests/fixtures/sample/` and `--sample` support for crawl/parse
- [ ] 6. Report data quality (bouts per year, draw rate, parse failures) in handoff notes

## Handoff notes
