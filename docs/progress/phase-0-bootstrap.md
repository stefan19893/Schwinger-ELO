# Phase 0 — Bootstrap
**Agent:** data-engineer

## Goal
Runnable project skeleton so later phases only add code.

## Exit criteria
- Directory layout from `docs/SPEC.md` §3 exists (empty modules with `__init__.py`).
- `requirements.txt`, `.gitignore`, minimal `README.md` present.
- `pytest` runs green with one trivial test.
- `src/config.py` and `src/cli.py` exist with stub subcommands (spec §5).
- `./scripts/deploy_local.sh --no-serve` works on a fresh clone and produces a placeholder `dist/`; without `--no-serve` it serves it (spec §6).

## Tasks
- [x] 1. Create directory structure and package `__init__.py` files
- [x] 2. Write `requirements.txt` (pinned major versions) and `pyproject.toml` / pytest config
- [ ] 3. Write `.gitignore` (`.venv/`, `data/raw/`, `data/*.db`, `dist/`, caches)
- [ ] 4. Create venv, install deps, add smoke test, confirm `pytest` passes
- [ ] 5. `src/config.py` (paths, year range, rate limits, ELO params; env overrides) + `src/cli.py` with stub subcommands and `serve`
- [ ] 6. `scripts/deploy_local.sh` (preflight, venv + hash-based reinstall, flags from spec §6.2), placeholder `dist/`; test with a fresh `.venv`
- [ ] 7. Update commands section in `CLAUDE.md`

## Handoff notes
<!-- YYYY-MM-DD — what was done / where / surprises -->
- 2026-09-29 — Task 1: created spec §3 layout: `src/{scraper,pipeline,exporter}` packages with docstring-only stub modules (incl. `exporter/static_builder.py`), `tests/` (+ `fixtures/sample/.gitkeep`), `web/`, `.github/workflows/`, `scripts/`. `.gitignore` written early so the initial commit is clean (task 3 finalizes it). `web/js`, `web/css` and HTML templates deferred to Phase 5.
- 2026-09-29 — Task 2: `requirements.txt` (major-version ranges: httpx, bs4, selectolax, tenacity, tqdm, pandas, numpy, scipy, pyarrow, pytest), `pyproject.toml` (metadata, `requires-python >=3.11`, pytest `testpaths=tests`), minimal `README.md`. Deps live only in requirements.txt (not duplicated in pyproject). pyarrow added for Parquet (not listed in spec stack).
