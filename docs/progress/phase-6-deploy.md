# Phase 6 — CI & deployment (Milestone 5)
**Agent:** web-builder

## Goal
Automated weekly update and GitHub Pages deployment.

## Exit criteria
- Workflows call only the CLI (`python -m src.cli …`), no logic duplicated in YAML.
- `.github/workflows/deploy_pages.yml`: install, test, `cli build`, `actions/deploy-pages@v4`.
- `.github/workflows/scrape_and_update.yml`: scheduled weekly during season, incremental crawl → rebuild → deploy.
- State persistence between runs decided and logged in `STATE.md`.
- `README.md` documents local usage (`deploy_local.sh` + flags) and hosted setup (enabling Pages).

## Tasks
- [ ] 1. Decide state persistence strategy for scheduled runs; log decision
- [ ] 2. Write `deploy_pages.yml`
- [ ] 3. Write `scrape_and_update.yml` (cron, polite crawl, caching)
- [ ] 4. Validate workflows (syntax/`actionlint` if available)
- [ ] 5. README: quick start with `./scripts/deploy_local.sh`, flags, CLI reference, Pages setup
- [ ] 6. Hand over to user for first push + enabling Pages (do not push yourself)

### Added by the main session's brief (2026-10-03), done before / between the tasks above
- [x] A. Publication defaults in config + exporter: `publish_min_age`, `site_noindex` (+ `robots.txt`), `contact_email`
- [~] B. Deploy guard in the CLI (`check-site`: empty / shrunken site against the last accepted `meta.json`)
- [ ] C. Headless-browser smoke test of the built sample site (CI must run it, locally it skips without a browser)

## Known inputs from Phase 5 review (2026-10-03)
- **Deploy by Pages artifact**, not by committing `dist/` (8,864 files, 51 MB; `actions/upload-pages-artifact` + `actions/deploy-pages@v4`). `dist/` stays git-ignored.
- **State between runs:** `data/raw`, `data/schwingen.db` and the Parquet files need a persistent store between scheduled runs (task 1). A cache miss must be treated as a **failure**, not as "start from scratch": a fresh runner without the cache would re-crawl everything (thousands of requests) or publish nothing.
- **Guard against an empty or shrunken site.** `build` already exits 1 without rating data (`build --allow-empty` is the explicit opt-in and must never be used in a workflow). In addition the deploy job should fail on `meta.empty` and compare `meta.counts` (athletes, ranked, festivals, bouts) with the `meta.json` of the currently deployed site, failing on a drop. This check belongs in the CLI, not in YAML.
- **`athlete_id` URLs are not stable.** Ids can change between runs (Phase 3) and the slugs fold ae / oe / ue and drop non-ASCII letters (e.g. `giger-samul-p878`). Decide the id scheme / a registry of issued ids before the site is linked publicly; today an outdated link lands on the not-found page with suggestions.
- **The JavaScript has no automated tests** (no Node / browser in pytest). Add a headless smoke test in CI: load the four pages against the built site, fail on the error banner (`#se-banner[data-error]`) and on an empty `#se-view`.
- **Owner decisions still open before any deployment** (do not deploy without them):
  - publication of ESV-derived data (results obtained via schlussgang.ch; ESV terms claim ownership);
  - 478 athletes born 2009–2010 are listed by name (345 of them ranked);
  - the only objection route is a public GitHub issue — consider an e-mail contact on `about.html`;
  - search-engine indexing: no `robots` meta tag and no `robots.txt` today, i.e. indexable once published.

## Handoff notes
- 2026-10-03 — **Task A done (web-builder): publication defaults.** Files: `src/config.py` (`publish_min_age = 18`, `site_noindex = True`, `contact_email = ""`, validated; env `SCHWINGEN_PUBLISH_MIN_AGE`, `SCHWINGEN_SITE_NOINDEX`, `SCHWINGEN_CONTACT_EMAIL`), `src/exporter/static_builder.py`, `web/js/app.js`, `web/js/about.js`, `web/about.html`, tests in `tests/test_static_builder.py` (+8) and `tests/test_web.py` (+1). 955 tests green. No CSS class added.
  - **Age filter.** Only the birth year is in the Parquet files, so "younger than 18 at the data date" cannot be decided for the cohort that turns 18 in the data year. Rule: withheld when `data year - birth year <= publish_min_age` (not *certainly* 18). A withheld athlete (`_Person.withheld`) counts in the ratings but has no history file, no search entry, no rank, no season / peak / namesake entry; a festival file keeps his row and bouts with `id`, `name`, `club`, `tv` = null and the new last column `anon` = 1 (`FEST_ATHLETE_COLS`; additive, `SCHEMA_VERSION` stays 1); the page shows "Jungschwinger, Name nicht veröffentlicht" (`SE.athleteLink`). W / D / L, grade sum and rating before / after of the row are still shown (the opponents' bouts need them). Withheld rows sort after named rows of equal grade points (not by name). Unknown birth year: cannot be filtered, stays; `about.html` says so (`#se-minors`, filled from `meta.publish` / `meta.counts.withheld*`).
  - **Ranks are re-numbered among the published athletes** (`_published_ranks`: engine rank minus the withheld athletes ahead; identical to `athlete_ratings.rank` when nobody is withheld). The same number is used in `rankings_latest.json`, `athletes.json` and the history file (tested). Reason: gaps would show "Rang 2020" in a list of 1,497 and each gap would point at a hidden person.
  - **Real data (run, as of 2026-09-27, data year 2026 → born 2008 or later withheld):** 718 athletes withheld, 523 of them ranked; published 6,314 athletes (was 7,032), 1,497 ranked (was 2,020); 7,521 anonymous rows in 390 festival files; first changed rank: 138 (was 139); the 200 highest peaks are unchanged; `dist/` 8,147 files, 50.1 MB. With the narrower reading "born 2009 or later" (the review's count) it would be 478 / 345: the 240 athletes born 2008 (178 ranked) are the difference. `publish_min_age = 0` gives the previous site (7,032 / 2,020; run).
  - **`site_noindex`:** `apply_indexing` puts `<meta name="robots" content="noindex">` into every page of `dist/` (after the charset tag; a page without `<head>` fails the build) and writes `robots.txt` (`Disallow: /`). The sources in `web/` stay free of the tag. **Limit:** crawlers read `robots.txt` only at the root of a host; under `<user>.github.io/Schwinger-ELO/` it is not consulted, so only the meta tag of the four pages works there, and the JSON data files cannot carry a tag at all (GitHub Pages sends no `X-Robots-Tag`). With a custom domain the file is at the root and takes effect.
  - **`contact_email`:** written to `meta.json` as `contact` (null when empty); `about.js` shows the hidden paragraph `#se-contact` with a `mailto:` link only when it is set. No address exists anywhere in the repository.
  - `meta.json` gained `counts.withheld`, `counts.withheld_ranked`, `publish {min_age, withheld_from_birth_year, noindex}`, `contact`. `build` logs the three settings and the withheld counts.
  - **Not checked in a browser yet** (label on a festival page, the two about paragraphs) — done with task C.
