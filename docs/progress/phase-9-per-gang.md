# Phase 9 — Rating change per Gang (feature request 2026-10-04)
**Agents:** elo-modeler (tasks 1–2), web-builder (tasks 3–6). Branch `phase-9-per-gang`, stacked on `phase-8-compare-by-bouts` (PR #10, open).

## Goal
On the comparison chart's "Gänge" axis a visitor sees an athlete's rating move Gang by Gang (up to about seven points per festival) instead of once per festival, and can read per Gang: opponent, result and what that Gang contributed.

## What the model allows (read before designing)
The engine updates once per festival (`elo_update_mode = "festival"`, confirmed 2026-10-03): every bout of a festival is scored against the *pre-festival* ratings and an athlete's changes are summed. So there is no engine state "rating after Gang 3". What exists is each bout's contribution; the contributions of a festival sum to `rating_after − rating_before`. The per-Gang line is therefore a decomposition: `before + running sum of contributions in Gang order`. The page must say so in plain words and must not present it as the rating the next opponent was scored against. **The ratings themselves must not change in this phase** (`ratings.parquet`, `athlete_ratings.parquet`, `season_ratings.parquet` byte-identical before and after).

## Privacy (decide cautiously, record, owner confirms)
A single bout's contribution is `K · MoV · (score − expected)`; with the public formula it reveals the opponent's pre-festival rating. For a *withheld* opponent (`publish_min_age`, unknown birth year + recent debut) that would export a rating value the site deliberately does not publish ("their festival rows carry no rating value"). Default for this phase: no individual contribution is exported for a bout against a withheld, `not_a_name` or unrated opponent; such Gänge of a festival are exported only as one combined remainder (count and summed contribution), which is what `after − before` minus the published bouts already implies today. Measure how many festival rows have exactly one such bout (there the remainder is that bout's value — already inferable today; say so in the notes). No withheld id, name, club or birth year in any new data; leak tests extended.

## Exit criteria
- Engine writes the per-bout contributions (new Parquet output or columns; contract documented in SPEC); per athlete and festival they sum to the festival change within rounding; existing rating outputs byte-identical; engine tests cover it.
- Exporter writes them on demand for the comparison page (extend `bouts_<id>.json` or a new per-athlete file; measure sizes on the real build and stay within the payload bounds of the tests, raising a bound only with a recorded reason); deterministic; other pages' payload unchanged; privacy rule above enforced by tests on the real build; deploy guard covers any new file family.
- `compare.html`, axis "Gänge": one point per Gang in Gang order, festivals still recognisable, the 1 April step unchanged; tooltip per Gang (festival, Gang, opponent when published, result, contribution, running value); combined remainder shown honestly; uncertain Gang order (`gang_uncertain`) said; a visible sentence explains that the rating is calculated per festival and the Gang points are its breakdown. The other three axes and time mode render as before.
- Works for 1–6 athletes on phone width without the chart becoming unusably dense (decide: always per Gang, or per festival until zoomed in / a sub-switch — record the choice).
- Tests: engine, JSON contract, privacy, page rules, browser smoke; whole suite green; `check-site` passes on sample and real build. Numbers checked against Parquet for several athletes.
- About page, README, SPEC updated.

## Tasks
- [ ] 1. (elo-modeler) Read the engine; design the per-bout output (name, columns, rounding, how MoV / K / flags enter); note it here
- [ ] 2. (elo-modeler) Implement + tests; prove existing outputs are byte-identical; record counts and file size
- [ ] 3. (web-builder) Data contract for the page incl. the privacy rule; measure sizes on the real build; note it here
- [ ] 4. (web-builder) Exporter + deploy guard + tests (contract, privacy on the real build)
- [ ] 5. (web-builder) `compare.js`: per-Gang series on the "Gänge" axis, tooltip, explanatory text, density handling; tests; verification against Parquet in a browser
- [ ] 6. (web-builder) About page, README, SPEC

## Handoff notes
