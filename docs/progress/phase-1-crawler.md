# Phase 1 — Festival crawler (Milestone 1)
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
- [ ] 1. Investigate sources: check robots.txt/terms, locate festival index + Notenblatt pages, document URL patterns in handoff notes; record primary-source decision in `STATE.md`
- [ ] 2. Save representative HTML fixtures (index page, one festival page per category)
- [ ] 3. Implement `client.py` (rate limit, retries, cache in `data/raw/`) + tests
- [ ] 4. Define SQLite schema (`festivals`) + persistence helper
- [ ] 5. Implement `fests_crawler.py` (incremental: skip known festivals) + tests
- [ ] 6. Wire `cli crawl` + test
- [ ] 7. Run full crawl 2011–present; record counts per year/category in handoff notes

## Handoff notes
