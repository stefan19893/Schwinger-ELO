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
- [ ] 1. Core update (expected score, win/draw/loss, K by category) + unit tests
- [ ] 2. MoV multiplier + tests
- [ ] 3. Season mean reversion + inactivity/provisional flag + tests
- [ ] 4. Wire `cli elo`; run over full history, write `ratings.parquet`
- [ ] 5. Calibrate `alpha` / `BaselineDiff` (e.g. predictive log-loss on later seasons); log decision
- [ ] 6. Sanity tests with known elite athletes; summarize top-20 per era in handoff notes

## Handoff notes

## Known inputs from Phase 2 review
Notes only (2026-10-01, Phase 2 review fixes; data = `data/schwingen.db`, parser v2).
- **Gang order is approximate.** `gang_collision` (an athlete has two bouts in the same Gang) affects ~2 % of bouts in every era (10,605 of 491,669), plus `gang_uncertain` 4,845 / `gang_inferred` 1,446, because the sources list some bouts out of order (Corcelles 2024: Pellaton has Burger as his 4th bout, Burger has Pellaton as his 3rd). Recommendation: update once per festival from pre-festival ratings, or at most by Ausstich phase (Gänge 1–4, then 5+), not strictly sequentially by `gang_nr`.
- **Bouts with a NULL grade are outcome-only** (user decisions 2026-10-01; parser v3: 730 `extra_bout`, 16 `one_sided` Schlussgang bouts built from the winner's entry because the sheet omits the loser's line, 2 `grade_missing`; 738 bouts with a NULL grade): use the outcome, skip the MoV multiplier (λ = 1, SPEC §4.2). Their `gang_nr` is the opponent's position (often the last Gang); extra bouts are excluded from `gang_collision`. Several are the Schlussgang itself (the Schlussgang loser's line printed as a surplus 0.00 / no-grade entry).
- **A is always the better-ranked athlete on the sheet** (lower sheet index; WIN_A 69.6 %, WIN_B 10.1 %, DRAW 20.3 %): do not treat A/B asymmetrically (no home advantage, no ordering prior).
- **Coverage:** ESAF 2013 is ~70 % complete (640 bouts vs ~905–919 at other ESAFs; the final sheet and the fetched "nach 4 Gängen" interim sheet both omit the 77 athletes eliminated after Gang 4); 2011–2014 coverage is weaker (paired entries 95–97.6 % vs ≥ 98.5 % later; several truncated sheets list only the top ranks; 2011 has only 3 Regional festivals on schlussgang). `festival_parse.n_gaenge` gives each festival's derived Gang count (5-Gang festivals exist).
- `bouts.schlussgang` is NULL (unknown) except on the 19 sheets that mark it — don't use it.
- Only festivals with `festivals.elo_eligible = 1` count (excludes `team`, `ausland`; user decision 2026-10-01).
