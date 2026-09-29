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

    from src.scraper.client import HttpClient
    from src.scraper.fests_crawler import CrawlReport

log = logging.getLogger("schwingen")


# --------------------------------------------------------------------------- stages
def cmd_crawl(cfg: Config, transport: httpx.BaseTransport | None = None) -> int:
    """Discover festivals (schlussgang.ch JSON:API) into the SQLite ``festivals`` table."""
    if cfg.sample:
        return _crawl_sample(cfg)
    from src.db import connect
    from src.scraper import fests_crawler as fc
    from src.scraper.client import FetchError, client_from_config

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
    if rep.missing:
        log.error("crawl: --offline: %d statistic PDFs not in cache", len(rep.missing))
        return 1
    return 0


def _crawl_sample(cfg: Config) -> int:
    """--sample: load committed listing JSON from the sample dataset, no network."""
    import json

    from src.db import connect, upsert_festivals
    from src.scraper.fests_crawler import load_listing_files

    files = sorted((cfg.sample_dir / "schlussgang").glob("events_*.json"))
    log.info("crawl: --sample mode, %d listing file(s) from %s (no network)",
             len(files), cfg.sample_dir)
    parsed = load_listing_files(json.loads(f.read_text(encoding="utf-8")) for f in files)
    for sk in parsed.skipped:
        log.warning("crawl: skipped event %s %r: %s", sk.fest_id, sk.name, sk.reason)
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


def cmd_parse(cfg: Config) -> int:
    log.warning("parse: not implemented yet (Phase 2) - db=%s", cfg.db_path)
    return 0


def cmd_clean(cfg: Config) -> int:
    log.warning("clean: not implemented yet (Phase 3) - out=%s", cfg.processed_dir)
    return 0


def cmd_elo(cfg: Config) -> int:
    log.warning("elo: not implemented yet (Phase 4) - out=%s", cfg.processed_dir)
    return 0


def cmd_build(cfg: Config) -> int:
    from src.exporter.static_builder import build_site

    dist = build_site(cfg)
    log.info("build: wrote placeholder site to %s", dist)
    return 0


def cmd_all(cfg: Config, skip_crawl: bool = False) -> int:
    stages: list[tuple[str, Callable[[Config], int]]] = [
        ("crawl", cmd_crawl),
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
                        help="only crawl festival listings, skip statistic PDF downloads")
    add("parse", "parse cached Notenblaetter into SQLite")
    add("clean", "identity resolution -> data/processed/*.parquet")
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
            return cmd_all(cfg, skip_crawl=args.skip_crawl)
        return COMMANDS[args.command](cfg)
    except ValueError as exc:
        log.error("%s: %s", args.command, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
