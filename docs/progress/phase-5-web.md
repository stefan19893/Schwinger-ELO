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
- [ ] 1. Define JSON contracts (document field names in handoff notes)
- [ ] 2. Implement `static_builder.py` + tests for output shape
- [ ] 3. Leaderboard + search page
- [ ] 4. Athlete profile page with ECharts career line
- [ ] 5. Festival lookup page
- [ ] 6. End-to-end test `tests/test_cli.py` (`all --sample`)
- [ ] 7. Local check via `./scripts/deploy_local.sh --sample` and with real data; verify all pages; record bundle sizes

## Handoff notes
