# Phase 6 — CI & deployment (Milestone 5)
**Agent:** web-builder

## Goal
Automated weekly update and GitHub Pages deployment.

## Exit criteria
- Workflows call only the CLI (`python -m src.cli …`), no logic duplicated in YAML.
- `.github/workflows/deploy_pages.yml`: install, test, `cli build`, `actions/deploy-pages@v4`.
- `.github/workflows/scrape_and_update.yml`: scheduled weekly during season, incremental crawl → rebuild → deploy.
- State persistence between runs decided and logged in `STATE.md`.
- `README.md` documents local usage (`deploy_local.sh` + flags) and hosted setup (enabling Pages).

## Tasks
- [ ] 1. Decide state persistence strategy for scheduled runs; log decision
- [ ] 2. Write `deploy_pages.yml`
- [ ] 3. Write `scrape_and_update.yml` (cron, polite crawl, caching)
- [ ] 4. Validate workflows (syntax/`actionlint` if available)
- [ ] 5. README: quick start with `./scripts/deploy_local.sh`, flags, CLI reference, Pages setup
- [ ] 6. Hand over to user for first push + enabling Pages (do not push yourself)

## Handoff notes
