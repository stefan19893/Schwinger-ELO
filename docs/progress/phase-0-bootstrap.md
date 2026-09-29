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
- [x] 5. `src/config.py` (paths, year range, rate limits, ELO params; env overrides) + `src/cli.py` with stub subcommands and `serve`
- [x] 6. `scripts/deploy_local.sh` (preflight, venv + hash-based reinstall, flags from spec §6.2), placeholder `dist/`; test with a fresh `.venv`
- [x] 7. Update commands section in `CLAUDE.md`

## Handoff notes
<!-- YYYY-MM-DD — what was done / where / surprises -->
- 2026-09-29 — Task 1: created spec §3 layout: `src/{scraper,pipeline,exporter}` packages with docstring-only stub modules (incl. `exporter/static_builder.py`), `tests/` (+ `fixtures/sample/.gitkeep`), `web/`, `.github/workflows/`, `scripts/`. `.gitignore` written early so the initial commit is clean (task 3 finalizes it). `web/js`, `web/css` and HTML templates deferred to Phase 5.
- 2026-09-29 — Task 2: `requirements.txt` (major-version ranges: httpx, bs4, selectolax, tenacity, tqdm, pandas, numpy, scipy, pyarrow, pytest), `pyproject.toml` (metadata, `requires-python >=3.11`, pytest `testpaths=tests`), minimal `README.md`. Deps live only in requirements.txt (not duplicated in pyproject). pyarrow added for Parquet (not listed in spec stack).
- 2026-09-29 — Task 3: `.gitignore` verified with `git check-ignore` (`.venv/`, `data/raw/`, `data/processed/`, `data/*.db`, `dist/`, pycache/pytest caches ignored; `tests/fixtures/` not ignored). Note: `data/processed/` is ignored too (spec §3: all of `data/` is generated) — revisit in Phase 6 if CI decides to commit processed Parquet.
- 2026-09-29 — Task 4: `.venv` created and deps installed (Python 3.14.4; resolved: httpx 0.28.1, bs4 4.15.0, selectolax 0.4.13, tenacity 9.1.4, tqdm 4.70.1, pandas 2.3.3, numpy 2.5.3, scipy 1.18.1, pyarrow 22.0.0, pytest 8.4.2). `tests/test_smoke.py` imports every module — 10 passed. SURPRISE: system Python has no `ensurepip` (Debian `python3.14-venv` not installed) and no pip/uv, so `python3 -m venv` fails. Worked around with `venv --without-pip` + official `get-pip.py` bootstrap; `deploy_local.sh` must implement the same fallback (task 6).
- 2026-09-29 — Task 5: `src/config.py` — frozen `Config` dataclass (paths, year range 2011..current year, politeness 0.5–1.0 s / timeout / retries / User-Agent, ELO params incl. K-factors, season reversion, host/port); `load_config(overrides, env)` applies defaults < `SCHWINGEN_<FIELD>` env < CLI flags (None = unset), with validation. `src/cli.py` — argparse; global options (`--sample`, `--data-dir`, `--skip-crawl`, `--refresh`, `-v`) work before or after the subcommand (subparser copies use SUPPRESS); `crawl`/`all` take `--from-year/--to-year`, `serve` takes `--port/--host`. crawl/parse/clean/elo are logging stubs; `build` calls `exporter.static_builder.build_site` (copies `web/`, writes placeholder `index.html` + `.nojekyll`, refuses to wipe repo root/home/data/web); `serve` uses stdlib `ThreadingHTTPServer`, errors if dist/index.html missing. `tests/test_cli.py` (20 tests, incl. `all --sample` → dist). Total 30 passed. `SCHWINGEN_DIST_DIR` env override exists (not a CLI flag) — tests use it to build into tmp.
- 2026-09-29 — Task 6: `scripts/deploy_local.sh` finished (reviewed the interrupted draft; kept it, two fixes). Steps: preflight (Python >= 3.11; `PYTHON=` override, else python3/python3.14..3.11/python), `.venv` (recreated if broken), sha256 of `requirements.txt` in `.venv/.requirements.sha256` → reinstall only on change, optional `pytest -q` (`--test`), `python -m src.cli all` with `--sample/--skip-crawl/--refresh` passed through, then `exec python -m src.cli serve --port N` unless `--no-serve`. Also `--port=N`, `-h`; bad flag/port exits 1; warns before a first real crawl. Fixes vs draft: (a) pip is now checked/bootstrapped independently of venv creation (`get-pip.py` saved inside `.venv`, hash file cleared), so a `.venv` left without pip by an interrupted bootstrap self-heals; (b) venv's ensurepip error is printed to stdout, so the first attempt is now silenced with `>/dev/null 2>&1`. Tested: old `.venv` moved to scratchpad; fresh run from `/tmp` → venv via `--without-pip` + get-pip.py, deps installed, `dist/index.html` + `.nojekyll`, exit 0; second run → "unchanged - skipping install" (0.15 s); stale hash → reinstall; `--test` → 30 passed; serve on a random free port → HTTP 200, then killed. Backup venv deleted. `pytest`: 30 passed.
- 2026-09-29 — Task 7 NOT done (left `[ ]`, awaiting user): the data-engineer agent may not edit `CLAUDE.md` on another agent's instruction; needs the user's direct OK. Checked: the current Commands section is already accurate (all six script flags + CLI subcommands match). Proposed additions (in the agent report): `--port=N`/`-h`, `PYTHON=` interpreter override, CLI global options `--sample --data-dir --skip-crawl --refresh -v`, `crawl|all --from-year/--to-year`, `serve --port/--host`, tests via `.venv/bin/python -m pytest`.
- 2026-09-29 — Task 7: Commands section in `CLAUDE.md` updated by the main session (user asked to continue Phase 0) with the proposed additions: script `-h`/`PYTHON=`, CLI global options, `--from-year/--to-year`, `serve --port/--host`, pytest via `.venv`.
- 2026-09-29 — Phase review: all exit criteria PASS. Fixed the reviewer's risks on branch `phase-0-review-fixes`: safe dist wipe (`.nojekyll` marker), `--sample` → `data/sample/`, port validation + clean bind errors, warnings for ineffective `--skip-crawl`/`--refresh`, unbounded pyarrow, get-pip.py sha256 print/pin, User-Agent contact URL. Tests 30 → 42. Status → in review.
- 2026-09-29 — PR #1 (review fixes) merged into `main`; user approved closing Phase 0. Status → done.
