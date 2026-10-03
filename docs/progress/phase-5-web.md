# Phase 5 — Exporter & frontend (Milestone 4)
**Agent:** web-builder

## Goal
Static site in `dist/` with leaderboards, search, athlete profiles and festival lookup.

## Exit criteria
- `python -m src.cli build` (via `src/exporter/static_builder.py`) writes `dist/` with `athletes.json` (search index), `rankings_latest.json`, top-200 all-time, peak per season, `history_<id>.json` per athlete.
- Pages: `index.html` (leaderboard + search), `athlete.html` (ECharts career chart), `fests.html`.
- Works when served from a sub-path (`/Schwinger-ELO/`) — relative URLs only.
- `./scripts/deploy_local.sh --sample` on a fresh clone shows a working site; `tests/test_cli.py` checks `all --sample` produces a valid `dist/`.
- Responsive on phone width; loads per-athlete data on demand.

## Tasks
- [x] 1. Define JSON contracts (document field names in handoff notes)
- [~] 2. Implement `static_builder.py` + tests for output shape
- [ ] 3. Leaderboard + search page
- [ ] 4. Athlete profile page with ECharts career line
- [ ] 5. Festival lookup page
- [ ] 6. End-to-end test `tests/test_cli.py` (`all --sample`)
- [ ] 7. Local check via `./scripts/deploy_local.sh --sample` and with real data; verify all pages; record bundle sizes

## Handoff notes
- 2026-10-03 — **Task 1 done (web-builder): JSON contracts.** Designed from the real Parquet schemas (`data/processed/`, ratings regenerated with K x 2 / δ 0.05; no code yet). All files live under `dist/data/`; compact JSON (no whitespace, UTF-8, `null` for NaN / None, dates `YYYY-MM-DD`); tables are `{"cols": [...], "rows": [[...], ...]}` to keep payloads small. Ratings are rounded to integers in lists and to one decimal in the per-athlete history.
  - **Exportable athlete** = has rated bouts (`athlete_ratings.rating` not null) and `athletes.evidence != "not_a_name"`. Only these get a history file, appear in the search index and are linked; other names inside a festival are shown without a link (`id: null`). Exported attributes: name, club, Teilverband, birth year — no residence, birthday, licence number, portrait slug.
  - `meta.json` — `schema`, `sample`, `as_of` (date of the last rated festival; no build timestamp, so builds are deterministic), `first_season`, `last_season`, `counts {athletes, ranked, festivals, bouts, history_rows}`, `model {initial, mean, delta, k_scale, k {category: effective K}, provisional_min_bouts, inactive_days, first_ranked_season, season_min_bouts, thin_season_festivals}`.
  - `rankings_latest.json` — `as_of`, cols `rank, id, name, club, tv, by, rating, peak, last, idle, bouts, unc`: every athlete with `ranked` (never provisional / not_a_name / without bouts), by `rank`. `last` = date of the last bout, `idle` = days since then, `unc` = 1 if `identity_uncertain`.
  - `athletes.json` (search index, loaded on first use of the search field) — cols `id, name, club, tv, by, first, last, rating, peak, rank, flags`, sorted by name, id. `first` / `last` = first / last season; `flags` bitmask 1 ranked, 2 `few_bouts`, 4 `inactive`, 8 `identity_uncertain`.
  - `alltime_top200.json` — `as_of`, cols `pos, id, name, club, tv, by, peak, date, fest_id, fest, flags`: 200 highest `rating_peak` (Phase 4 definition: after a festival, athlete no longer `few_bouts`, 2011 excluded), with date and festival of the peak.
  - `seasons.json` — `min_bouts`, `seasons: [{season, status, n_festivals, n_athletes, n_listed, peak {id, name, rating}, cols, rows}]`, newest first; cols `pos, id, name, club, tv, by, rating, peak, bouts, unc` (season-end rating, season peak, season bouts), top 100. `status`: `ok`, `current` (season of `as_of`), `burn_in` (2011), `thin` (fewer rated festivals than `thin_season_festivals`, i.e. 2020). `pos` is the place among athletes that are `ranked` in `season_ratings` **and** have at least `min_bouts` season bouts; it is `null` for `burn_in` / `thin` seasons (list by rating only, shown behind a caveat). `peak` = highest rating reached in the season by a listed athlete ("peak per season").
  - `festivals.json` — cols `id, name, date, cat, eidg, loc, athletes, bouts, status`, newest first: all active, not cancelled festivals. `status`: `ok`, `partial` (sheet incomplete), `unrated` (bouts shown, not counted: team / Ausland), `none` (no result sheet could be read; no detail file).
  - `fests/fest_<fest_id>.json` (on demand) — `id, name, date, season, category, eidg_type, location, url` (schlussgang event page = source link), `status`, `n_gaenge`, `athletes {cols: id, name, club, tv, before, after, w, d, l, pts, unc}`, `bouts {cols: gang, a, b, res, ga, gb, flags}`. `a` / `b` index into `athletes.rows`; `res` 1 = a won, 0 = gestellt, 2 = b won; `ga` / `gb` grades or null; bout `flags` bitmask 1 Schlussgang (only when the sheet marks it — NULL and False are both "not marked"), 2 `extra_bout`, 4 grade not printed (`grade_missing` / `one_sided`), 8 Gang number uncertain. `pts` = sum of the printed grades (not an official rank). `before` / `after` null for unrated festivals.
  - `history/history_<athlete_id>.json` (on demand, one per exportable athlete) — `id, name, club, tv, by, first, last, bouts, festivals, record [w, d, l], rating` (stored current rating, keeps reverting), `rating_last` (after the last festival), `last_date`, `idle`, `peak, peak_date, peak_fest {id, name}`, `rank, ranked, provisional` (list of reasons), `unc`, `unc_rows [low-confidence rows, all rows]`, `namesakes [{id, name, club, tv, by, first, last}]` (other exportable athletes with the same name), `rev [delta, mean]` (for drawing the 1 April reversion), `as_of`, `seasons {cols: season, rating, peak, bouts, pos}`, `history {cols: date, fest_id, fest, cat, before, after, n, score, exp, flags}` (flags 1 = provisional at that festival, 2 = return after > 1.5 seasons).
  - **Sizes** are measured in task 2 / 7. Expected file count on real data: ~7,000 history + ~1,800 festival files + 6 index files.
