# Phase 3 — Identity cleaning (Spec §4.1, Milestone 3a)
**Agent:** data-engineer

## Goal
One canonical `athlete_id` per real person; clean Parquet outputs.

## Exit criteria
- `src/pipeline/cleaner.py` resolves identities via profile slug URL, then name + Schwingklub.
- Duplicate-name collisions handled and covered by tests.
- Club names and sub-associations (BKSV, ISV, NOSV, NWSV, SWSV) normalized.
- `data/processed/bouts.parquet` and `athletes.parquet` written by `python -m src.cli clean` (also works with `--sample`).

## Tasks
- [ ] 1. Analyse raw athletes: list name collisions and spelling variants
- [ ] 2. Implement identity resolution + tests
- [ ] 3. Club / sub-association normalization + tests
- [ ] 4. Write Parquet outputs via `cli clean`; record athlete/bout counts in handoff notes

## Handoff notes
