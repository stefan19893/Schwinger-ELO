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
- [x] 1. Save Notenblatt fixtures (normal festival, ESAF, edge cases)
- [~] 2. Implement parser for one festival sheet + tests
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
- 2026-09-29 — **Task 1 done.** All statistic PDFs downloaded early (needed to survey layouts before choosing fixtures): `crawl` PDF stage in 5 chunks (`SCHWINGEN_PDF_MAX_REQUESTS=400`), **1865 requests + ~15 during candidate selection, 0 retries, 0 HTTP errors**, ~6 min per 400 (≈0.9 s/request). 1878 distinct URLs for active non-cancelled festivals (1879 festivals; the Oberarth/Schwarzenberg 2022 URL is shared). `data/raw/` now 252 MB. Text extraction of all 1879 with PDFium: 11 s, 0 errors.
  - **Layout survey** (scratch classifier over all texts): standard ESV/Heller one-line-per-bout 1693, multi-column (3 athletes per row, Heller spreadsheet/"(SWS)" style) 123 (2011: 15, 2012: 53, 2013: 34, 2014: 17, 2015: 4), block layout (symbols/names/grades in separate runs) 14 (2011–2015), other 42 (incl. NOSV "Rang: N" layout 2011–2013 with numbered entries, Binningen "Notenblätterdetails", garbled-font PDFs), empty/scanned (no text layer) 7.
  - **Symbols:** `+` win, `-` gestellt, `o`/`0` loss (older sheets use zero), `s+`/`s-`… marks the Schlussgang in block layouts, `z <sym> Name` (no grade) = extra bout listed after the regular Gänge (youth sheets), `°` before a name = withdrawn (0.00 points).
  - **Youth-mixed sections:** "Aktive"/"Actif" vs "Kat. B; Jg. 99/00", "Kategorie 2000/01", "Jungschwinger 03-04", "JS 00/01", "1999-2000", "2000 - 2001", bare "1997" (youth in Le Mouret 2012 — i.e. a bare year is a youth category, the active section is labelled "Actif"). Actives may come first or last.
  - **Name forms:** stars glued or spaced (`Remo**`, `* *`), status letters (`E`/`K`/`EK`/`TK`, `T**`/`S*` prefixes), `, S` category suffix, `(2001)`/`(03)` birth years, `(SWS)`/`(BE)` association, `(Bonaduz)` place, suffixes `Herger Elias 1`/`2`.
  - **Fixtures** (`tests/fixtures/statistic/`, 404 KB): extracted texts `<fest_id>.txt` for 15 sheets (normal 46055, ESAF 24110 + 21055, draw-heavy 37052 [42 % draws vs median 20 %], youth-mixed 25799 + 26296, 2011 multi-column Bergkranz 26412, block 26414, Rang layouts 26413 + 26108, shared sheet 23674, Jungaktive 24013, Hallenschwinget 45965, Binningen 25931, garbled 24038 [trimmed]); real PDFs 46055.pdf + 45965.pdf (to test extraction); `festivals.json` = the `festivals` rows (+ note) of all fixtures incl. 23796 (true owner of the shared sheet).
