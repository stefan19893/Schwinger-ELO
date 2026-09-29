# Schwinger-ELO

Historical ELO ratings for Swiss Schwingen athletes (2011–present).

Pipeline: scrape festival results (schlussgang.ch / esv.ch) → clean & resolve
athlete identities → Schwingen-specific ELO → static JSON + HTML site
(GitHub Pages).

Full specification: [`docs/SPEC.md`](docs/SPEC.md). Progress: [`docs/progress/STATE.md`](docs/progress/STATE.md).

## Quick start (local)

Requires Python 3.11+ and bash (Linux, macOS, WSL).

```bash
./scripts/deploy_local.sh --sample    # offline demo, then serve at http://localhost:8000
./scripts/deploy_local.sh             # real data (first crawl is slow: polite delays)
./scripts/deploy_local.sh --no-serve  # build dist/ only
```

Flags: `--sample`, `--skip-crawl`, `--refresh`, `--test`, `--no-serve`, `--port N`.

## CLI

Every stage is a subcommand of one CLI (used locally and in CI):

```bash
python -m src.cli {crawl|parse|clean|elo|build|all|serve} [--sample] [--data-dir DIR]
```

## Tests

```bash
pytest
```
