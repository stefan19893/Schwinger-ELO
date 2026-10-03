"""Single entry point for every pipeline stage (spec §5).

    python -m src.cli {crawl|parse|clean|elo|build|all|serve} [options]

Global options (``--sample``, ``--data-dir``, ``--skip-crawl``, ``--refresh``,
``-v``) are accepted before or after the subcommand. Local runs
(``scripts/deploy_local.sh``) and CI call exactly this CLI.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import logging
import sys
from collections.abc import Callable, Sequence

from typing import TYPE_CHECKING

import httpx

from tqdm import tqdm

from src.config import Config, load_config

if TYPE_CHECKING:
    import sqlite3

    from src.db import Festival
    from src.scraper.client import HttpClient
    from src.scraper.fests_crawler import CrawlReport

log = logging.getLogger("schwingen")


# --------------------------------------------------------------------------- stages
def cmd_crawl(cfg: Config, transport: httpx.BaseTransport | None = None,
              portraits: bool = True, portraits_only: bool = False) -> int:
    """Discover festivals (schlussgang.ch JSON:API) into the SQLite ``festivals`` table,
    then download statistic + ranking PDFs and the athlete portraits.
    ``portraits_only`` skips the listings and PDFs (no festival request at all)."""
    if cfg.sample:
        return _crawl_sample(cfg)
    from src.db import connect
    from src.scraper import fests_crawler as fc
    from src.scraper.client import FetchError, client_from_config

    if portraits_only:
        log.info("crawl: portraits only, offline=%s, cache=%s", cfg.offline, cfg.raw_dir)
        with client_from_config(cfg, transport=transport, offline=cfg.offline) as client:
            rc = _crawl_portraits(cfg, client)
            log.info("crawl: %d network requests (%d retries), %d cache hits",
                     client.stats.network_requests, client.stats.retries,
                     client.stats.cache_hits)
        return rc
    log.info("crawl: years %d-%d, refresh=%s, offline=%s, cache=%s, db=%s",
             cfg.from_year, cfg.to_year, cfg.refresh, cfg.offline, cfg.raw_dir, cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        with client_from_config(cfg, transport=transport, offline=cfg.offline) as client:
            try:
                report = fc.crawl_festivals(
                    client, conn, cfg.from_year, cfg.to_year,
                    current_max_age=cfg.listing_max_age_hours * 3600,
                    final_grace_days=cfg.listing_final_grace_days,
                    max_requests=cfg.crawl_max_requests,
                    exclude_flags=_exclude_flags(cfg),
                    progress=sys.stderr.isatty(),
                )
            except fc.CrawlLimitExceeded as exc:
                log.error("crawl: %s - finished years are saved; raise "
                          "SCHWINGEN_CRAWL_MAX_REQUESTS or narrow --from-year/--to-year", exc)
                return 1
            except FetchError as exc:
                log.error("crawl: %s - finished years are saved, re-run to resume", exc)
                return 1
            _log_crawl_report(report)
            pdf_rc = _crawl_pdfs(cfg, client, conn) if cfg.crawl_pdfs else 0
            if cfg.crawl_pdfs:
                pdf_rc = _crawl_ranking_pdfs(cfg, client, conn) or pdf_rc
            if portraits:
                pdf_rc = _crawl_portraits(cfg, client) or pdf_rc
            stats = client.stats
    finally:
        conn.close()
    log.info("crawl: %d network requests (%d retries), %d cache hits",
             stats.network_requests, stats.retries, stats.cache_hits)
    if report.cache_misses:
        log.error("crawl: --offline: %d listing queries not in cache (%s) - run without "
                  "--offline to fetch them", len(report.cache_misses),
                  ", ".join(report.cache_misses[:10])
                  + (" ..." if len(report.cache_misses) > 10 else ""))
        return 1
    return pdf_rc


def _crawl_pdfs(cfg: Config, client: HttpClient, conn: sqlite3.Connection) -> int:
    """Download (or confirm cached) the statistic PDF of every active festival."""
    import datetime as dt

    from src.db import load_festivals
    from src.scraper.statistic_pdfs import PdfLimitExceeded, download_statistic_pdfs

    seen: set[str] = set()
    todo = []
    for f in sorted(load_festivals(conn).values(), key=lambda f: (f.date, f.fest_id)):
        if f.kind == "active" and not f.cancelled and f.statistic_pdf_url \
                and f.statistic_pdf_url not in seen:
            seen.add(f.statistic_pdf_url)
            todo.append(f)
    log.info("crawl: statistic PDFs for %d active festivals (cap %d network requests)",
             len(todo), cfg.pdf_max_requests)
    before = client.stats.network_requests
    try:
        rep = download_statistic_pdfs(
            client, tqdm(todo, desc="pdfs", unit="pdf", disable=not sys.stderr.isatty()),
            today=dt.date.today(), max_requests=cfg.pdf_max_requests,
            max_age_hours=cfg.pdf_max_age_hours, grace_days=cfg.pdf_final_grace_days)
    except PdfLimitExceeded as exc:
        log.error("crawl: %s after %d requests - downloaded PDFs are cached, re-run to "
                  "continue", exc, client.stats.network_requests - before)
        return 1
    except RuntimeError as exc:
        log.error("crawl: %s", exc)
        return 1
    log.info("crawl: PDFs: %d downloaded, %d cached, %d failed%s, %d not cached (offline)",
             rep.fetched, rep.cached, len(rep.failed),
             f" (HTTP {rep.status_counts})" if rep.status_counts else "", len(rep.missing))
    for fid, err in rep.failed:
        log.warning("crawl: PDF of festival %d failed: %s", fid, err)
    rep.missing += _crawl_interim_sheets(client, todo, cfg)
    if rep.missing:
        log.error("crawl: --offline: %d statistic PDFs not in cache", len(rep.missing))
        return 1
    return 0


def _crawl_ranking_pdfs(cfg: Config, client: HttpClient, conn: sqlite3.Connection) -> int:
    """Download (or confirm cached) the Schlussrangliste PDF of every active festival
    (Phase 3 identity evidence), highest tiers first, own request cap."""
    import datetime as dt

    from src.db import load_festivals
    from src.scraper.ranking_pdfs import (download_ranking_pdfs, festivals_with_ranking,
                                          ranking_max_requests)
    from src.scraper.statistic_pdfs import PdfLimitExceeded

    todo = festivals_with_ranking(load_festivals(conn).values())
    cap = ranking_max_requests()
    log.info("crawl: ranking PDFs for %d active festivals (cap %d network requests)",
             len(todo), cap)
    before = client.stats.network_requests
    try:
        rep = download_ranking_pdfs(
            client, tqdm(todo, desc="rankings", unit="pdf", disable=not sys.stderr.isatty()),
            today=dt.date.today(), max_requests=cap, max_age_hours=cfg.pdf_max_age_hours,
            grace_days=cfg.pdf_final_grace_days)
    except PdfLimitExceeded as exc:
        log.error("crawl: %s after %d requests - downloaded PDFs are cached, re-run to "
                  "continue", exc, client.stats.network_requests - before)
        return 1
    except RuntimeError as exc:
        log.error("crawl: %s", exc)
        return 1
    log.info("crawl: ranking PDFs: %d downloaded, %d cached, %d failed%s, %d not cached "
             "(offline)", rep.fetched, rep.cached, len(rep.failed),
             f" (HTTP {rep.status_counts})" if rep.status_counts else "", len(rep.missing))
    for fid, err in rep.failed:
        log.warning("crawl: ranking PDF of festival %d failed: %s", fid, err)
    if rep.missing:
        log.error("crawl: --offline: %d ranking PDFs not in cache", len(rep.missing))
        return 1
    return 0


def _crawl_portraits(cfg: Config, client: HttpClient) -> int:
    """Download (or confirm cached) all schlussgang portraits and the 2023+
    festival -> portrait listings (Phase 3 identity evidence)."""
    from src.scraper.client import FetchError
    from src.scraper.portraits import (PortraitLimitExceeded, crawl_portraits,
                                       portrait_max_requests)

    cap = portrait_max_requests()
    before = client.stats.network_requests
    try:
        rep = crawl_portraits(client, to_year=cfg.to_year, max_requests=cap,
                              listing_max_age=cfg.listing_max_age_hours * 3600,
                              grace_days=cfg.listing_final_grace_days)
    except (PortraitLimitExceeded, FetchError) as exc:
        log.error("crawl: portraits: %s after %d requests - cached pages are kept, re-run "
                  "to continue", exc, client.stats.network_requests - before)
        return 1
    log.info("crawl: portraits: %d on %d pages, %d festival appearances (%d queries), "
             "%d network requests", rep.portraits, rep.pages, rep.appearances,
             rep.event_queries, client.stats.network_requests - before)
    if rep.cache_misses:
        log.error("crawl: --offline: %d portrait queries not in cache (%s)",
                  len(rep.cache_misses), ", ".join(rep.cache_misses[:5]))
        return 1
    return 0


def _crawl_interim_sheets(client: HttpClient, fests: list[Festival], cfg: Config) -> list[int]:
    """Interim statistic sheets registered in ``supplements.INTERIM_SHEETS`` (cached
    after the first download). Returns festivals whose sheet is missing offline."""
    import datetime as dt

    from src.scraper.client import CacheMiss, FetchError
    from src.scraper.supplements import INTERIM_SHEETS, fetch_interim_sheet

    missing: list[int] = []
    for f in fests:
        if f.fest_id not in INTERIM_SHEETS:
            continue
        try:
            res = fetch_interim_sheet(client, f, dt.date.today(), cfg.pdf_max_age_hours,
                                      cfg.pdf_final_grace_days)
            log.info("crawl: interim sheet of festival %d %s", f.fest_id,
                     "cached" if res is not None and res.from_cache else "downloaded")
        except CacheMiss:
            missing.append(f.fest_id)
        except FetchError as exc:
            log.warning("crawl: interim sheet of festival %d failed: %s", f.fest_id, exc)
    return missing


def _exclude_flags(cfg: Config) -> tuple[str, ...]:
    return tuple(x.strip() for x in cfg.elo_exclude_flags.split(",") if x.strip())


def _crawl_sample(cfg: Config) -> int:
    """--sample: load committed listing JSON from the sample dataset, no network."""
    import json

    from src.db import connect, upsert_festivals
    from src.scraper.fests_crawler import apply_eligibility, load_listing_files

    files = sorted((cfg.sample_dir / "schlussgang").glob("events_*.json"))
    log.info("crawl: --sample mode, %d listing file(s) from %s (no network)",
             len(files), cfg.sample_dir)
    parsed = load_listing_files(json.loads(f.read_text(encoding="utf-8")) for f in files)
    for sk in parsed.skipped:
        log.warning("crawl: skipped event %s %r: %s", sk.fest_id, sk.name, sk.reason)
    exclude = _exclude_flags(cfg)
    parsed.festivals = [apply_eligibility(f, exclude) for f in parsed.festivals]
    conn = connect(cfg.db_path)
    try:
        up = upsert_festivals(conn, parsed.festivals)
    finally:
        conn.close()
    log.info("crawl: %d festivals (%d new, %d updated, %d unchanged), %d skipped -> %s",
             len(parsed.festivals), up.inserted, up.updated, up.unchanged,
             len(parsed.skipped), cfg.db_path)
    return 0


def _log_crawl_report(report: CrawlReport) -> None:
    from collections import Counter

    from src.db import CATEGORIES

    counts = report.counts()
    years = sorted({y for y, _ in counts})
    extra = sorted({c for _, c in counts if c not in CATEGORIES})
    cols = [*CATEGORIES, *extra]
    log.info("crawl: festivals per year (active by category, other kinds in [brackets]):")
    log.info("  %-4s %s  %5s %6s", "year", " ".join(f"{c[:10]:>10}" for c in cols), "total",
             "w/stat")
    stat = Counter(f.year for f in report.festivals.values()
                   if f.kind == "active" and f.statistic_pdf_url)
    for y in years:
        row = [counts.get((y, c), 0) for c in cols]
        log.info("  %-4d %s  %5d %6d", y, " ".join(f"{n:>10}" for n in row), sum(row), stat[y])
    log.info("crawl: %d festivals stored (%d new, %d updated, %d unchanged), "
             "%d future (not stored), %d skipped/invalid, %d queries / %d pages",
             len(report.festivals), report.upsert.inserted, report.upsert.updated,
             report.upsert.unchanged, report.future, len(report.skipped),
             report.queries, report.pages)
    for sk in report.skipped:
        log.warning("crawl: skipped %s %r: %s", sk.fest_id, sk.name, sk.reason)
    esaf = Counter(f.eidg_type for f in report.festivals.values() if f.category == "ESAF")
    log.info("crawl: ESAF tier by eidg_type: %s",
             ", ".join(f"{k}={v}" for k, v in sorted(esaf.items())) or "none")
    unknown = Counter(f.name.rsplit(" ", 1)[0] for f in report.reference_unknown)
    log.info("crawl: reference check: %d mismatches, %d Kranzfeste not in reference list%s",
             len(report.reference_mismatches), len(report.reference_unknown),
             f" (e.g. {', '.join(n for n, _ in unknown.most_common(5))})" if unknown else "")


def cmd_parse(cfg: Config, force: bool = False) -> int:
    """Parse cached statistic PDFs into SQLite bouts / athletes_raw (offline)."""
    from src.db import connect
    from src.scraper.client import client_from_config
    from src.scraper.parse_runner import PARSER_VERSION, parse_all

    if cfg.sample:
        return _parse_sample(cfg, force=force)
    log.info("parse: db=%s, cache=%s (offline), parser v%d%s", cfg.db_path, cfg.raw_dir,
             PARSER_VERSION, ", force" if force else "")
    conn = connect(cfg.db_path)
    try:
        with client_from_config(cfg, offline=True) as client:
            rep = parse_all(conn, client, min_pair_rate=cfg.parse_min_pair_rate, force=force,
                            progress=sys.stderr.isatty(),
                            pdf_max_age_hours=cfg.pdf_max_age_hours,
                            pdf_grace_days=cfg.pdf_final_grace_days)
            _log_parse_summary(conn, rep.parsed, rep.unchanged)
            _parse_identity_evidence(cfg, conn, client, force=force)
    finally:
        conn.close()
    return 0


def _log_parse_summary(conn: sqlite3.Connection, parsed: int, unchanged: int) -> None:
    q = conn.execute
    log.info("parse: %d festivals (re)parsed, %d unchanged", parsed, unchanged)
    status = dict(q("SELECT status, COUNT(*) FROM festival_parse GROUP BY status").fetchall())
    log.info("parse: festival status: %s", ", ".join(f"{k}={v}" for k, v in sorted(status.items())))
    n_bouts, draws = q("SELECT COUNT(*), SUM(outcome = 'DRAW') FROM bouts").fetchone()
    n_entries = q("SELECT COALESCE(SUM(n_entries), 0) FROM festival_parse").fetchone()[0]
    log.info("parse: %d bouts (%.1f%% draws), %d raw athletes, %.1f%% of %d entries paired",
             n_bouts, 100 * (draws or 0) / max(n_bouts, 1),
             q("SELECT COUNT(*) FROM athletes_raw").fetchone()[0],
             100 * 2 * n_bouts / max(n_entries, 1), n_entries)
    top = q("SELECT reason, COUNT(*) n FROM parse_rejects GROUP BY reason ORDER BY n DESC "
            "LIMIT 8").fetchall()
    log.info("parse: top rejects: %s", ", ".join(f"{r}={n}" for r, n in top))


def _parse_identity_evidence(cfg: Config, conn: sqlite3.Connection, client: HttpClient | None,
                             force: bool = False) -> None:
    """Phase 3 evidence (offline): Schlussranglisten -> ranking_entries (linked to
    athletes_raw), portraits -> portraits / portrait_appearances, then clubs and
    athlete_evidence. ``client`` None = --sample (ranking lists and portrait JSON from
    the sample dir)."""
    from src.scraper.evidence import build_evidence
    from src.scraper.portraits import load_portraits, load_portraits_from_dir
    from src.scraper.ranking_runner import parse_rankings, parse_rankings_from_dir

    if client is None:
        rrep = parse_rankings_from_dir(conn, cfg.sample_dir / "ranking", force=force)
    else:
        rrep = parse_rankings(conn, client, force=force, progress=sys.stderr.isatty(),
                              pdf_max_age_hours=cfg.pdf_max_age_hours,
                              pdf_grace_days=cfg.pdf_final_grace_days)
    log.info("parse: ranking lists: %d (re)parsed, %d unchanged, status %s; %d entries "
             "linked to athletes_raw (%s), unlinked %s", rrep.parsed, rrep.unchanged,
             dict(rrep.status), rrep.linked, dict(rrep.link_methods), dict(rrep.unlinked))
    if client is None:
        prep = load_portraits_from_dir(conn, cfg.sample_dir / "schlussgang")
    else:
        prep = load_portraits(conn, client, to_year=cfg.to_year)
    if prep.cache_misses:
        log.warning("parse: portraits: %d queries not cached (run `crawl`): %s",
                    len(prep.cache_misses), ", ".join(m[:120] for m in prep.cache_misses[:3]))
    log.info("parse: portraits: %d portraits, %d festival appearances, linked %s, "
             "not stored %s", prep.portraits, prep.appearances, dict(prep.linked),
             dict(+prep.skipped))
    erep = build_evidence(conn)
    log.info("parse: athlete evidence: %d rows, %d with club (%d canonical clubs), "
             "Teilverband by source %s, %d with portrait", erep.rows, erep.with_club,
             erep.clubs, dict(erep.with_sub), erep.with_portrait)


def _parse_sample(cfg: Config, force: bool = False) -> int:
    """--sample: parse the committed sheets in tests/fixtures/sample/statistic/ (no network)."""
    from src.db import connect
    from src.scraper.parse_runner import parse_from_dir

    directory = cfg.sample_dir / "statistic"
    log.info("parse: --sample mode, sheets from %s (no network), db=%s", directory, cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        rep = parse_from_dir(conn, directory, min_pair_rate=cfg.parse_min_pair_rate, force=force)
        _log_parse_summary(conn, rep.parsed, rep.unchanged)
        _parse_identity_evidence(cfg, conn, None, force=force)
    finally:
        conn.close()
    return 0


def cmd_clean(cfg: Config) -> int:
    """Identity resolution: SQLite (read-only) -> data/processed/*.parquet (idempotent)."""
    import time

    from src.pipeline.cleaner import run_clean

    if cfg.sample and not _sample_db_ready(cfg):
        log.info("clean: --sample db at %s is empty, building it first (crawl + parse --sample)",
                 cfg.db_path)
        rc = _crawl_sample(cfg) or _parse_sample(cfg)
        if rc:
            return rc
    if not cfg.db_path.is_file():
        log.error("clean: %s not found - run `crawl` and `parse` first", cfg.db_path)
        return 1
    t0 = time.perf_counter()
    result = run_clean(cfg.db_path, cfg.processed_dir)
    log.info("clean: done in %.1f s -> %s (%s)", time.perf_counter() - t0, cfg.processed_dir,
             ", ".join(f"{k}={v}" for k, v in result.counts.items()))
    return 0


def _sample_db_ready(cfg: Config) -> bool:
    """The --sample db exists and holds parsed sheets (read-only check)."""
    import sqlite3

    if not cfg.db_path.is_file():
        return False
    conn = sqlite3.connect(f"file:{cfg.db_path.resolve()}?mode=ro", uri=True)
    try:
        return bool(conn.execute("SELECT EXISTS (SELECT 1 FROM sqlite_master "
                                 "WHERE name = 'bouts')").fetchone()[0]
                    and conn.execute("SELECT EXISTS (SELECT 1 FROM bouts)").fetchone()[0])
    finally:
        conn.close()


def cmd_elo(cfg: Config) -> int:
    log.warning("elo: not implemented yet (Phase 4) - out=%s", cfg.processed_dir)
    return 0


def cmd_build(cfg: Config) -> int:
    from src.exporter.static_builder import build_site

    dist = build_site(cfg)
    log.info("build: wrote placeholder site to %s", dist)
    return 0


def cmd_all(cfg: Config, skip_crawl: bool = False, portraits: bool = True) -> int:
    stages: list[tuple[str, Callable[[Config], int]]] = [
        ("crawl", functools.partial(cmd_crawl, portraits=portraits)),
        ("parse", cmd_parse),
        ("clean", cmd_clean),
        ("elo", cmd_elo),
        ("build", cmd_build),
    ]
    for name, fn in stages:
        if name == "crawl" and skip_crawl:
            log.info("crawl: skipped (--skip-crawl)")
            continue
        rc = fn(cfg)
        if rc != 0:
            log.error("%s failed with exit code %d", name, rc)
            return rc
    return 0


def cmd_serve(cfg: Config) -> int:
    dist = cfg.dist_dir
    if not (dist / "index.html").is_file():
        log.error("serve: %s/index.html missing - run `python -m src.cli build` first", dist)
        return 1
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(dist))
    try:
        httpd = http.server.ThreadingHTTPServer((cfg.host, cfg.port), handler)
    except OSError as exc:
        log.error("serve: cannot bind %s:%d (%s) - port in use? try --port N",
                  cfg.host, cfg.port, exc.strerror or exc)
        return 1
    with httpd:
        print(f"Serving {dist} at http://localhost:{cfg.port}/  (Ctrl+C to stop)", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


# --------------------------------------------------------------------------- parser
def _add_global_options(p: argparse.ArgumentParser, suppress: bool) -> None:
    """Add global options. On subparsers use SUPPRESS so they don't clobber
    values given before the subcommand."""
    d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
    p.add_argument("--sample", action="store_true", default=d(False),
                   help="use the offline dataset in tests/fixtures/sample/ (no network)")
    p.add_argument("--data-dir", default=d(None), help="data directory (default: ./data)")
    p.add_argument("--skip-crawl", action="store_true", default=d(False),
                   help="in `all`: use only already cached data")
    p.add_argument("--refresh", action="store_true", default=d(False),
                   help="re-fetch pages even if cached")
    p.add_argument("--offline", action="store_true", default=d(False),
                   help="crawl from the data/raw cache only; report cache misses, never fetch")
    p.add_argument("-v", "--verbose", action="store_true", default=d(False),
                   help="debug logging")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description=__doc__.split("\n")[0])
    _add_global_options(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        _add_global_options(sp, suppress=True)
        return sp

    for name in ("crawl", "all"):
        sp = add(name, "discover festivals and download statistic PDFs" if name == "crawl"
                 else "crawl -> parse -> clean -> elo -> build")
        sp.add_argument("--from-year", type=int, default=None)
        sp.add_argument("--to-year", type=int, default=None)
        sp.add_argument("--no-pdfs", action="store_true", default=False,
                        help="only crawl festival listings, skip statistic / ranking PDFs")
        sp.add_argument("--no-portraits", action="store_true", default=False,
                        help="skip the schlussgang athlete portraits")
        if name == "crawl":
            sp.add_argument("--portraits-only", action="store_true", default=False,
                            help="only download the athlete portraits and the 2023+ "
                                 "festival -> portrait listings (no festival listings, no PDFs)")
    sp = add("parse", "parse cached statistic PDFs into SQLite (offline)")
    sp.add_argument("--force", action="store_true", default=False,
                    help="re-parse festivals even if PDF and parser version are unchanged")
    add("clean", "identity resolution -> data/processed/{bouts,athletes,festivals,"
                 "identity_map,bout_rejects}.parquet (reads SQLite read-only)")
    add("elo", "compute ratings -> data/processed/ratings.parquet")
    add("build", "write the static site to dist/")
    sp = add("serve", "serve dist/ over HTTP")
    sp.add_argument("--port", type=int, default=None, help="port (default 8000)")
    sp.add_argument("--host", default=None, help="bind address (default 127.0.0.1)")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    overrides = {
        "data_dir": args.data_dir,
        "from_year": getattr(args, "from_year", None),
        "to_year": getattr(args, "to_year", None),
        "port": getattr(args, "port", None),
        "host": getattr(args, "host", None),
        # Flags only override when set; otherwise env/defaults apply.
        "sample": True if args.sample else None,
        "refresh": True if args.refresh else None,
        "offline": True if args.offline else None,
        "crawl_pdfs": False if getattr(args, "no_pdfs", False) else None,
    }
    return load_config(overrides)


COMMANDS: dict[str, Callable[[Config], int]] = {
    "crawl": cmd_crawl,
    "parse": cmd_parse,
    "clean": cmd_clean,
    "elo": cmd_elo,
    "build": cmd_build,
    "serve": cmd_serve,
}


# Options that are accepted globally but only affect some subcommands.
_OPTION_SCOPE: dict[str, frozenset[str]] = {
    "skip_crawl": frozenset({"all"}),
    "refresh": frozenset({"crawl", "all"}),
    "offline": frozenset({"crawl", "all"}),
}


def _warn_ignored_options(args: argparse.Namespace) -> None:
    for opt, commands in _OPTION_SCOPE.items():
        if getattr(args, opt) and args.command not in commands:
            flag = "--" + opt.replace("_", "-")
            log.warning("%s has no effect on `%s` (only: %s)",
                        flag, args.command, ", ".join(sorted(commands)))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:  # httpx logs every request URL at INFO
        logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = config_from_args(args)
    except (ValueError, KeyError) as exc:
        log.error("configuration error: %s", exc)
        return 2
    _warn_ignored_options(args)
    try:
        if args.command == "all":
            return cmd_all(cfg, skip_crawl=args.skip_crawl, portraits=not args.no_portraits)
        if args.command == "parse":
            return cmd_parse(cfg, force=args.force)
        if args.command == "crawl":
            if args.portraits_only and args.no_portraits:
                raise ValueError("--portraits-only and --no-portraits exclude each other")
            return cmd_crawl(cfg, portraits=not args.no_portraits,
                             portraits_only=args.portraits_only)
        return COMMANDS[args.command](cfg)
    except ValueError as exc:
        log.error("%s: %s", args.command, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
