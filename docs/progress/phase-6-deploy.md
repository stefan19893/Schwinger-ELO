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
