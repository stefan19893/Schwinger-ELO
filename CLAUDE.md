# Schwinger-ELO

Pipeline + static website computing historical ELO ratings for Swiss Schwingen athletes (2011–present).
Scrape festival results (schlussgang.ch / esv.ch) → clean & resolve identities → Schwingen-specific ELO → static JSON + HTML on GitHub Pages.

Full specification: `docs/SPEC.md` (source of truth for schema, ELO formulas, K-factors, layout).

## Stack
- Python 3.11+, `httpx`, `selectolax`/`BeautifulSoup4`, `tenacity`, `pandas`, `numpy`, `scipy`, `pytest`
- Storage: SQLite (`data/schwingen.db`) for staging, Parquet in `data/processed/`
- Frontend: static HTML + Tailwind + ECharts in `web/`, built into `dist/` by `src/exporter/static_builder.py`
- Single CLI `src/cli.py` for every stage; used by the local script and by CI
- CI/hosting: GitHub Actions + GitHub Pages

## Commands
- Everything locally, one command: `./scripts/deploy_local.sh` (flags: `--sample`, `--skip-crawl`, `--refresh`, `--test`, `--no-serve`, `--port N`) → http://localhost:8000
- Fast offline demo: `./scripts/deploy_local.sh --sample`
- Single stages: `python -m src.cli {crawl|parse|clean|elo|build|all|serve}`
- Tests: `pytest`

## Conventions
- Never put pipeline logic in the shell script or workflow YAML — add it to the CLI so local and CI stay identical.
- Code lives in `src/` (packages `scraper`, `pipeline`, `exporter`); tests in `tests/`.
- Parsers are tested against saved HTML fixtures in `tests/fixtures/` — tests never hit the network.
- Scraping must be polite: 0.5–1.0 s delay between requests, cache every raw response in `data/raw/`, never re-fetch a cached page unless explicitly refreshing.
- Generated data (`data/raw/`, `data/schwingen.db`, `dist/`) is not committed.
- Never push to GitHub unless the user asks. Commit locally after each completed task.

## Phased work & memory (read this first every session)
Work is split into phases. Progress is persisted in `docs/progress/` so any session can resume after context/tokens run out.

1. **Start of session:** read `docs/progress/STATE.md`, then the current phase file `docs/progress/phase-N-*.md`.
2. **Resume** from the first unchecked task. A task marked `[~]` was interrupted — read its handoff note and finish it first.
3. **After every task** (not only at the end of a phase): tick it `[x]`, add a dated line under *Handoff notes* in the phase file (what was done, where, anything surprising), update *Next action* in `STATE.md`, and commit.
4. **Before starting a larger task**, mark it `[~]` so an interruption is visible.
5. **Decisions** (parameter values, schema changes, deviations from the spec) go into *Decisions* in `STATE.md` with a one-line reason.
6. **End of phase:** run the `phase-reviewer` agent against the phase's exit criteria, then stop and ask the user before starting the next phase.

## Subagents (`.claude/agents/`)
| Phase | Agent |
|---|---|
| 0 Bootstrap, 1 Festival crawler, 2 Bout parser, 3 Identity cleaning | `data-engineer` |
| 4 ELO engine | `elo-modeler` |
| 5 Exporter & frontend, 6 CI & deployment | `web-builder` |
| End of every phase | `phase-reviewer` (read-only) |

The main session orchestrates: it reads `STATE.md`, delegates the current phase to its agent, and keeps `STATE.md` in sync.
