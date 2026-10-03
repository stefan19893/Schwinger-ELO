# Schwinger-ELO

Historical ELO ratings for Swiss Schwingen athletes (2011–present).

Pipeline: collect festival results (schlussgang.ch) → clean & resolve athlete
identities → Schwingen-specific ELO → static JSON + HTML site, served locally or on
GitHub Pages.

The site is **not published**. The workflows in `.github/workflows/` that crawl or deploy
do nothing until the repository owner opts in — see [Hosted setup](#hosted-setup-github-pages).

Full specification: [`docs/SPEC.md`](docs/SPEC.md). Progress: [`docs/progress/STATE.md`](docs/progress/STATE.md).

## Quick start (local)

Requires Python 3.11+ and bash (Linux, macOS, WSL). Nothing else: no Node, no browser.

```bash
./scripts/deploy_local.sh --sample    # offline demo, then serve at http://localhost:8000
./scripts/deploy_local.sh             # real data (first crawl is slow: polite delays)
./scripts/deploy_local.sh --no-serve  # build dist/ only
```

The script creates `.venv/`, installs `requirements.txt` (again only when the file
changed), runs `python -m src.cli all` and serves `dist/`. It can be run from any
directory and re-run at any time.

| Flag | Effect |
|---|---|
| `--sample` | Offline demo data from `tests/fixtures/sample/`, no network; data in `data/sample/` |
| `--skip-crawl` | Rebuild from the data already cached / processed, no request |
| `--refresh` | Re-fetch pages even if cached (thousands of requests — rarely what you want) |
| `--test` | Run the test suite (`pytest -q`) before building; stop on failure |
| `--no-serve` | Build only, do not start the server |
| `--port N` | Server port (default 8000) |
| `-h`, `--help` | Help |

`PYTHON=/path/to/python3.x` picks the interpreter.

## CLI

Every stage is a subcommand of one CLI; the local script and the GitHub workflows call
nothing else.

```bash
python -m src.cli COMMAND [options]
```

| Command | Does |
|---|---|
| `crawl` | Festival listings, statistic PDFs, ranking lists and portraits from schlussgang.ch into the cache `data/raw/` and SQLite. Incremental; 0.5–1.0 s between requests, request caps per run. Options: `--from-year`, `--to-year`, `--no-pdfs`, `--no-portraits`, `--portraits-only` |
| `parse` | Cached PDFs → SQLite (`bouts`, `athletes_raw`, identity evidence). Offline. `--force` re-parses everything |
| `clean` | Identity resolution → `data/processed/*.parquet` |
| `elo` | Ratings → `ratings.parquet`, `athlete_ratings.parquet`, `season_ratings.parquet`. `--evaluate` also prints the evidence report behind the model parameters (one to two minutes) |
| `build` | Static site → `dist/`. Exits 1 without rating data; `--allow-empty` writes pages without content (never for a deployment) |
| `check-site` | Deploy guard: exits 1 on an empty, incomplete or shrunken site in `dist/`, compared with the last accepted `meta.json` (`data/published_meta.json`). `--record` stores a passed site as the new baseline, `--accept-changes` lets an intended change pass once, `--baseline FILE` |
| `all` | `crawl → parse → clean → elo → build`. `--skip-crawl` leaves the crawl out |
| `serve` | Serve `dist/` at `http://localhost:8000` (`--port`, `--host`) |
| `state-export OUTPUT` | Bundle `data/raw`, the database, the Parquet files and the guard baseline into one `.tar.gz` (file, or a directory for a time-stamped name). **Not for publication** |
| `state-import SOURCE` | Unpack a bundle (file, or the newest bundle of a directory) into the data directory after verifying it; `--force` replaces existing state |

Global options (before or after the command): `--sample`, `--data-dir DIR`,
`--skip-crawl` (`all`), `--refresh`, `--offline` (cache only, never fetch),
`--require-state` (`crawl` / `all`: exit 1 before the first request unless the data
directory holds the state of earlier runs — used in CI), `-v`.

Configuration: defaults in `src/config.py`, overridden by `SCHWINGEN_<FIELD>` environment
variables, overridden by CLI flags. `SCHWINGEN_DIST_DIR` moves the output directory.

`elo --evaluate` re-runs the rating engine with other parameters (update order, MoV, K
scale, mean reversion) and prints prediction error, calibration, rating drift and the
identity sensitivity pass. The report is read-only (the ratings written are those of the
configured model). The model parameters are `elo_k_scale`, `season_reversion_delta`,
`mov_alpha`, ... in `src/config.py`.

### What gets published (switches in `src/config.py`)

| Setting | Default | Effect |
|---|---|---|
| `publish_min_age` | `18` | Athletes who are not certainly 18 at the data date (data year − birth year ≤ 18) are not published by name: they count in the ratings, but have no profile, no search entry and no rank, and a festival lists them as "Jungschwinger, Name nicht veröffentlicht". Ranks are the places among the published athletes. Athletes without a known birth year cannot be filtered and are published. `0` publishes everyone. On the data of 2026-09-27: 718 athletes withheld, 523 of them otherwise ranked |
| `site_noindex` | `True` | `<meta name="robots" content="noindex">` on every page and a `robots.txt`. Under `<user>.github.io/Schwinger-ELO/` crawlers do not read that `robots.txt` (only the one at the root of the host counts) and the JSON data files cannot carry the tag — the switch keeps the pages out of search results, it is not access control |
| `contact_email` | `""` | When set, the about page shows the address as a non-public route for corrections and objections beside the GitHub issues link. Empty: nothing is shown |

Athlete links (`athlete.html?id=…`) are not permanent: an id can change when the
identity resolution changes. An outdated link shows "Schwinger nicht gefunden" with
suggestions.

## Website

`python -m src.cli build` copies `web/` to `dist/` and writes the data the pages read to
`dist/data/` (`src/exporter/static_builder.py`): `meta.json`, `rankings_latest.json`,
`athletes.json` (search index), `alltime_top200.json`, `seasons.json`, `festivals.json`,
one `history/history_<athlete_id>.json` per athlete and one `fests/fest_<fest_id>.json`
per festival. The same inputs give a byte-identical `dist/`.

`build` needs the outputs of `clean` and `elo` in `data/processed/`. Without them (or with
an empty `ratings.parquet`) it exits with status 1 and leaves an existing `dist/` untouched,
so a deployment can never publish an empty site by accident. `build --allow-empty` writes
the pages with empty data files instead (layout work on a fresh clone); `build --sample`
and `./scripts/deploy_local.sh --sample` build their own demo data.

Pages (German, static, relative URLs only, so they work under `/Schwinger-ELO/`):

| Page | Shows |
|---|---|
| `index.html` | current ranking with Teilverband filter, season lists (`#saison-2019`), highest ratings (`#bestwerte`), search |
| `athlete.html?id=<athlete_id>` | profile, career chart, seasons, festivals |
| `fests.html`, `fests.html?id=<fest_id>` | festival list and one festival with every athlete's bouts |
| `about.html` | method, source, known limitations, how to report errors |

```bash
python -m src.cli build && python -m src.cli serve     # http://localhost:8000
```

Third-party code is vendored, nothing is loaded from a CDN: Apache ECharts
(`web/vendor/`, see its `README.md`) and a stylesheet generated by Tailwind CSS. After
adding or removing classes in `web/*.html` or `web/js/*.js`, regenerate
`web/css/style.css` (needs no Node; downloads the Tailwind standalone CLI once into
`.cache/`):

```bash
./scripts/build_css.sh
```

## Tests

```bash
.venv/bin/python -m pytest
```

The tests never use the network (`tests/conftest.py` blocks it). The page scripts are
exercised by `tests/test_browser_smoke.py`: it builds the sample site, serves it locally
and loads every page in a headless Chrome / Chromium (`google-chrome` or `chromium` on the
PATH, the Windows Chrome under WSL, or `SCHWINGEN_BROWSER=/path/to/browser`). Without a
browser those tests are skipped; `SCHWINGEN_REQUIRE_BROWSER=1` (set in CI) makes a missing
browser an error.

## Hosted setup (GitHub Pages)

Three workflows:

| Workflow | Starts | Does | Needs the opt-in |
|---|---|---|---|
| `ci.yml` | push to `main`, pull requests | install, `pytest` (incl. the browser smoke test), sample build, `check-site --sample`. No request to schlussgang.ch, publishes nothing | no |
| `deploy_pages.yml` | by hand | restore the state, rebuild offline (`all --skip-crawl`), deploy guard, save the state, deploy to Pages. No crawl | **yes** |
| `scrape_and_update.yml` | Tuesdays 03:17 UTC from March to October, the 1st of the month from November to February, or by hand | restore the state, incremental crawl, rebuild, deploy guard, save the state, deploy | **yes** |

"Opt-in" is the repository variable `PUBLISH_ENABLED`. Unless it is exactly `true`, every
job of the two publishing workflows is skipped: no crawl, no build, no deployment — also
when the schedule fires or someone presses "Run workflow".

**The state between runs** (HTTP cache, database, Parquet files, guard baseline; about
500 MB) is one bundle file attached to a **draft** release named `pipeline-state`. A draft
is visible only to accounts with write access to the repository. **Never press "Publish
release" on it:** the bundle contains what the site deliberately leaves out (birthdays,
licence numbers, residences) and the source's PDFs. Every run checks that the release is
still a draft and stops otherwise. A run that finds no bundle fails before any request —
a runner never starts a crawl from scratch.

