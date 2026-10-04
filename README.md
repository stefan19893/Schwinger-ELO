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
| `elo` | Ratings → `ratings.parquet`, `athlete_ratings.parquet`, `season_ratings.parquet`, `bout_ratings.parquet` (rating change per bout). `--evaluate` also prints the evidence report behind the model parameters (one to two minutes) |
| `build` | Static site → `dist/`. Exits 1 without rating data; `--allow-empty` writes pages without content (never for a deployment) |
| `check-site` | Deploy guard: exits 1 on an empty, incomplete or shrunken site in `dist/`, compared with the last accepted `meta.json` (`data/published_meta.json`) — and on a site that publishes more athletes or withholds fewer than that one (an age filter that lost its birth years). Never deployable, whatever the option: a site whose data files (rankings, season and all-time lists, festival rows, opponent lists in `data/bouts`, namesakes) name an athlete id that is not in the search index `athletes.json`, or whose per-athlete files do not match it one to one. `--record` stores a passed site as the new baseline, `--accept-changes` lets an intended change pass once, `--baseline FILE` |
| `pack-site OUTPUT` | Pack `dist/` into the tar file GitHub Pages deploys (uncompressed; hidden files left out, as `actions/upload-pages-artifact` does) **without listing its files** — they are named after athletes, and a workflow log is public. Logs counts and sizes only. Exits 1 on a missing site, on a symbolic link or special file in it, on an archive of 1 GB or more, and when `OUTPUT` lies inside `dist/`. Always packs the site directory; there is no option for another source |
| `all` | `crawl → parse → clean → elo → build`. `--skip-crawl` leaves the crawl out |
| `serve` | Serve `dist/` at `http://localhost:8000` (`--port`, `--host`) |
| `state-export OUTPUT` | Bundle `data/raw`, the database, the Parquet files and the guard baseline into one `.tar.gz` (`OUTPUT` ending in `.tar.gz` is the file; anything else is a directory, created if missing, for a time-stamped name). The bundle is readable by you only (mode 0600). **Not for publication** |
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
| `publish_min_age` | `18` | Athletes who are not certainly 18 at the data date (data year − birth year ≤ 18) are not published by name: they count in the ratings, but have no profile, no search entry and no rank, and a festival lists them as "Jungschwinger, Name nicht veröffentlicht". No rating is displayed for them (their festival rows carry no rating value). **This withholds the display, not the information:** their ratings are calculated from public results and follow from the published athletes' histories (see "What you accept by opting in"). Ranks are the places among the published athletes. `0` publishes everyone. On the data of 2026-09-27: 726 athletes withheld, 525 of them otherwise ranked |
| `publish_unknown_recent_seasons` | `3` | Athletes **without a known birth year** are withheld in the same way when their first season is one of the last three of the data year (2024–2026): they may be minors (97.7 % of debutants are at least 16; a 16-year-old debutant is not certainly 18 for three data years). 8 of the 726. The 1,737 athletes without a birth year who started earlier are published. `0` switches the rule off; the `--sample` demo runs without it |
| `site_noindex` | `True` | `<meta name="robots" content="noindex">` on every page and a `robots.txt`. Under `<user>.github.io/Schwinger-ELO/` crawlers do not read that `robots.txt` (only the one at the root of the host counts) and the JSON data files cannot carry the tag — the switch keeps the pages out of search results, it is not access control |
| `contact_email` | `""` | When set, the about page shows the address as a non-public route for corrections and objections beside the GitHub issues link. Empty: nothing is shown |

Athlete links (`athlete.html?id=…`, `compare.html?ids=…`) are not permanent: an id can
change when the identity resolution changes. An outdated link shows "Schwinger nicht
gefunden" with suggestions (on the comparison page for the affected athlete only).

## Website

