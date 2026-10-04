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
- [ ] 1. (data-engineer) Survey: page through the pre-2011 listing (cached), download a small sample of `field_event_pdf` files across years and categories, establish what they contain; write the finding and a go / no-go per year range here. **Stop after this task if no year before 2011 carries bouts.**
- [ ] 2. (data-engineer) Crawler: festival listing and PDF download for the usable years, with the gentler backfill pacing; run it; counts per year and category
- [ ] 3. (data-engineer) Parser for the old PDF layouts; fixtures and tests; parse success per year; rejects documented
- [ ] 4. (data-engineer) Identity resolution across old and new data; id stability measured
- [ ] 5. (data-engineer) `from_year` and everything that depends on it; `clean` output; counts
- [ ] 6. (elo-modeler) Re-evaluate burn-in and parameters on the longer history; record decisions; effect on current ranking
- [ ] 7. (web-builder) Texts and rules that assume 2011; guard baseline; README / SPEC / about page
- [ ] 8. phase-reviewer

## Handoff notes
