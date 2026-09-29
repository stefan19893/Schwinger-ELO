# Project State

**Last updated:** 2026-09-29
**Current phase:** 1 — Festival crawler (in progress, branch `phase-1-crawler`)
**Next action:** Phase 1, task 1 — investigate sources (robots.txt/terms, festival index + Notenblatt URL patterns).

## Phases
| # | Phase | Spec milestone | File | Status |
|---|---|---|---|---|
| 0 | Bootstrap + CLI + local script | M0 | `phase-0-bootstrap.md` | done |
| 1 | Festival crawler | M1 | `phase-1-crawler.md` | in progress |
| 2 | Bout parser | M2 | `phase-2-parser.md` | not started |
| 3 | Identity cleaning | M3a | `phase-3-cleaning.md` | not started |
| 4 | ELO engine | M3b | `phase-4-elo.md` | not started |
| 5 | Exporter & frontend | M4 | `phase-5-web.md` | not started |
| 6 | CI & deployment | M5 | `phase-6-deploy.md` | not started |

Status values: `not started` · `in progress` · `in review` · `done`

## Decisions
<!-- - YYYY-MM-DD — decision — reason -->
- 2026-09-29 — Single CLI (`python -m src.cli`) used by both `scripts/deploy_local.sh` and GitHub Actions; `--sample` offline dataset for fast local runs — spec §5–6.
- 2026-09-29 — Repo root `Schwinger-ELO/` is the project root (spec §3 updated accordingly).
- 2026-09-29 — Added `pyarrow` to requirements — pandas needs a Parquet engine for `data/processed/*.parquet`.
- 2026-09-29 — Dependencies listed only in `requirements.txt`; `pyproject.toml` holds metadata + pytest config — single source for the hash-based reinstall in `deploy_local.sh` and CI.
- 2026-09-29 — When `ensurepip` is unavailable (Debian/Ubuntu without `python3.X-venv`), create `.venv` with `--without-pip` and bootstrap pip via `https://bootstrap.pypa.io/get-pip.py` — the dev machine (WSL, Python 3.14) lacks ensurepip and installing the apt package needs sudo. User may prefer `sudo apt install python3.14-venv` instead.
- 2026-09-29 — `mov_alpha = 0.0`, `mov_baseline_diff = 0.0` placeholders in `src/config.py` (MoV multiplier neutral, λ = 1) — spec leaves them unspecified; calibrate in Phase 4.
- 2026-09-29 — K-factor keys follow spec §4.1 categories; `Gauverband` and `Regional` both K = 16 — spec §4.2 lists only "Regional-/Rangschwinget" for the lowest tier.
- 2026-09-29 — `serve` binds to `127.0.0.1` by default (`--host` / `SCHWINGEN_HOST` to change) — local preview only, don't expose on the LAN by default.
- 2026-09-29 — Extra `serve --host` option and `SCHWINGEN_DIST_DIR` env override beyond spec §5 — needed for tests and flexibility; no spec behaviour changed.

- 2026-09-29 — `deploy_local.sh` also accepts `--port=N`, `-h/--help` and a `PYTHON=` env var to pick the interpreter; pip bootstrap runs whenever `.venv` lacks pip (not only right after creation) — convenience/self-healing, spec §6 behaviour unchanged.
- 2026-09-29 — `--sample` defaults `data_dir` to `data/sample/` (gitignored); explicit `--data-dir` / `SCHWINGEN_DATA_DIR` still win — sample runs must never overwrite the real db/Parquet (phase-0 review).
- 2026-09-29 — `build_site` only deletes an existing dist dir that is empty or contains the `.nojekyll` build marker — prevents wiping arbitrary folders via `SCHWINGEN_DIST_DIR` (phase-0 review).
- 2026-09-29 — Port validated in `load_config` (1–65535, exit 2); bind failures in `serve` exit 1 with a message instead of a traceback (phase-0 review).
- 2026-09-29 — CLI warns when `--skip-crawl` (only `all`) or `--refresh` (only `crawl`/`all`) is passed to a command it doesn't affect — kept as global options for spec §5 compatibility (phase-0 review).
- 2026-09-29 — `pyarrow>=18` without upper bound — frequent major releases, we only use the stable Parquet read/write API (phase-0 review).
- 2026-09-29 — `get-pip.py` fallback prints its sha256 and can be pinned via `GET_PIP_SHA256`; no default pin because the file changes with every pip release. Preferred fix remains `sudo apt install python3.14-venv` (phase-0 review).
- 2026-09-29 — User-Agent contact: `+https://github.com/stefan19893/Schwinger-ELO` (public repo) — resolves open question.

## Open questions
- Which source is primary (schlussgang.ch vs esv.ch), and do their robots.txt / terms allow crawling? → answer in Phase 1.
- ELO parameters `alpha` and `BaselineDiff` (MoV multiplier) are unspecified → calibrate in Phase 4.
- How does the scheduled GitHub Action keep state between runs (commit processed Parquet, release asset, or cache)? → decide in Phase 6.


## Blockers
- none
