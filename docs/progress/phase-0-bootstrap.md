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
- [x] 3. Write `.gitignore` (`.venv/`, `data/raw/`, `data/*.db`, `dist/`, caches)
- [x] 4. Create venv, install deps, add smoke test, confirm `pytest` passes
- [ ] 5. `src/config.py` (paths, year range, rate limits, ELO params; env overrides) + `src/cli.py` with stub subcommands and `serve`
- [ ] 6. `scripts/deploy_local.sh` (preflight, venv + hash-based reinstall, flags from spec §6.2), placeholder `dist/`; test with a fresh `.venv`
- [ ] 7. Update commands section in `CLAUDE.md`

## Handoff notes
<!-- YYYY-MM-DD — what was done / where / surprises -->
- 2026-09-29 — Task 1: created spec §3 layout: `src/{scraper,pipeline,exporter}` packages with docstring-only stub modules (incl. `exporter/static_builder.py`), `tests/` (+ `fixtures/sample/.gitkeep`), `web/`, `.github/workflows/`, `scripts/`. `.gitignore` written early so the initial commit is clean (task 3 finalizes it). `web/js`, `web/css` and HTML templates deferred to Phase 5.
- 2026-09-29 — Task 2: `requirements.txt` (major-version ranges: httpx, bs4, selectolax, tenacity, tqdm, pandas, numpy, scipy, pyarrow, pytest), `pyproject.toml` (metadata, `requires-python >=3.11`, pytest `testpaths=tests`), minimal `README.md`. Deps live only in requirements.txt (not duplicated in pyproject). pyarrow added for Parquet (not listed in spec stack).
- 2026-09-29 — Task 3: `.gitignore` verified with `git check-ignore` (`.venv/`, `data/raw/`, `data/processed/`, `data/*.db`, `dist/`, pycache/pytest caches ignored; `tests/fixtures/` not ignored). Note: `data/processed/` is ignored too (spec §3: all of `data/` is generated) — revisit in Phase 6 if CI decides to commit processed Parquet.
- 2026-09-29 — Task 4: `.venv` created and deps installed (Python 3.14.4; resolved: httpx 0.28.1, bs4 4.15.0, selectolax 0.4.13, tenacity 9.1.4, tqdm 4.70.1, pandas 2.3.3, numpy 2.5.3, scipy 1.18.1, pyarrow 22.0.0, pytest 8.4.2). `tests/test_smoke.py` imports every module — 10 passed. SURPRISE: system Python has no `ensurepip` (Debian `python3.14-venv` not installed) and no pip/uv, so `python3 -m venv` fails. Worked around with `venv --without-pip` + official `get-pip.py` bootstrap; `deploy_local.sh` must implement the same fallback (task 6).
