# Project State

**Last updated:** 2026-09-29
**Current phase:** 2 — Bout parser (in progress, branch `phase-2-parser`)
**Next action:** Phase 2, task 6 — data-quality report (bouts per year, draw rate, parse failures) in handoff notes.

## Phases
| # | Phase | Spec milestone | File | Status |
|---|---|---|---|---|
| 0 | Bootstrap + CLI + local script | M0 | `phase-0-bootstrap.md` | done |
| 1 | Festival crawler | M1 | `phase-1-crawler.md` | done |
| 2 | Bout parser | M2 | `phase-2-parser.md` | in progress |
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
- 2026-09-29 — ~~K-factor keys follow spec §4.1 categories; `Gauverband` and `Regional` both K = 16~~ **SUPERSEDED 2026-09-29 by user decision: Gauverband K = 24 (see below).** Original reason: spec §4.2 lists only "Regional-/Rangschwinget" for the lowest tier.
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
- 2026-09-29 — Primary and only source = schlussgang.ch (website + `backend-api.schlussgang.ch/jsonapi`) — user decision (relayed by coordinator); robots.txt `Allow: /`, no terms page prohibiting crawling.
- 2026-09-29 — esv.ch is NOT crawled at all (its Nutzungsbedingungen forbid scraping); only the ESV Anlass id from schlussgang (`field_event_esv_id`) is stored as a reference field.
- 2026-09-29 — Festival index via JSON:API `backend-api.schlussgang.ch/jsonapi/node/event` instead of HTML scraping — same data as /resultate, structured, includes category/association/PDF links (coordinator: prefer JSON API).
- 2026-09-29 — Bout source for Phase 2 = "Statistische Tabelle" PDFs linked from each event node (`field_final_statistic_pdf` or `field_event_pdf` item described "Statistik") — the only place schlussgang exposes opponent + grade per Gang.
- 2026-09-29 — `festivals.fest_id` = schlussgang node id (`drupal_internal__nid`, stable INTEGER); extra columns beyond spec §4.1 (kind, cancelled, source category, association, esv_id, event_type, participant_count, url, statistic/ranking PDF URLs, first/last_seen) — needed for Phase 2 and for traceability; schema in `src/db.py`, versioned via `PRAGMA user_version`.
- 2026-09-29 — ~~Kilchberg/Unspunnen/Jubiläum → Bergkranz~~ **SUPERSEDED 2026-09-29 by user decision (see below): they are ESAF tier.** Rest still valid: Category mapping (schlussgang tid → spec): 11 Eidgenössische Anlässe → `ESAF` only for the ESAF itself (name "Eidgenössisches Schwing…"), other competitive tid-11 events (Kilchberg, Unspunnen, Jubiläumsschwingfest 125 J. ESV) → `Bergkranz` (spec §4.2.3 puts Unspunnen/Kilchberg at K=40); 12 → `Bergkranz`; 13 → `Teilverband` (incl. Berner Kantonalschwingfest, which schlussgang files as the BKSV Teilverbandsfest); 15 → `Regional`; 14 Kantonal-/Gaufest → `Gauverband` iff the name matches a Bernese Gau festival (Mittelländisches, Oberländisches, Seeländisches, Bern-Jurassisches, Oberaargauisches, Emmentalisches) and doesn't contain "kantonal", else `Kantonal` (only BKSV has Gauverbände; Basel-Stadt / Glarner-Bündner count as Kantonal) — coordinator instruction + source taxonomy — **confirmed by user 2026-09-29**.
- 2026-09-29 — Youth (tid 10) and women's (tid 16) categories are not requested at all; events in tids 11–15 that are youth / women / non-competition (Abgeordnetenversammlung, Fussballturnier, awards…) are stored with `kind` ≠ 'active' and `category` NULL so nothing is dropped silently; ELO uses only `kind='active'` — adult men's competition only, halves request count — **confirmed by user 2026-09-29**.
- 2026-09-29 — Listing pages for the current season (year ≥ today's year) are re-fetched when their cache entry is older than 24 h (`max_age`); all other pages are cached forever unless `--refresh` — otherwise new festivals could never be discovered without a full `--refresh`, which in Phase 2 would re-download every PDF. (slight relaxation of "never re-fetch cached pages") — **confirmed by user 2026-09-29**.
- 2026-09-29 — Festivals dated after today are counted but not stored — no results yet; they are picked up by a later crawl.
- 2026-09-29 — Crawl safety cap `crawl_max_requests = 1500` network requests per run (`SCHWINGEN_CRAWL_MAX_REQUESTS`) and `listing_max_age_hours = 24` in `src/config.py` — enforce the agreed request budget in code, not by convention.
- 2026-09-29 — `tests/conftest.py` blocks all real httpx transport I/O in tests — a Phase 0 test accidentally triggered a live crawl once real crawling existed.
- 2026-09-29 — Tests split as `tests/test_client.py`, `tests/test_db.py`, `tests/test_fests_crawler.py` instead of one `test_scraper.py` (spec §3) — smaller files per module.
- 2026-09-29 — "Nationalturnen" events (tid 11) are stored as `kind='non_competition'` — not a Schwingfest; different scoring, no statistic PDF — **confirmed by user 2026-09-29**.
- 2026-09-29 — Legacy statistic-PDF selection is a tiered rule (description → empty-description filename → "(nach N Gängen)" final) — descriptions are free text with many variants; covered by parametrised tests.
- 2026-09-29 — Year-rollover cache rule: every year's listing uses `max_age = 24 h` until a copy was fetched after Dec 31 + `listing_final_grace_days` (60, Config); then cached forever — phase-1 review must-fix (late festivals / late PDF uploads were missed for past years). Supersedes the "current season only" part of the 24 h decision.
- 2026-09-29 — `request_delay_min >= 0.5` enforced in `load_config` and `HttpClient` (`MIN_REQUEST_DELAY`) — env vars must not be able to break politeness (phase-1 review).
- 2026-09-29 — Retry-After honoured on retryable statuses, capped at `retry_after_max = 300 s` (Config); wait = max(backoff, Retry-After), throttle still applies (phase-1 review).
- 2026-09-29 — `--offline` global option (crawl/all, env `SCHWINGEN_OFFLINE`): cache-only, serves stale copies, reports misses and exits 1; incompatible with `--refresh` (phase-1 review).
- 2026-09-29 — Test network guard also at socket level (`connect`/`connect_ex`, loopback + AF_UNIX allowed) (phase-1 review).
- 2026-09-29 — **User decision:** Kilchberger Schwinget, Unspunnen-Schwinget and ESV Jubiläumsschwingfest move to the ESAF tier (`category='ESAF'`, K = 48) — festivals with eidgenössischem Charakter awarding the eidgenössischer Kranz (reference data). New column `festivals.eidg_type` (`ESAF`/`Kilchberg`/`Unspunnen`/`Jubilaeum`, set iff category = ESAF; the real ESAF is `eidg_type='ESAF'`); schema `user_version` 2 with an in-place migration (ADD COLUMN, old ESAF rows → 'ESAF'). Unknown competitive tid-11 events keep the Bergkranz fallback.
- 2026-09-29 — **User decision:** Gauverband K = 24, same as Kantonal — the reference groups Kantonal- and Gauverbandsfeste as one Kranzfest tier; separate category labels kept.
- 2026-09-29 — User-supplied tier reference `src/scraper/reference/schwingfeste_schweiz.json` is used to *validate* (not override) the mapping: tests over all reference names + schlussgang spellings, and a crawl-time warning for active tid 11–14 festivals. Documented supplements in `src/scraper/festival_reference.py`: "Bern-Jurassisches Schwingfest" = Gauverband (real BKSV Gau festival the reference omits; BKSV has 6 Gaue) and the spelling "Basellandschaftliches". Not in the reference but correctly Kantonal: Tessiner, Jurassisches, "Baselbieter" Kantonalschwingfest, Jubiläums-Schwingfest 100 J. UKSV 2017.
- 2026-09-29 — PDF library = **pypdfium2** (PDFium; BSD/Apache) instead of pypdf — pypdf fails on malformed sheets (2013 Binningen), PDFium reads all 1879 in 11 s with identical text on normal sheets.
- 2026-09-29 — `crawl` downloads the statistic PDFs of active, non-cancelled festivals after the listings (`--no-pdfs` to skip); own cap `pdf_max_requests = 2500`; past-season PDFs cached forever, current-season PDFs re-checked after 24 h until fetched ≥ 14 days after the festival (`pdf_max_age_hours`, `pdf_final_grace_days`); stops after 10 consecutive download errors. `parse` works offline on the cache (spec §5).
- 2026-09-29 — **Correction:** statistic-sheet symbols are `+` win, `-` gestellt (draw), `o`/`0` loss. The Phase 1 note "Schlussgang loser printed as `o`" was a misreading — `o` is the normal loss symbol.
- 2026-09-29 — Bouts are built by pairing both athletes' entries; unpaired/unresolvable entries are rejects (not one-sided bouts) — both grades are needed for the MoV multiplier and one-sided entries are mostly source errors.
- 2026-09-29 — Injury-decided bouts (`u`, `> unfall`, 0.00 grade) are rejected as `forfeit_injury`, not rated — not a sporting result.
- 2026-09-29 — Gang number = position in the complete list (or the larger position); flagged `gang_inferred`/`gang_uncertain` when list positions disagree (~1 % of bouts) — only used to order bouts within a festival.
- 2026-09-29 — Same-name athletes in one sheet are disambiguated by the mirror entry; raw names (suffixes "1"/"2", birth years, S/T markers) are kept for Phase 3.
- 2026-09-29 — `parse_min_pair_rate = 0.5`: sheets where < 50 % of entries pair into bouts are treated as structurally unreliable and import no bouts (25 festivals / ~800 bouts in the dry run) — better to lose a few bouts than import misassigned ones. Max Gänge: 8 for the ESAF itself (`eidg_type='ESAF'`), else 6.
- 2026-09-30 — Schema v3: `festivals.event_flags` + `elo_eligible`, tables `festival_parse`, `athletes_raw`, `bouts`, `parse_rejects` (bout athlete ids are per-sheet `athletes_raw` ids until Phase 3 identity resolution). `parse` is incremental on PDF sha256 + `PARSER_VERSION`.
- 2026-09-30 — **AWAITING USER CONFIRMATION — borderline events:** all are parsed and stored; proposed ELO defaults via `elo_exclude_flags = "team,ausland"`: team competitions (Mannschaftsmeisterschaft) and Ausland festivals (Kanada/USA clubs, 52, mostly without statistic PDF) excluded; Jungaktive/U20 (11, 2021) and Hallenschwinget (142) included — U20/Jungaktive are regular active Schwinger in an age-limited field, indoor festivals are normal Rangschwinget. Changeable without code (`SCHWINGEN_ELO_EXCLUDE_FLAGS`).

## Open questions
- Phase 2: statistic PDFs for 13 Regional festivals 2012–2015 include youth categories ("inkl. Nachwuchs") — parser must keep only the active category.
- schlussgang lists only 3 Regional festivals for 2011 (vs ~100/year later) — accept the gap, or start ratings with a 2011 burn-in season? → decide in Phase 4.
- Phase 5/6: is publishing athlete ratings on GitHub Pages fine, given the underlying results are ESV data (ESV terms claim ownership) obtained via schlussgang.ch? → confirm with user before deploying.
- ELO parameters `alpha` and `BaselineDiff` (MoV multiplier) are unspecified → calibrate in Phase 4.
- How does the scheduled GitHub Action keep state between runs (commit processed Parquet, release asset, or cache)? → decide in Phase 6.


## Blockers
- none (2026-09-29: esv.ch-terms blocker resolved by user decision to use schlussgang.ch only)
