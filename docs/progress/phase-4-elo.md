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
