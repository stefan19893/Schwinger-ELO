# Phase 4 — ELO engine (Milestone 3b)
**Agent:** elo-modeler

## Goal
`SchwingElo` engine implementing spec §4.2 with full rating history.

## Exit criteria
- `src/pipeline/elo_engine.py`: expected score, draws (S=0.5), MoV multiplier, festival K-factors, April mean reversion (δ≈0.10, mean 1500), provisional flag after >1.5 inactive seasons.
- Bouts processed in chronological order (date, then gang_nr); deterministic output.
- `alpha` and `BaselineDiff` chosen with documented reasoning in `STATE.md` Decisions.
- `tests/test_elo.py`: unit tests for each rule + sanity tests on real data (elite athletes such as Glarner, Wicki, Reichmuth, Forrer rank near the top in their peak seasons).
- Parameters live in `src/config.py`.
- `python -m src.cli elo` writes `data/processed/ratings.parquet` (athlete, date, fest_id, rating_before, rating_after); works with `--sample`.

## Tasks
- [x] 1. Core update (expected score, win/draw/loss, K by category) + unit tests
- [x] 2. MoV multiplier + tests
- [~] 3. Season mean reversion + inactivity/provisional flag + tests
- [ ] 4. Wire `cli elo`; run over full history, write `ratings.parquet`
- [ ] 5. Calibrate `alpha` / `BaselineDiff` (e.g. predictive log-loss on later seasons); log decision
- [ ] 6. Sanity tests with known elite athletes; summarize top-20 per era in handoff notes

## Handoff notes
- 2026-10-03 — **Task 1 done** (elo-modeler). `src/pipeline/elo_engine.py`: `EloParams` (frozen, `from_config`), pure formulas (`expected_score`, `mov_multiplier`, `bout_multiplier`, `revert_to_mean`, `rating_year`, `k_factor`), `SchwingElo` with the step-by-step reference (`rate_bout`, `apply_season_reversion`) and the vectorised `run(bouts) -> EloResult` (`history` = one row per athlete and festival with `rating_before` / `rating_after` + `season`, `category`, `n_bouts`, `score`, `expected`, `bouts_before`, `days_inactive`, `provisional`, `provisional_reason`; `bouts` = per-bout expected scores for evaluation; `ratings`; `as_of`). Four update modes (`festival` / `phase` / `gang` / `sequential`, config `elo_update_mode`) so task 5 can compare them; default `festival` for now. The whole file (incl. MoV and reversion code) was written in one go; tasks 2 and 3 add their tests and any fixes. `src/config.py`: new `elo_update_mode`, `elo_phase_split_gang`, `mov_lambda_min/max`, `provisional_min_bouts`. `tests/test_elo.py`: 40 tests (formula, W/D/L, K per category, zero-sum, A/B symmetry incl. partially mirrored tables, row-order independence, sequential run == bout-by-bout reference, festival mode independent of `gang_nr`, input validation). First real-data smoke run: 491,597 eligible bouts, 173,153 history rows, ~1.2 s per run in every mode. Unknown category / outcome, self-bouts and duplicate bout ids raise `ValueError` (CLI turns that into exit 1).
- 2026-10-03 — **Task 2 done.** MoV multiplier `lambda = clamp(1 + alpha * (grade_winner - grade_loser - baseline_diff), mov_lambda_min, mov_lambda_max)` (`mov_multiplier` / `bout_multiplier` in `elo_engine.py`; clamp 0.5..2.0 in `src/config.py`): wins only, always the winner's grade minus the loser's (so WIN_B bouts mirror WIN_A bouts), draws and bouts with a NULL grade get lambda = 1; the delta is applied with opposite signs, so a bout stays zero-sum. 14 new tests. `alpha` / `baseline_diff` are still the neutral placeholders (0 / 0) until task 5. Data note for task 5: among graded wins the margin takes only three values — 1.00 (9.75 : 8.75, 4.4 %), 1.25 (10 : 8.75 or 9.75 : 8.50, 45.4 %), 1.50 (10 : 8.50, 48.9 %) — plus ~30 misprints (margins <= 0 or 1.75) that the clamp keeps harmless.

