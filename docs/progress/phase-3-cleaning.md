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

## Known inputs from Phase 2 review
Notes only (2026-10-01, Phase 2 review fixes; data = `data/schwingen.db` table `athletes_raw`, parser v2, 174,175 rows).
- **Metadata is sparse:** birth year is present on only ~7.9 % of raw athlete rows, association on ~2.2 %, place on ~0.6 %, and there is **no club** column in this source. **Profile-slug URLs don't exist** in the statistic sheets — the spec's slug-first rule cannot apply; resolution must rely on name + co-occurrence (opponents, festivals, regions, years).
- **Star counts differ** between an athlete's own line and the opponent lines (`S**` vs `S***`): never use Kranz stars as part of an identity key.
- **Suffixes "1"/"2"** (`Herger Elias 1`) are only meaningful within one sheet. There are cross-sheet typos (Marridor/Maridor), 396 duplicate names within sheets (`duplicate_name_in_sheet` rejects), and parsing leftovers in keys (e.g. `koch silvan 10`).
- **Glyph-id sheets** (`parse_rejects.reason = 'glyph_ids_decoded_lossy'`, 8 sheets, 2021): non-ASCII letters are `?` (`M?ller`) → need fuzzy matching; `glyph_ids_decoded_mac` sheets (3) are exact.
- **Bout ids point at per-sheet `athletes_raw` rows** (`<fest_id>-<idx>`): after identity resolution every bout must be remapped to the canonical `athlete_id`.
- **`athletes_raw.flags`** (added 2026-10-01): `entries_overflow` = the row's entry list merges two athletes' blocks (more entries than Gänge + 1; 131 rows, mostly sheets 25225/25254) → **not usable as identity evidence** (opponents/points belong partly to someone else); `interim_sheet` = row taken from an interim sheet (ESAF 2013, rank NULL, points after 4 Gänge).
