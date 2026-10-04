# Phase 10 — History before 2011 (owner's request 2026-10-04: "all data as far back as possible")
**Agents:** data-engineer (tasks 1–5), elo-modeler (task 6), web-builder (task 7), phase-reviewer. Branch `phase-10-history` from `main` at e0ae051 (Phases 8 and 9 merged).

## Goal
Extend the data from "2011–present" to as far back as the permitted source carries usable results, so that ratings, profiles and comparisons cover earlier careers and the "data begins in 2011" caveats move back accordingly.

## Source — what is allowed and what is not
- **schlussgang.ch only** (website + `backend-api.schlussgang.ch/jsonapi`), as decided 2026-09-29. **esv.ch is not requested at all**: its Nutzungsbedingungen forbid automated retrieval and building a database without written permission of the ESV. The owner asked on 2026-10-04 for an esv.ch scraper; the main session declined and drafted a permission request for him to send. If the owner reports a written permission, that is a new phase; until then no request to esv.ch, however slow.
- Honest, identified crawling: the project User-Agent, robots.txt respected, every response cached in `data/raw/`, nothing fetched twice. Nothing that hides the crawler (no browser User-Agent, no proxy or address rotation).
- **Gentler than the normal crawl for this backfill:** it is a one-time bulk of old files nobody is waiting for. Use a clearly longer delay than the 0.5–1.0 s floor (decide and record; my suggestion 2–4 s with jitter), keep the per-run request caps, stop on repeated errors or any 403 / 429 instead of retrying harder, and spread the backfill over several runs if it is large.

## What is known (main session, 2026-10-04, four API requests)
- The JSON:API lists events before 2011: the first page sorted by date starts on 2001-04-29 (Kantonal-, Teilverbands-, Bergfeste, ESAF Nyon 2001) and there are more pages.
- No event before 2011 has `field_final_statistic_pdf` or `field_final_ranking_pdf` (both queries returned 0). The pipeline's bouts come from the statistic PDFs today.
- Events before 2011 do have `field_event_pdf` (first page of that query: 38 events of 2001, 12 of 2002, more pages). What these PDFs contain is not known yet — if they are Ranglisten with the Gänge per athlete (opponent, result, grade), bouts can be parsed from them; if they are only final ranks, they cannot.

## Exit criteria
- A written finding, with numbers per year 2001–2010: events listed, events with a PDF, PDFs that carry bouts, layouts found; and the earliest season from which the data is good enough to rate (coverage of the Kranzfeste, parse success, both sides of a bout agreeing). If nothing before 2011 carries bouts, the phase ends there with that finding.
- For the usable years: festivals crawled and stored, PDFs cached, bouts parsed into the existing schema with the same checks as 2011+ (mirror check, rejects recorded), parser tests against saved fixtures (anonymised as the existing ones where they carry personal data); tests never hit the network.
- Identity resolution works across the old and the new data (same person before and after 2011 becomes one athlete; namesakes kept apart); athlete ids of existing athletes change as little as possible and the change is measured and reported.
- ELO: burn-in, K-factors, scale drift and provisional rules re-examined for the longer history (`elo --evaluate`); decisions recorded; the effect on today's ranking reported (how many of the top 100 move, by how much).
- Site: every text and rule that says or assumes 2011 follows the data (`meta.first_season`, about page, comparison caveats, README, SPEC); deploy guard baseline handled deliberately (the site grows — the owner accepts it once).
- Publication rules hold: withheld athletes unchanged; no names or ids in logs above DEBUG.
- Whole suite green, `check-site` passes, phase reviewed.

## Tasks
- [x] 1. (data-engineer) Survey: page through the pre-2011 listing (cached), download a small sample of `field_event_pdf` files across years and categories, establish what they contain; write the finding and a go / no-go per year range here. **Stop after this task if no year before 2011 carries bouts.**
- [x] 2. (data-engineer) Crawler: festival listing and PDF download for the usable years, with the gentler backfill pacing; run it; counts per year and category
- [ ] 3. (data-engineer) Parser for the old PDF layouts; fixtures and tests; parse success per year; rejects documented
- [ ] 4. (data-engineer) Identity resolution across old and new data; id stability measured
- [ ] 5. (data-engineer) `from_year` and everything that depends on it; `clean` output; counts
- [ ] 6. (elo-modeler) Re-evaluate burn-in and parameters on the longer history; record decisions; effect on current ranking
- [ ] 7. (web-builder) Texts and rules that assume 2011; guard baseline; README / SPEC / about page
- [ ] 8. phase-reviewer

## Handoff notes