### Checklist for the owner

Nothing below has been done; each step is yours.

**1. Decide**

- [ ] Publish at all? The ratings are derived from ESV festival results obtained through
      schlussgang.ch; the ESV's terms claim ownership of the results.
- [ ] `publish_min_age` (default 18: 718 athletes are withheld, 523 of them ranked; `0`
      publishes everyone, including 478 athletes born 2009–2010).
- [ ] `site_noindex` (default on).
- [ ] `contact_email` (default none: corrections and objections only through public
      GitHub issues).
- [ ] Athlete links are not permanent (no id registry) — acceptable for now?

Change the three settings in `src/config.py` and commit; the workflows have no copies of
them.

**2. Get the workflows onto `main`** (merge / push the branch). `ci.yml` starts running;
the other two stay inert.

**3. Build, look, accept** — on the machine that has the data:

```bash
python -m src.cli all                 # or: all --skip-crawl, to use what is there
python -m src.cli serve               # look at http://localhost:8000
python -m src.cli check-site --accept-changes --record
```

The last command stores this site's counts as the baseline (`data/published_meta.json`)
that later builds are compared with.

**4. Upload the initial state**

```bash
python -m src.cli state-export /tmp/schwingen-state
gh release create pipeline-state --draft \
  --title "Pipeline state - keep as DRAFT, never publish" \
  --notes "Private working data of the scheduled workflow. Not for publication." \
  /tmp/schwingen-state/schwingen-state-*.tar.gz
gh release view pipeline-state --json isDraft,assets   # isDraft must be true, one asset
```

