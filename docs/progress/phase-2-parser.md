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
- [~] 1. Save Notenblatt fixtures (normal festival, ESAF, edge cases)
- [ ] 2. Implement parser for one festival sheet + tests
- [ ] 3. Validation rules (grade range, symmetric bouts, gang count) + tests
- [ ] 4. Batch-parse all crawled festivals into SQLite `bouts` / `athletes_raw`
- [ ] 5. Wire `cli parse`; create `tests/fixtures/sample/` and `--sample` support for crawl/parse
- [ ] 6. Report data quality (bouts per year, draw rate, parse failures) in handoff notes

## Known risks from Phase 1 review
Logged 2026-09-29 from the Phase 1 phase-reviewer; details in `phase-1-crawler.md` (task 1 + task 7 notes). Not yet addressed.
- **Shared statistic PDF URL:** Schwarzenberg-Schwinget 2022 and Frühjahrsschwinget Oberarth 2022 both point to `.../Statistik-1.pdf.pdf`. Verify each PDF's header (festival name/date) against the `festivals` row before attaching bouts; flag mismatches instead of importing.
- **Youth-mixed PDFs 2012–2015:** 13 Regional festivals have statistic PDFs described "(inkl. Nachwuchs)" — keep only the active (Aktive) category.
- **Borderline events** — decide (and log under Decisions) whether they count toward ELO: Mannschaftsmeisterschaft Comptoir Lausanne 2015 (team format), Rangschwinget Jungaktive Sarnen 2021 (young actives), Ausland events (e.g. Quebec/Kanada), Hallenschwinget (indoor, often reduced fields).
- **Request budget:** ~1880 statistic-PDF downloads exceed the 1500 `crawl_max_requests` cap — PDFs need their own cap setting and cache policy (PDFs of finished festivals never change → cache forever; consider a separate `max_age` rule only for current-season festivals).
- **`pypdf` not in `requirements.txt`** yet (tested in Phase 1, gives clean text).
- **Parsing quirks:** the Schlussgang loser is printed as `o` with a loss grade (e.g. ESAF 2019 Wicki Gang 8 `o Stucki 8.75` vs Stucki `+ Wicki 10.00`) — derive outcomes from both sides/grades; duplicate names carry suffixes ("Herger Elias 1" / "Herger Elias 2") — keep them for identity resolution in Phase 3.

## Handoff notes
- 2026-09-29 — **Task 1 in progress (WIP note).** Added `src/scraper/statistic_pdfs.py` (cache policy, PDFium text extraction, `download_statistic_pdfs` with own cap + "10 consecutive errors → stop" guard) and a PDF stage in `crawl` (`--no-pdfs` to skip; `pdf_max_requests`=2500, `pdf_max_age_hours`=24, `pdf_final_grace_days`=14 in Config). Library: **pypdfium2** instead of pypdf — pypdf raised `PdfReadError` on the malformed 2013 Binningen sheet, PDFium reads it; identical text on normal sheets; faster. **Correction of a Phase 1 note:** the symbols are `+` win, `-` gestellt (draw), `o` loss — so "Schlussgang loser printed as o" is not a quirk, it is the normal loss symbol (verified: Scherrer/Bruhin `-` 8.75 both sides, totals add up). Layouts seen so far: modern ESV (2019+), 2013/14 with "Aktive"/"Actif" sections and split rank lines, 2011 "Schlussrangliste" style (`1.`, `* *`, `S58.75`), a 3-column layout (Le Mouret 2012, youth-mixed), Binningen 2013 "Notenblätterdetails" (points before name). Next: download all PDFs (`crawl` in chunks), survey layouts, pick fixtures.
