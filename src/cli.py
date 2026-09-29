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

from src.config import Config, load_config

log = logging.getLogger("schwingen")


# --------------------------------------------------------------------------- stages
def cmd_crawl(cfg: Config) -> int:
    if cfg.sample:
        log.info("crawl: --sample mode, using %s (no network)", cfg.sample_dir)
        return 0
    log.info(
        "crawl: years %d-%d, refresh=%s, cache=%s",
        cfg.from_year, cfg.to_year, cfg.refresh, cfg.raw_dir,
    )
    log.warning("crawl: not implemented yet (Phase 1) - nothing fetched")
    return 0


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
        sp = add(name, "discover and fetch festivals" if name == "crawl"
                 else "crawl -> parse -> clean -> elo -> build")
        sp.add_argument("--from-year", type=int, default=None)
        sp.add_argument("--to-year", type=int, default=None)
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