(Or in the browser: Releases → Draft a new release → tag `pipeline-state` → attach the
file → **Save draft**.) There must be no git tag called `pipeline-state`.

**5. Enable Pages:** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
This publishes nothing by itself.

**6. Opt in:** Settings → Secrets and variables → Actions → Variables → New repository
variable `PUBLISH_ENABLED` = `true` (or `gh variable set PUBLISH_ENABLED --body true`).
From now on the next scheduled run crawls and deploys.

**7. First deployment by hand:** Actions → "Deploy to GitHub Pages" → Run workflow
(branch `main`), or `gh workflow run deploy_pages.yml`. Leave `accept_changes` off if you
did step 3; tick it if the bundle has no baseline. Then check:

- [ ] the run is green and its "Rebuild" step reports the athlete counts you saw locally;
- [ ] `https://<user>.github.io/Schwinger-ELO/` shows the site; the page source contains
      `<meta name="robots" content="noindex">` (if `site_noindex` is on);
- [ ] `gh release view pipeline-state --json isDraft,assets`: still a draft, exactly one
      asset, named after the run.

This first run is also the first real test of the workflows — they were validated with
`actionlint` and by running every command locally, but never executed on GitHub.

**Afterwards**

- A red "Scrape and update" run deploys nothing. If the deploy guard stopped it
  ("check-site: FAILED"), read the numbers in the log; if the change is intended (for
  example after changing `publish_min_age`), start the workflow by hand with
  `accept_changes` ticked, once.
- If the state is lost or damaged, repeat step 4 from your machine (delete the old asset
  first). The next run catches up on what is missing, within the request caps.
- GitHub pauses scheduled workflows after 60 days without repository activity and sends
  a mail; re-enable it under Actions.
- The state bundle grows by roughly 60 MB a season; a release asset may be 2 GB.

**Turning it off**

- Stop updates: `gh variable set PUBLISH_ENABLED --body false` (or delete the variable).
  The site that is already deployed **stays online**.
- Take the site down: Settings → Pages → "Unpublish site" (or set the source to None).
- Remove the working data from GitHub: `gh release delete pipeline-state --yes`.