## Known inputs from Phase 3 review
Notes only (2026-10-03, phase-reviewer + review fixes; data = `data/processed/*.parquet` written by `python -m src.cli clean`, resolver `evidence`; 7,397 athletes, 491,676 bouts, 174,176 raw rows).
- **Keep all bouts, rate everything.** Carry an identity-uncertainty marker per athlete (e.g. share of rows with `identity_map.confidence` ≤ 0.4, or the notes in `athletes.evidence`: `ambiguous_rows=N`, `unbridged_gap_rows=N`, `career_gap=N`, `fragment`, `teilverbaende=…`) and run **one sensitivity pass without the confidence ≤ 0.4 rows** (1,416 rows: 884 `…|ambiguous`, 384 plain `name` rows of names with namesakes, 26 `…|gap` only, 122 `not_a_name`) to see how much the ratings move.
- **Exclude** `not_a_name` athletes (122; `athletes.evidence == "not_a_name"`, single-letter / garbage names, one athlete per row) and athletes with 0 bouts (356; rows of sheets whose bouts were not imported).
- **1,501 one-row athletes** (one festival only) need a provisional status / minimum-bout threshold before they appear in rankings.
- **`athlete_id` can change between runs** when a portrait is attached or detached (`<name>-p<portrait id>` ↔ `<name>-<fest_id>-<idx>`) or the earliest row moves: key ratings on the current run only, never join ratings of one run to athletes of another.
- **Bout orientation:** A = the better-ranked athlete of the sheet (WIN_A 342,116 / WIN_B 49,576 / DRAW 99,984) — no A/B asymmetry in the model (see the Phase 2 note below).
- **An athlete appears twice in the same festival and Gang in 6,120 cases** — from the Phase 2 Gang inference (`gang_collision` etc.), not from identity resolution (0 athletes have two rows in one festival or on one date; the resolver raises otherwise).
- **Identities to keep marked as unreliable** (the split between the two persons is not decidable from the data; ≥ 10 ambiguous rows each): `gasser-dominik-p1002` (103 of 116 rows) / `gasser-dominik-p822` (39 of 57) — same club and village, b. 1998 / 1996; `odermatt-remo-26422-103` (42 / 82); `ming-christian-p769` (37 / 108) / `ming-christian-25249-092` (11 / 49); `herger-elias-p2544` (36 / 44) / `herger-elias-p3398` (9 / 10); `grab-martin-p915` (34 / 57) / `grab-martin-p37537`; `schuler-alex-p682` (32 / 159) / `schuler-alex-p9246`; `odermatt-sepp-p19774` (27 / 35); `burch-jonas-p907` (18 / 134); `betschart-fabian-p1891` (16 / 20); `wuthrich-jonas-p1712` (14 / 35); `vogler-jonas-p1285` (14 / 82); `bucher-thomas-p773` (13 / 168); `heinzer-stefan-26429-026` (12 / 66) / `heinzer-stefan-p646` (11 / 88); `gwerder-andreas-p647` (12 / 140); `herger-simon-26422-154`, `betschart-silvan-p744`, `gwerder-christian-26429-016`, `arnold-andreas-26424-067`, `zimmermann-martin-p52979`. In total 168 athletes carry `ambiguous_rows`, 9 `unbridged_gap_rows`, 79 are `fragment`s (≤ 2 rows inside a namesake's career), 109 have a `career_gap` ≥ 5 seasons (possible father / son with the same club and village).
- Athlete attributes: `birth_year` 71.8 %, `club` 71.2 %, `sub_association` 93.9 %, `slug` 48.6 %; the club is the most frequent one of the athlete's rows (not necessarily the current one).

## Known inputs from Phase 2 review
Notes only (2026-10-01, Phase 2 review fixes; data = `data/schwingen.db`, parser v2).
- **Gang order is approximate.** `gang_collision` (an athlete has two bouts in the same Gang) affects ~2 % of bouts in every era (10,605 of 491,669), plus `gang_uncertain` 4,845 / `gang_inferred` 1,446, because the sources list some bouts out of order (Corcelles 2024: Pellaton has Burger as his 4th bout, Burger has Pellaton as his 3rd). Recommendation: update once per festival from pre-festival ratings, or at most by Ausstich phase (Gänge 1–4, then 5+), not strictly sequentially by `gang_nr`.
- **Bouts with a NULL grade are outcome-only** (user decisions 2026-10-01; parser v3: 730 `extra_bout`, 16 `one_sided` Schlussgang bouts built from the winner's entry because the sheet omits the loser's line, 2 `grade_missing`; 738 bouts with a NULL grade): use the outcome, skip the MoV multiplier (λ = 1, SPEC §4.2). Their `gang_nr` is the opponent's position (often the last Gang); extra bouts are excluded from `gang_collision`. Several are the Schlussgang itself (the Schlussgang loser's line printed as a surplus 0.00 / no-grade entry).
- **A is always the better-ranked athlete on the sheet** (lower sheet index; WIN_A 69.6 %, WIN_B 10.1 %, DRAW 20.3 %): do not treat A/B asymmetrically (no home advantage, no ordering prior).
- **Coverage:** ESAF 2013 is ~70 % complete (640 bouts vs ~905–919 at other ESAFs; the final sheet and the fetched "nach 4 Gängen" interim sheet both omit the 77 athletes eliminated after Gang 4); 2011–2014 coverage is weaker (paired entries 95–97.6 % vs ≥ 98.5 % later; several truncated sheets list only the top ranks; 2011 has only 3 Regional festivals on schlussgang). `festival_parse.n_gaenge` gives each festival's derived Gang count (5-Gang festivals exist).
- `bouts.schlussgang` is NULL (unknown) except on the 19 sheets that mark it — don't use it.
- Only festivals with `festivals.elo_eligible = 1` count (excludes `team`, `ausland`; user decision 2026-10-01).
