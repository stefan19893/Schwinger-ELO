# Phase 1 — Festival crawler (Milestone 1)
**Status:** blocked — awaiting user decision on data source / permission (see task 1 handoff note)
**Agent:** data-engineer

## Goal
Discover every festival 2011–present and persist it in SQLite.

## Exit criteria
- `src/scraper/client.py`: rate-limited (0.5–1.0 s), retrying (`tenacity`), disk-cached HTTP client.
- `src/scraper/fests_crawler.py` fills a `festivals` table matching the `Festival` schema in the spec.
- Festival `category` mapped to the spec's categories (ESAF, Bergkranz, Teilverband, Kantonal, Gauverband, Regional).
- `python -m src.cli crawl` wired up (`--from-year`, `--to-year`, `--refresh`).
- Tests run offline against fixtures in `tests/fixtures/`.

## Tasks
- [~] 1. Investigate sources: check robots.txt/terms, locate festival index + Notenblatt pages, document URL patterns in handoff notes; record primary-source decision in `STATE.md`
- [ ] 2. Save representative HTML fixtures (index page, one festival page per category)
- [ ] 3. Implement `client.py` (rate limit, retries, cache in `data/raw/`) + tests
- [ ] 4. Define SQLite schema (`festivals`) + persistence helper
- [ ] 5. Implement `fests_crawler.py` (incremental: skip known festivals) + tests
- [ ] 6. Wire `cli crawl` + test
- [ ] 7. Run full crawl 2011–present; record counts per year/category in handoff notes

## Handoff notes

- 2026-09-29 — **Task 1 (investigation done, BLOCKED — task left `[~]` until the user decides on the source).**
  - **robots.txt:** `www.schlussgang.ch` → `User-agent: * / Allow: /`. `backend-api.schlussgang.ch` (Drupal) → standard Drupal robots, `/jsonapi/` not disallowed. `esv.ch` → disallows only `/test/ /audio/ /images/ /etat/` (+ malformed `Disallow /…_hauptnavigation` lines); `/ranglisten/` not disallowed.
  - **esv.ch terms — prohibit crawling.** `https://esv.ch/nutzungsbedingungen/` ("Stand: 6. Juli 2026"): Ranglisten, Einteilungen, Statistiken, Live-Daten are claimed as ESV property; only personal, non-commercial use allowed. §4 forbids *without written ESV permission*: automated retrieval ("Scraping, Crawling, Bots oder vergleichbare Methoden"), use of the data in third-party applications/services, redistribution, and "die systematische Speicherung von Inhalten zur Erstellung eigener Datenbanken". Licence requests: ESV Geschäftsstelle, Rumendingenstrasse 1, 3423 Ersigen. Existing media partnerships/licences unaffected.
  - **schlussgang.ch terms:** no AGB/Nutzungsbedingungen page; `/impressum` copyright only says reprinting texts/ads requires written permission of the publisher (SCHLUSSGANG Medien AG, Luzern). No explicit crawling ban. **But** schlussgang's result data evidently comes from ESV: every event node carries `field_event_esv_id` (e.g. Schwägalp 2011 = 1258) and the frontend has `getEventByEsvId`. ESV claims ownership of Ranglisten data regardless of where they are shown, so crawling schlussgang instead would likely just route around the ESV terms (schlussgang is presumably an ESV media licensee).
  - **Stopped here per instructions**: no crawl, no fixtures saved, nothing written to `data/raw/`. Requests made during investigation (all with project UA, ≥0.7 s apart): esv.ch — robots, home, `/ranglisten/`, one `/ranglisten/?anlass=7515`, datenschutz, nutzungsbedingungen, impressum (the 3 content pages were fetched *before* the terms were read; they were only in the session scratchpad and have been deleted). schlussgang — robots (www + backend), sitemap index, pages + events sitemaps, home, `/resultate`, `/impressum`, 15 Next.js JS chunks (to find the data API), 1 event page, 1 JSON:API query.
  - **URL patterns found (for whenever a source is cleared):**
    - esv.ch year index: `https://esv.ch/ranglisten/?jahr=YYYY` — one page per year, `<tr id="anlassNNNN" class="aktiv|jung">` with `time[datetime]` (ISO date), `td.name`, `td.typ`, `td.ort`. Archive back to 1997. No category field. → 16 requests for 2011–2026.
    - esv.ch festival/"Notenblatt": `https://esv.ch/ranglisten/?anlass=ID` — Rangliste with per-Gang opponent, `+`/`-`/`o` and grade per athlete, plus Verband/Kanton (this is the bout source for Phase 2). Tabs Rangliste/Einteilung.
    - schlussgang JSON:API: `https://backend-api.schlussgang.ch/jsonapi/node/event?filter[field_category.tid]=<tid>&filter[d][condition][path]=field_event_date&…[operator]=BETWEEN&…[value][0]=YYYY-01-01&…[value][1]=YYYY-12-31&fields[node--event]=title,path,drupal_internal__nid,field_event_esv_id,field_event_date,field_category&page[limit]=50` (+`page[offset]`). Category tids: 11 Eidgenössische Anlässe, 12 Bergkranzfest, 13 Teilverbandsfest, 14 Kantonal-/Gaufest (Kantonal and Gau combined → would need a name heuristic), 15 Regionalfeste, 10 Jungschwingen, 16 Frauenschwingen; association tids 25 Ausland, 26 Bern, 27 Innerschweiz, 28 NOS, 29 NWS, 30 SWS. Est. ~100–150 requests for 2011–2026. Aborted festivals exist (e.g. "Weissenstein 2011 !!ABGEBROCHEN!!", no esv_id).
    - schlussgang event page: `https://www.schlussgang.ch/event/<slug>` (server-rendered Next.js; Schlussrangliste + Zwischenranglisten; per-bout data location on schlussgang not yet confirmed).
  - **Options for the user:** (a) ask ESV for written permission (non-commercial, derived ELO ratings published on GitHub Pages) — cleanest, then esv.ch is the natural primary source (official, complete, bout-level); (b) ask SCHLUSSGANG Medien AG and check whether their ESV licence covers third-party reuse; (c) use only manually provided data files (no crawler). Publishing derived ratings publicly probably needs explicit consent in any case (§4 "Nutzung der Daten in Applikationen … Dritter").