- 2026-10-04 — **Task 1 done (survey) — GO: the pre-2011 "Statistik" PDFs carry bouts in every year 2001–2010; complete sheets from about 2004/2005, extracts of the top ranks before.** Nothing built yet; scripts only in the scratch directory.
  - **Requests: 136, all to schlussgang.ch** with the project User-Agent through `HttpClient`, 2–4 s apart (jitter), no retry configured, 0 errors, everything cached in `data/raw/`: 2 robots.txt, 80 listing pages (the crawler's own query per category × year, 1995–2010 — so task 2 gets them from the cache), 54 PDFs. robots.txt re-checked 2026-10-04: `www.schlussgang.ch` `Allow: /`; `backend-api.schlussgang.ch` standard Drupal file, neither `/jsonapi/` nor `/sites/default/files/` disallowed, no Crawl-delay. **No request to esv.ch.** No schlussgang response links to esv.ch for results: the API only carries the numeric `field_event_esv_id` (110 of the 381 events), which is stored as before and not followed.
  - **Listing:** 1995–2000 return nothing (30 empty queries; I could have asked once without a category instead — noted). **2001–2010: 381 events, Kranzfeste only** — no Regional event (tid 15) exists before 2011, so the old seasons have 37–39 festivals each against ~140 from 2012. Classified with the existing rules: 379 active, 2 youth (Eidg. Nachwuchsschwingertag 2006, 2009), 0 skipped, 0 cancelled. `field_event_association`, `field_event_type`, `field_event_state` are empty on every old event. All 719 files are `field_event_pdf` items on `www.schlussgang.ch`: 375 "Schlussrangliste", 337 "Statistik", 4 "Ranglistenauszug und Statistikauszug", 2 without description, 1 "Steinstossen".

    | year | events | active | ESAF tier | Bergkranz | Teilverband | Kantonal + Gau | with Statistik PDF | without |
    |---|---|---|---|---|---|---|---|---|
    | 2001 | 38 | 38 | 1 (ESAF Nyon) | 6 | 5 | 26 | **10** | 28 |
    | 2002 | 39 | 39 | 1 (Kilchberg) | 7 (incl. Expo-Schwinget Murten) | 5 | 26 | 39 | 0 |
    | 2003 | 37 | 37 | 0 | 6 | 5 | 26 | 34 | 3 |
    | 2004 | 38 | 38 | 1 (ESAF Luzern) | 6 | 5 | 26 | 36 | 2 |
    | 2005 | 38 | 38 | 0 | 6 | 5 | 27 | 35 | 3 |
    | 2006 | 39 | 38 | 1 (Unspunnen) | 6 | 5 | 26 | 36 | 2 |
    | 2007 | 38 | 38 | 1 (ESAF Aarau) | 6 | 5 | 26 | 37 | 1 |
    | 2008 | 38 | 38 | 1 (Kilchberg) | 6 | 5 | 26 | 37 | 1 |
    | 2009 | 38 | 37 | 0 | 6 | 5 | 26 | 37 | 0 |
    | 2010 | 38 | 38 | 1 (ESAF Frauenfeld) | 6 | 5 | 26 | 38 | 0 |

    339 active festivals have a statistic PDF by the existing picker (337 "Statistik" + 2 picked by file name). The 40 without are 28 × 2001 and 12 south-west / Bern-Jura festivals 2003–2008 (Waadtländer, Neuenburger, Genfer, Südwestschweizer 2003 and 2006, Bern-Jurassisches 2006–2008).
  - **Sample: 54 PDFs** (47 "Statistik": per year the eidg. event if any, 1 Bergkranz, 1 Teilverband, 2 Kantonal/Gau, drawn at random with a fixed seed; plus 7 of the other kinds). Text extracted with the pipeline's PDFium extractor and run through the existing parser unchanged.
    - **Every one of the 47 Statistik files prints bouts**: per athlete the Gänge with sign (`+` / `-` / `o` or `0`), opponent and grade — the same information as 2011+, each bout from both sides when both athletes are printed. All have a text layer.
    - **The other kinds carry no bouts**: "Schlussrangliste" = rank, points, name (2001–2004 only the top ranks on one page); the two "Ranglistenauszug und Statistikauszug" sampled are scans without text (2.4 and 1.9 MB, 0 characters); the file without description is a ranking. A festival without a "Statistik" file has no bouts on schlussgang.
    - **Completeness is the dividing line** (athletes printed per sampled sheet, eidg. event first):
      | year | athletes printed in the sampled sheets | reading |
      |---|---|---|
      | 2001 | ESAF 147 · 10, 7, 13, 12 | extracts: only the first ranks, 2–3 pages |
      | 2002 | Kilchberg 45 (whole field) · 22, 18, 29, 21 | extracts, 1–2 pages |
      | 2003 | 16, **157**, 31, 22 | 1 of 4 complete (Nordostschweizer) |
      | 2004 | ESAF 280 · 56, 12, **111**, **159** | 2 of 4 complete, 56 unclear |
      | 2005 | 61, **156**, **152**, **90** | 3 of 4 complete |
      | 2006 | **91**, **120**, **100**, **111** | complete |
      | 2007 | ESAF 279 · **121**, **159**, **101**, **164** | complete |
      | 2008 | Kilchberg 60 · **90**, **201**, **133**, **149** | complete |
      | 2009 | **62**?, **201**, **145**, **102** | complete (Stoos 62 to be checked) |
      | 2010 | ESAF 138 · **120**, **228**, **95**, **190** | complete |
      These are newspaper tables (footer "(c) 2002 schwingfeste.ch", later "© Bruno Heller"); an extract prints the 7–31 best-ranked athletes with all their Gänge. A bout between two printed athletes appears twice and can be mirror-checked; a bout against an athlete who is not printed appears once (sign and the printed athlete's grade, no grade for the opponent). Even "complete" sheets print only the athletes who wrestled all six Gänge in several layouts (e.g. Thurgauer 2004: 111 athletes × 6 = 666 entries, 94 of them against athletes not printed, i.e. eliminated after Gang 4) — in 2011+ everyone is printed.
    - **Layouts found** (existing parser on the 47: 3 ok, 20 partial, 24 failed — mostly layout, not content): (a) three or two athletes per row with names (2002 Kilchberg, 2003–2007 NOS/Bern/Rigi; existing `multicol`, works); (b) the same but with the header names in a separate text run at the end of the page (2002–2003 extracts: header reads `1 58.50 2 57.50 3a 57.25`) — needs the positional extractor (`pdf_layout`); (c) 2001 "Notenblätter": two athletes per row, signs+names and grades in separate runs; (d) ESAF 2001 two columns, points before the name, `O` as loss sign, decimal comma; (e) ESAF 2004 one column `1. 77.75 Name ** S`; (f) "Notenblätterdetails" with start numbers and upper-case names, three per row (Weissenstein 2005, Basel 2006) or one per row (Basel 2007/2009, Aargau 2008); (g) south-west 2004 `+ 10,00 Name` (grade before the name); (h) Zug 2005 / Freiburg 2006 / Schwarzsee 2007 variants of multicol with club and status columns; (i) Heller tables 2008–2010 (`standard`, `rang`, `blocks`) — the 2011 layouts, parse today.
  - **Go / no-go per year range:** 2005–2010 **go** (complete sheets in the sample, 35–38 of 38 Kranzfeste with a Statistik file); 2004 **likely go** (ESAF complete, 2 of 4 others complete); 2002–2003 **extracts only** — bouts exist, but only those of the first 15–30 ranks, mostly one-sided; 2001 **no-go as a season** (10 of 38 festivals have a file, 4 of the 5 sampled are extracts of ~10 athletes). The provisional earliest rateable season is **2005, possibly 2004**; it is fixed in task 3 on all files, by the share of a season's Kranzfeste that are present and complete. All 339 statistic PDFs of 2001–2010 are downloaded in task 2 (285 files still to fetch, small: 7–290 KB) because whether a sheet is complete can only be seen in the file, and so that the extract years are measured rather than guessed.
  - **For the later tasks:** Regional festivals do not exist before 2011 (the 2011 gap of the Phase 1 note continues backwards) — the old seasons are Kranzfest-only, which changes who appears (few non-Kranz athletes) and matters for burn-in (task 6). `GAU_RE` misses the spellings "Berner Jurassisches" (2001, 2007) and "Bern-Jurasisches" (2006) → mapped Kantonal instead of Gauverband (same K; fixed in task 2). "Expo-Schwinget Murten 2002" is filed under Bergkranzfest by schlussgang and not in the reference list — owner to confirm its tier (open question).
- 2026-10-04 — **Task 2 done: backfill mode in the crawler; 2001–2010 listed and all PDFs downloaded.**
  - **`crawl --backfill`** (with `--from-year` / `--to-year`), settings in `src/config.py`: `backfill_delay_min/max` = **2.0–4.0 s** with jitter (at most ~20 requests a minute; the normal crawl stays 0.5–1.0 s and `MIN_REQUEST_DELAY` = 0.5 is untouched — a backfill delay below the normal one is a config error), `backfill_max_requests` = **400 per run**, one cap for listings, statistic and ranking PDFs together, `backfill_max_errors` = 3. In this mode the client (`client_from_config`) makes at most 2 attempts per file instead of 4 and raises `Blocked` on **HTTP 403 / 429 without any retry**; `cmd_crawl` then stops the whole run (no next file) and says so. Only festivals of the given year range are looked at (the 2011+ PDFs, incl. the current season's re-checks, are not touched), no portraits, no interim sheets, `--refresh` is refused. Same User-Agent, same cache, nothing fetched twice. A run that hits the cap exits 1 and logs how many files are still uncached; **re-running the same command continues** (cache). Files: `src/config.py`, `src/scraper/client.py` (`Blocked`, `block_statuses`), `src/cli.py` (`_backfill_range`, `_backfill_budget`, `_not_cached`), `src/scraper/statistic_pdfs.py` / `ranking_pdfs.py` (let `Blocked` through), `README.md` / `docs/SPEC.md` (one line each on the option). `CLAUDE.md` does not list the option (not edited by an agent — owner may add it under Commands).
  - **Category fix:** `GAU_RE` and the reference check now know "Berner Jurassisches" (2001, 2007) and "Bern-Jurasisches" (2006) → `Gauverband` (were `Kantonal`; same K-factor). No 2011+ festival changes category (festivals table 2011+ identical, verified against the copy of the database).
  - **Runs** (all with the project User-Agent, robots.txt checked in task 1, 0 retries, 0 errors, no 403 / 429, no request to esv.ch):
    | run | command | requests | what |
    |---|---|---|---|
    | survey (task 1) | scratch scripts | 136 | 2 robots.txt, 80 listing pages, 54 PDFs |
    | 1 | `python -m src.cli crawl --backfill --from-year 2001 --to-year 2010` | 400 (cap) | 0 listings (50 cache hits), 291 statistic PDFs, 109 ranking PDFs; exit 1 at the cap, 262 ranking PDFs left |
    | 2 | same command | 262 | 262 ranking PDFs; exit 0 |
    | check | same + `--offline` | 0 | 768 cache hits |
    **798 requests in total for the whole history**, about 3.1 s apart on average (run 1: 400 requests in 20 min 54 s; run 2: 262 in 13 min 26 s). Nothing is left to fetch: 339 statistic PDFs and 379 ranking PDFs of 2001–2010 are cached (`data/raw/` 540 → ~575 MB).
  - **Stored: 381 festivals 2001–2010** (379 active, 2 youth; `data/schwingen.db` festivals 2,037 → 2,418). Active festivals per year × category (w/stat = with a statistic PDF; every active festival has a ranking PDF):
    | year | ESAF tier | Bergkranz | Teilverband | Kantonal | Gauverband | total | w/stat |
    |---|---|---|---|---|---|---|---|
    | 2001 | 1 | 6 | 5 | 20 | 6 | 38 | 10 |
    | 2002 | 1 | 7 | 5 | 20 | 6 | 39 | 39 |
    | 2003 | 0 | 6 | 5 | 20 | 6 | 37 | 34 |
    | 2004 | 1 | 6 | 5 | 20 | 6 | 38 | 36 |
    | 2005 | 0 | 6 | 5 | 21 | 6 | 38 | 35 |
    | 2006 | 1 | 6 | 5 | 20 | 6 | 38 | 36 |
    | 2007 | 1 | 6 | 5 | 20 | 6 | 38 | 37 |
    | 2008 | 1 | 6 | 5 | 20 | 6 | 38 | 37 |
    | 2009 | 0 | 6 | 5 | 20 | 6 | 37 | 37 |
    | 2010 | 1 | 6 | 5 | 20 | 6 | 38 | 38 |
    ESAF tier: ESAF 2001 / 2004 / 2007 / 2010, Kilchberg 2002 / 2008, Unspunnen 2006. Reference check: 0 mismatches, 1 festival not in the reference list (Expo-Schwinget Murten 2002, stored as Bergkranz — open question in STATE). No Regional festival, no `association`, `event_type` or `esv`-independent field on the old events; `event_flags` empty everywhere.
  - **Tests** (offline, MockTransport): 403 / 429 stop without retry and are not cached; 429 is still retried in the normal crawl; backfill client = slower, two attempts, same User-Agent; config validation (floor, order, `--refresh`); the CLI run sleeps 2–4 s between requests, shares one cap between listings and PDFs, resumes after the cap without fetching a file twice, stops at the first refusal and after repeated errors, and leaves other seasons alone; the three Bern-Jura spellings. Suite: 1,225 → 1,288 passed (this includes the parser tests of task 3, written while the crawl ran).
  - `src/cli.py` / `src/pipeline/cleaner.py` already carry the `from_year` filter of `clean` (task 5): with the default still 2011 the outputs are unchanged, the old seasons are in SQLite only.