`python -m src.cli build` copies `web/` to `dist/` and writes the data the pages read to
`dist/data/` (`src/exporter/static_builder.py`): `meta.json`, `rankings_latest.json`,
`athletes.json` (search index), `alltime_top200.json`, `seasons.json`, `festivals.json`,
one `history/history_<athlete_id>.json` per athlete, one `fests/fest_<fest_id>.json`
per festival and one `bouts/bouts_<athlete_id>.json` per athlete (his bouts against other
published athletes, each with what it contributed to his rating, and the opponents' names;
read only by the comparison page, one file per selected athlete). The contributions come
from `bout_ratings.parquet`, so `build` needs the output of the current `elo`. Nothing is
exported about a bout against an athlete who is not published by name.
`meta.json` and every history and bouts file carry the same `build` stamp (a digest of the
inputs and settings, not a time). The comparison page draws an athlete's Gänge only when
his two files carry the same stamp; with a cached file from an earlier build it falls back
to one point per festival and says so. `check-site` refuses a bouts file that holds
anything beyond its contract and files of mixed builds.
The same inputs give a byte-identical `dist/` (real data: about 14,400 files, 103 MB).

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
| `compare.html?ids=<athlete_id>,<athlete_id>,…` | comparison of up to six athletes: figures side by side, ratings in one chart — over time or, with the switch above the chart, by number of bouts (`&x=gaenge`; there the line moves Gang by Gang: the rating is calculated per festival, and the chart shows how the festival's change is made up of the contributions of its Gänge — a breakdown, not a rating after each Gang; bouts against athletes who are not published appear only as one combined remainder per festival), by age (`&x=alter`, calendar year minus birth year; athletes without a known birth year are not drawn and named) or by season of the recorded career (`&x=saison`, season-end ratings); any other value of `x` shows time — seasons, direct bouts (tally and list) and common festivals. The selection is part of the address, so a comparison can be shared; an outdated id shows suggestions for that slot. Athletes who are not published by name cannot be selected and do not occur in the comparison data |
| `fests.html`, `fests.html?id=<fest_id>` | festival list and one festival with every athlete's bouts |
| `about.html` | method, source, known limitations, how to report errors |

```bash
python -m src.cli build && python -m src.cli serve     # http://localhost:8000
```

Third-party code is vendored, nothing is loaded from a CDN: Apache ECharts
(`web/vendor/`, see its `README.md`) and a stylesheet generated by Tailwind CSS. After
adding or removing classes in `web/*.html` or `web/js/*.js`, regenerate
`web/css/style.css` (needs no Node; downloads the Tailwind standalone CLI once into
`.cache/`; `tests/test_web.py` fails when a class is used that the committed stylesheet
does not define — the comparison page was built from the existing classes only):

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
| `ci.yml` | push to `main`, pull requests | install (Python 3.11: `requirements.txt`; 3.14: the hashed `requirements-lock.txt`), `pytest` (incl. the browser smoke test), sample build, `check-site --sample`. No request to schlussgang.ch, publishes nothing | no |
| `deploy_pages.yml` | by hand | restore the state, rebuild offline (`all --skip-crawl`), deploy guard, save the state, pack the site (`pack-site`), deploy to Pages. No crawl | **yes** |
| `scrape_and_update.yml` | by hand (no schedule) | restore the state, incremental crawl, rebuild, deploy guard, save the state, deploy. Not needed when you crawl locally — see [Updating the site](#updating-the-site) | **yes** |

"Opt-in" is the repository variable `PUBLISH_ENABLED`. Unless it is exactly `true`, every
job of the two publishing workflows is skipped: no crawl, no build, no deployment — also
when someone presses "Run workflow". Nothing runs on a schedule: GitHub never crawls
schlussgang.ch on its own.

**The state between runs** (HTTP cache, database, Parquet files, guard baseline; about
500 MB) is one bundle file attached to a **draft** release named `pipeline-state`. A draft
is visible only to accounts with write access to the repository. **Never press "Publish
release" on it:** the bundle contains what the site deliberately leaves out (birthdays,
licence numbers, residences) and the source's PDFs. Every run checks that the release is
still a draft — before the download and again immediately before the upload — and stops
otherwise. A run that finds no bundle, or one whose cache is incomplete (statistic PDFs,
ranking PDFs, listings, portraits), fails before any request — a runner never starts a
crawl from scratch.

### Checklist for the owner

**Shortcut:** once steps 1–5 below are done, `./scripts/go_live.sh` does steps 6–7 in one
go: it checks the preparation (draft release with one bundle, Pages source, workflow),
asks for one confirmation, sets `PUBLISH_ENABLED=true`, starts the first deploy and waits
for it. `./scripts/go_live.sh --check` only checks; `./scripts/go_live.sh --off` switches
the automation off again (the deployed site stays online until it is unpublished).

Nothing below has been done; each step is yours.

**1. Decide** (each default is the cautious one; `src/config.py` unless noted)

- [ ] **Publish at all?** The ratings are derived from ESV festival results obtained
      through schlussgang.ch; the ESV's terms claim ownership of the results.
- [ ] **`publish_min_age`** (default 18 = everyone not *certainly* 18 at the data date,
      i.e. born 2008 or later: with `publish_unknown_recent_seasons` 726 athletes are
      withheld, 525 of them ranked. The narrower reading "born 2009 or later" would
      withhold 478 / 345 by birth year and publish the 240 athletes born 2008, some of
      whom are 17. `0` publishes everyone, including these 726).
- [ ] **`publish_unknown_recent_seasons`** (default 3: athletes without a birth year who
      started in the last three seasons are withheld too — 8 today).
- [ ] **Rows of withheld athletes.** A festival still lists them as "Jungschwinger, Name
      nicht veröffentlicht" with wins / draws / losses, grade sum and bouts (these are
      also their opponents' bouts), without name, club, Teilverband, birth year and
      without any rating shown. Laid beside the linked schlussgang.ch list, such a row can
      be matched to a person — the names are public there — and the rating that is not
      shown can be worked out (next list). Accept, or ask for these rows to be dropped
      (which also removes those bouts from the opponents' lists, and still does not hide
      the ratings: the opponents' rating changes remain).
- [ ] **`site_noindex`** (default on) — and its limits: the `noindex` tag covers the five
      HTML pages only. `robots.txt` is read by crawlers only at the root of a host; under
      `<user>.github.io/Schwinger-ELO/` it has no effect, and the JSON data files (names,
      ratings) cannot carry a tag. Enough, or use a custom domain (then `robots.txt` is
      at the root), or accept that the data files may be indexed?
- [ ] **`contact_email`** (default none: corrections and objections only through public
      GitHub issues — an athlete who objects has to do so in public).
- [ ] **Where the pipeline state lives.** Default: a draft release of this repository
      (below). It holds birthdays, licence numbers, residences and the source's PDFs; it
      is readable by every account with write access and becomes public with one click on
      "Publish release"; the job that reads and writes it needs `contents: write`.
      Alternative: a private store behind a secret (private repository or bucket) — not
      built; say so before opting in if you want it.
- [ ] Athlete links are not permanent (no id registry) — acceptable for now?

Change settings in `src/config.py` and commit; the workflows have no copies of them (a
test fails if a workflow sets one).

**What you accept by opting in** (known and not fixed; the reviewer's list)

- The state bundle is **one click from public** ("Publish release" on the draft) and
  readable by anyone with write access to the repository. The workflows check that the
  release is a draft before every download and upload; they cannot stop the click.
- **`noindex` covers the five HTML pages only**; `robots.txt` is ineffective under the
  project path and the JSON files may be indexed.
- **Objections go through public GitHub issues** as long as `contact_email` is empty.
- **Switching `PUBLISH_ENABLED` off stops updates only.** The deployed site, the draft
  release with the state, the workflow logs and the Pages artifacts of earlier runs stay
  until you remove them ("Turning it off" below).
- **An accepted change is consumed even if the deployment fails.** The guard's baseline
  is recorded and uploaded with the state before the `deploy` job runs. If that job then
  fails, the next run compares against the already accepted site and deploys the change
  without asking again. (Recording after the deployment would need a second 0.5 GB
  round trip of the state from a third job.)
- **Withheld athletes are not named and no rating is shown for them — but they are not
  hidden, and the site says so** (about page; decision of 2026-10-04). Their anonymous
  festival rows can be matched against the source list. Their ratings are calculated
  from public results like everyone's, and they can be worked out: measured with the
  published files and the public formula only, the pre-festival rating of every withheld
  athlete who met a published one (722 of the 726) follows from the published athletes'
  histories with a median error of 0.7 rating points (96 % within 5); and since this
  repository and the sources are public, running the pipeline with `publish_min_age = 0`
  gives all of them exactly. The comparison page also prints, per festival, what the
  bouts against unnamed opponents contributed together to a published athlete's rating.
  Closing this would mean coarser or missing ratings for the published athletes
  (rounding to 10 points still leaves a median error of 58 against 96 for a blind
  guess) — not done; the filter keeps minors out of the lists, the search and the
  rankings, which is what it is for.
- **The age filter depends on the source's birth years.** `check-site` fails when the
  number of withheld athletes drops, the number of published ones jumps or the birthdays
  disappear from the portraits (tolerances in `src/config.py`), and each January it lets
  through exactly the cohort that became certainly 18. A slow decay below the tolerances
  (e.g. new portraits without birthday) is not detected run by run: the unknown-birth-year
  rule then covers those debutants for three seasons.
- **Supply chain.** The publishing jobs install `requirements-lock.txt` with
  `--require-hashes` (exact versions, wheel hashes verified against the local
  installation; whether a GitHub runner is offered the same binary wheels is untested —
  if not, the install fails). `pip` itself and the actions (`actions/checkout@v5` …,
  major tags) are not pinned by hash — except `actions/upload-artifact`, which is pinned to
  the commit that `upload-pages-artifact@v4` itself uses (v4.6.2) — and project code plus these
  dependencies run in the same job that later holds the `contents: write` token: a job
  without third-party code is not possible while the state must not travel through a
  (publicly readable) artifact.
- **Workflow logs are public.** At the default log level no stage prints athlete names
  or ids (tested on the sample pipeline); a traceback from an unexpected crash could.
  The site's files are named after athletes (`data/history/history_<id>.json`), so the
  workflows do not use `actions/upload-pages-artifact` — its `tar` step lists every file
  it packs — but `pack-site`, which logs counts only, followed by the same upload step
  that action ends with.
- **The log of the first deployment (2026-10-04) does contain those file names.** It was
  made with `upload-pages-artifact@v4`, whose archive step printed 12,612 lines such as
  `data/history/history_<id>.json` and `data/bouts/bouts_<id>.json`: the ids (name slugs)
  of the *published* athletes — nobody the site withholds, nothing the site itself does
  not show, but in a place that was promised to hold no names. The owner can remove it:
  Actions → that run → "⋯" → **Delete all logs** (the run and the deployment stay).

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
python -m src.cli state-export ~/schwingen-state    # creates the directory (0700), bundle 0600
gh release create pipeline-state --draft \
  --title "Pipeline state - keep as DRAFT, never publish" \
  --notes "Private working data of the scheduled workflow. Not for publication." \
  ~/schwingen-state/schwingen-state-*.tar.gz
gh release view pipeline-state --json isDraft,assets   # isDraft must be true, one asset
rm -r ~/schwingen-state                                # the copy is no longer needed
```

Use a directory only you can read (not `/tmp`): the bundle holds birthdays and licence
numbers. `state-export` refuses a state whose cache is incomplete.

(Or in the browser: Releases → Draft a new release → tag `pipeline-state` → attach the
file → **Save draft**.) There must be no git tag called `pipeline-state`.

**5. Enable Pages:** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
This publishes nothing by itself.

**6. Opt in:** Settings → Secrets and variables → Actions → Variables → New repository
variable `PUBLISH_ENABLED` = `true` (or `gh variable set PUBLISH_ENABLED --body true`).
This alone starts nothing: no workflow has a schedule.

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

**Verify after the first push** (nothing of this could be checked without GitHub)

- [ ] `ci.yml` is green on both Python versions: the action tags resolve
      (`actions/checkout@v5`, `actions/setup-python@v6`), Chrome is found on the runner
      (the browser smoke test may not skip), and **the hashed lock installs on Python
      3.14** (`--require-hashes -r requirements-lock.txt`). If that install fails on a
      hash, the runner was offered other wheel files than this machine: regenerate the
      lock on Linux x86_64 / Python 3.14 (`scripts/make_lock.py`, or
      `pip-compile --generate-hashes`) before opting in.
- [ ] With `PUBLISH_ENABLED` unset, "Run workflow" on `deploy_pages.yml` gives a run
      whose jobs are **skipped**, and so does one on `scrape_and_update.yml`.
- [ ] After step 4: `gh release view pipeline-state --json isDraft` says `true`, and the
      release is not listed for a signed-out browser (`…/releases` shows nothing).
- [ ] After step 7: the local action `./.github/actions/pipeline-state` found the draft
      with the workflow token (steps "Download the pipeline state" and "Upload the new
      state" green), `deploy-pages@v4` resolved, the site is served under
      `/Schwinger-ELO/`, and the run's log shows no athlete name. **The first deploy
      run (2026-10-04) failed the last point:** the action then in use
      (`upload-pages-artifact@v4`) listed every file of the site, named after the
      published athletes, in the log. Delete that log (Actions → the run → "Delete all
      logs"); see "Workflow logs are public" above.
- [ ] After the first deployment with `pack-site` (not yet run on GitHub): the step
      "Pack the site" prints one line with counts, "Upload the Pages artifact" uploads
      one file `artifact.tar` under the name `github-pages`, the job "Deploy" accepts
      it, and a search of the run's log for `history_` finds nothing. If "Deploy"
      rejects the artifact, nothing was published: the previous deployment stays online.
- [ ] Only if you ever start `scrape_and_update.yml` by hand: the "Incremental crawl"
      step reports on the order of 100–350 network requests (not thousands), and
      schlussgang.ch answered the runner.
- [ ] Consider pinning the actions to commit SHAs (Dependabot can maintain them).

### Updating the site

Nothing reaches the public site by itself: a merge to `main` does not deploy and GitHub
never crawls. The site changes only when "Deploy to GitHub Pages" is started by hand
(with `PUBLISH_ENABLED` = `true`). There are two kinds of update.

**A. New code or page changes (no new data)** — for example a merged pull request.

1. Merge the pull request into `main` and wait for the green CI run on `main`.
2. Start the deployment, either
   - in the browser (also on a phone): open
     `https://github.com/<user>/Schwinger-ELO/actions/workflows/deploy_pages.yml` →
     **Run workflow** → branch `main` → leave `accept_changes` off, or
   - in a terminal: `gh workflow run deploy_pages.yml && gh run watch`.
3. When the run is green (about ten minutes), reload the site; a browser may show the
   old scripts until its cache is refreshed.

The run rebuilds the site from the state bundle on the draft release with the code of
`main`; it makes no request to schlussgang.ch. If the change makes the site smaller or
publishes more athletes on purpose (for example another `publish_min_age`), the deploy
guard stops the run: start it once more with `accept_changes` ticked.

**B. New festival results (crawl locally)** — needs your machine, on an up-to-date
`main` (`git switch main && git pull`). New festivals are fetched here, the state is
uploaded and the same deploy workflow rebuilds the site from it.

```bash
python -m src.cli all                                # incremental crawl + rebuild
python -m src.cli serve                              # look at it
python -m src.cli check-site --record                # deploy guard, new baseline
old="$(gh release view pipeline-state --json assets --jq '.assets[].name')"
python -m src.cli state-export ~/schwingen-state
gh release upload pipeline-state ~/schwingen-state/schwingen-state-*.tar.gz
for a in $old; do gh release delete-asset pipeline-state "$a" --yes; done
gh release view pipeline-state --json isDraft,assets # still a draft, exactly one asset
rm -r ~/schwingen-state
gh workflow run deploy_pages.yml && gh run watch
```

(`./scripts/deploy_local.sh --no-serve` does the first line including the virtual
environment.) The upload is the whole state, about 500 MB. The new bundle is uploaded
before the old one is removed, so a failed upload leaves the previous state in place.
Should two bundles ever be on the release, the deploy run imports the newer one (by its
creation time) and removes both after its own upload. If `check-site` fails locally,
read its numbers before anything is uploaded — see "Afterwards".

**After either:** `gh release view pipeline-state --json isDraft` must still say `true`.

**Afterwards**

- A red "Scrape and update" run deploys nothing. If the deploy guard stopped it
  ("check-site: FAILED"), read the numbers in the log; if the change is intended (for
  example after changing `publish_min_age`), start the workflow by hand with
  `accept_changes` ticked, once. **Do not accept a drop of the withheld athletes or a
  jump of the published ones without knowing why** — that is what a broken age filter
  looks like (birthdays missing in the source). The yearly release of the cohort that
  became 18 needs no override.
- If the state is lost or damaged, repeat step 4 from your machine (delete the old asset
  first). The next run catches up on what is missing, within the request caps.
- The state bundle grows by roughly 60 MB a season; a release asset may be 2 GB.

**Turning it off**

- Stop updates: `gh variable set PUBLISH_ENABLED --body false` (or delete the variable).
  **Nothing else changes:** the deployed site stays online, the state stays on the draft
  release, the logs and Pages artifacts of earlier runs stay.
- Take the site down: Settings → Pages → "Unpublish site" (or set the source to None).
- Remove the working data from GitHub: `gh release delete pipeline-state --yes`.
- Remove what earlier runs left: delete the workflow runs (Actions → run → "Delete
  workflow run"; this removes their logs and artifacts), or let them expire.
