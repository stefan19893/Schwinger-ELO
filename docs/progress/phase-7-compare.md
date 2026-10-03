# Phase 7 — Athlete comparison (feature request 2026-10-03)
**Agent:** web-builder

## Goal
A page on which a visitor selects two or more athletes and compares them: key figures side by side, rating over time in one chart, seasons, direct bouts and common festivals.

## Exit criteria
- `compare.html` (linked from the navigation and from athlete profiles): select athletes through the existing search, selection kept in the URL (shareable), works from a sub-path with relative URLs, phone width.
- Side-by-side figures per athlete: current rating and rank, peak rating and date, career span, bouts, won–gestellt–lost, club, Teilverband, birth year; inactivity and identity-uncertain markers as elsewhere.
- One ECharts chart with all selected careers over time (1 April reversion not attributed to a festival, as on the profile).
- Season table (season-end rating and place per athlete and year, respecting burn-in / thin seasons and the 12-bout rule).
- Head-to-head: direct bouts between the selected athletes with tally and list (festival, date, Gang, result, grades), and the festivals both attended.
- Exported data stays deterministic; initial payload of the other pages does not grow noticeably; per-athlete data loaded on demand.
- Publication rules hold: withheld athletes (`publish_min_age`, unknown birth year + recent debut) are not selectable and never appear by name, id or rating in any new data or view; leak check over the real build passes; deploy guard and `noindex` cover the new page.
- Tests: JSON contracts, privacy, page checks, browser smoke test includes the new page.

## Tasks
- [ ] 1. Design the data contract for head-to-head / common festivals (measure sizes on real data); document in handoff notes
- [ ] 2. Exporter + tests
- [ ] 3. `compare.html` + JS: selection, URL state, figures, chart, seasons
- [ ] 4. Head-to-head and common festivals
- [ ] 5. Navigation / profile links, about-page text, README / SPEC
- [ ] 6. Verification on sample and real data (browser, leak check, sizes); record results

## Handoff notes
