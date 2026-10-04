# Phase 8 — Comparison by number of bouts (feature request 2026-10-04)
**Agent:** web-builder

## Goal
On `compare.html` the visitor can switch the x-axis of the career chart from calendar time to the number of bouts ("Gänge"), so that careers are aligned by experience: "where was each athlete after his 100th bout?" — useful for athletes of different generations whose careers do not overlap in time.

## Design (decided with the owner's request; deviations go into the handoff notes)
- A two-way switch above the chart, German labels (e.g. "Zeit" / "Gänge"); default = time, as today. The choice is kept in the URL (e.g. `&x=gaenge`), shareable, back button works, as the selection already does.
- Bout axis: x = the athlete's cumulative number of bouts in the recorded data (sum of the history rows' `n`), y = rating after the festival. The rating changes per festival, so one point per festival at the cumulative count; the line starts at (0, first `before`).
- The 1 April reversion stays visible and unattributed to a festival: a vertical dashed step at the same x between `after` of one festival and `before` of the next. No idle tail in this mode.
- Tooltip: athlete, festival, date, "Gang a–b" of his career, before → after.
- Zoom buttons in this mode: whole range / common range (0 … the smallest bout total of the selection) instead of the year buttons.
- Honest caveats in visible text: the data begins in 2011, so for an athlete whose first recorded season is the first season of the data "Gang 1" is not his first career bout; everybody starts at the same initial rating. Show the note only when it applies to the selection.
- No new exported data expected: `history_<id>.json` already carries `n` per festival. If `n` turns out not to be what the axis needs (check against the header's `bouts`), stop and record it before changing the exporter.
- Publication rules untouched: nothing new is exported, withheld athletes stay unselectable.

## Exit criteria
- Switch works with 1–6 athletes, on phone width, in light and dark scheme, from a sub-path; URL state round-trips (load, toggle, back button).
- Time mode renders exactly as before.
- Numbers checked against Parquet for several athletes (cumulative bout count and rating at given festivals).
- Tests: page rules in `tests/test_web.py`, browser smoke test covers a bout-axis URL; whole suite green; sample and real build pass `check-site`.
- About-page text / README / SPEC mention the switch; `web/css/style.css` only rebuilt if new classes are really needed.

## Tasks
- [ ] 1. Read `web/js/compare.js` / `charts.js`; confirm the data suffices (`n` vs `bouts`); note the series design
- [ ] 2. Implement switch, URL state, bout-axis series, tooltip, zoom buttons, caveat note
- [ ] 3. Tests (page rules, browser smoke) and verification against Parquet on real data
- [ ] 4. About-page text, README, SPEC

## Handoff notes
