"""Pipeline state as one file: ``state-export`` / ``state-import`` and the cold-start check.

A scheduled run needs what earlier runs left behind: the HTTP cache (``data/raw``, every
response ever fetched), the staging database, the Parquet files and the deploy guard's
baseline (``published_meta.json``). Without them a crawl would re-request the whole archive
of schlussgang.ch - several thousand requests, more than the caps of one run allow. The
bundle moves that state between the owner's machine and a CI runner (see README, "Hosted
setup"), and :func:`state_problems` is what ``crawl`` / ``all --require-state`` consult
before the first request.

**The bundle is not for publication.** The cache and the database hold what the site
deliberately leaves out (birthdays, licence numbers, residences) and third-party PDFs.

Format (``FORMAT`` 1): a gzip-compressed tar with regular files only::

    bundle.json            {"format": 1, "created_at": "..."}   (first member)
    raw/**                 HTTP cache
    schwingen.db           consistent snapshot (SQLite backup API)
    processed/*.parquet
    published_meta.json    (if present)
    manifest.json          sizes and sha256 of every file above, counts   (last member)

``import_state`` unpacks into a temporary directory beside the target, verifies every file
against the manifest and only then moves the parts into place.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import io
import json
import logging
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from src.config import Config

log = logging.getLogger(__name__)

FORMAT = 1
BUNDLE_PREFIX = "schwingen-state-"
BUNDLE_SUFFIX = ".tar.gz"
HEADER, MANIFEST = "bundle.json", "manifest.json"
DB_NAME, BASELINE_NAME = "schwingen.db", "published_meta.json"
DIRS = ("raw", "processed")
TOP_LEVEL = (*DIRS, DB_NAME, BASELINE_NAME)
# `--require-state`: at least this share of the statistic PDFs of the festivals already in
# the database must be in the cache, otherwise the crawl would download them all again.
MIN_PDF_COVERAGE = 0.9
_CHUNK = 1 << 20


class StateError(RuntimeError):
    """The state (or a bundle of it) is missing, incomplete or damaged."""


@dataclass
class BundleInfo:
    path: Path
    created_at: str
    n_files: int
    n_bytes: int          # uncompressed payload
    counts: dict[str, int]


# --------------------------------------------------------------------------- cold start
def _db_counts(db_path: Path) -> dict[str, int]:
    """Row counts of the tables a warm state must have (read-only; missing table = 0)."""
    out = {"festivals": 0, "bouts": 0}
    if not db_path.is_file():
        return out
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        for table in out:
            try:
                out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.DatabaseError:
                out[table] = 0
    finally:
        conn.close()
    return out


def _pdf_coverage(cfg: Config) -> tuple[int, int]:
    """(cached, expected) statistic PDFs of the active festivals in the database."""
    from src.scraper.client import client_from_config

    conn = sqlite3.connect(f"file:{cfg.db_path.resolve()}?mode=ro", uri=True)
    try:
        urls = {r[0] for r in conn.execute(
            "SELECT statistic_pdf_url FROM festivals WHERE kind = 'active' AND NOT cancelled "
            "AND statistic_pdf_url IS NOT NULL AND statistic_pdf_url != ''")}
    except sqlite3.DatabaseError:
        return 0, 0
    finally:
        conn.close()
    with client_from_config(cfg, offline=True) as client:
        cached = sum(1 for u in urls if all(p.is_file() for p in client.cache_paths(u)))
    return cached, len(urls)


def state_problems(cfg: Config) -> list[str]:
    """Why ``cfg.data_dir`` is not the state of an earlier run (empty list = it is).

    Reads the local files only; never touches the network."""
    problems: list[str] = []
    if not cfg.db_path.is_file():
        return [f"{cfg.db_path} does not exist"]
    counts = _db_counts(cfg.db_path)
    for table, n in counts.items():
        if n == 0:
            problems.append(f"{cfg.db_path}: table `{table}` is empty or missing")
    if not cfg.raw_dir.is_dir() or not any(cfg.raw_dir.rglob("*.body")):
        problems.append(f"{cfg.raw_dir} holds no cached responses")
    elif counts["festivals"]:
        cached, expected = _pdf_coverage(cfg)
        if expected and cached < MIN_PDF_COVERAGE * expected:
            problems.append(f"only {cached} of the {expected} statistic PDFs of the known "
                            f"festivals are in {cfg.raw_dir} (need {MIN_PDF_COVERAGE:.0%})")
    return problems


# --------------------------------------------------------------------------- export
def bundle_name(now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return f"{BUNDLE_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}{BUNDLE_SUFFIX}"


def _snapshot_db(db_path: Path, target: Path) -> None:
    """Consistent copy of the database (also while a journal exists)."""
    src = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o644, 0
    tar.addfile(info, io.BytesIO(data))


class _HashingReader:
    def __init__(self, fh: BinaryIO) -> None:
        self.fh, self.sha = fh, hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        data = self.fh.read(n)
        self.sha.update(data)
        return data


def _add_file(tar: tarfile.TarFile, name: str, path: Path, files: list[dict[str, Any]]) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = path.stat().st_size, 0o644, 0
    with path.open("rb") as fh:
        reader = _HashingReader(fh)
        tar.addfile(info, reader)  # type: ignore[arg-type]
    files.append({"path": name, "size": info.size, "sha256": reader.sha.hexdigest()})


def export_state(cfg: Config, output: Path, now: _dt.datetime | None = None) -> BundleInfo:
    """Write the state in ``cfg.data_dir`` to ``output`` (a file, or a directory in which
    a time-stamped bundle is created). Refuses a state that is not usable
    (:func:`state_problems`): an upload must never replace a good bundle with a bad one."""
    problems = state_problems(cfg)
    missing = [n for n in ("bouts.parquet", "athletes.parquet", "ratings.parquet")
               if not (cfg.processed_dir / n).is_file()]
    if missing:
        problems.append(f"{cfg.processed_dir}: {', '.join(missing)} missing (run `clean`, `elo`)")
    if problems:
        raise StateError("nothing exported, the state is incomplete: " + "; ".join(problems))
    now = now or _dt.datetime.now(_dt.timezone.utc)
    created = now.isoformat(timespec="seconds")
    if output.is_dir():
        output = output / bundle_name(now)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".part")
    files: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".state-db-") as scratch:
        snapshot = Path(scratch) / DB_NAME
        _snapshot_db(cfg.db_path, snapshot)
        with tmp.open("wb") as raw, \
                gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0) as gz, \
                tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            _add_bytes(tar, HEADER, json.dumps({"format": FORMAT, "created_at": created}).encode())
            for top in DIRS:
                root = cfg.data_dir / top
                for path in sorted(p for p in root.rglob("*") if p.is_file()):
                    if path.is_symlink() or path.name.endswith((".tmp", ".part")):
                        continue
                    _add_file(tar, (PurePosixPath(top) / path.relative_to(root).as_posix())
                              .as_posix(), path, files)
            _add_file(tar, DB_NAME, snapshot, files)
            baseline = cfg.data_dir / BASELINE_NAME
            if baseline.is_file():
                _add_file(tar, BASELINE_NAME, baseline, files)
            counts = {**_db_counts(snapshot),
                      "raw_files": sum(1 for f in files if f["path"].startswith("raw/")),
                      "processed_files": sum(1 for f in files
                                             if f["path"].startswith("processed/"))}
            manifest = {"format": FORMAT, "created_at": created, "counts": counts,
                        "baseline": baseline.is_file(), "files": files}
            _add_bytes(tar, MANIFEST, json.dumps(manifest, separators=(",", ":")).encode())
    tmp.replace(output)
    return BundleInfo(output, created, len(files), sum(f["size"] for f in files), counts)


# --------------------------------------------------------------------------- import
def read_header(path: Path) -> dict[str, Any]:
    """The ``bundle.json`` of a bundle (its first member) - cheap, reads a few KB."""
    try:
        with tarfile.open(path, mode="r|gz") as tar:
            first = tar.next()
            if first is None or first.name != HEADER or not first.isreg():
                raise StateError(f"{path}: not a state bundle (no {HEADER})")
            fh = tar.extractfile(first)
            header = json.loads(fh.read().decode("utf-8")) if fh else None
    except (OSError, tarfile.TarError, ValueError, EOFError) as exc:
        raise StateError(f"{path}: not a readable state bundle ({exc})") from exc
    if not isinstance(header, dict) or header.get("format") != FORMAT \
            or not isinstance(header.get("created_at"), str):
        raise StateError(f"{path}: unsupported bundle format {header!r}")
    return header


def find_bundle(source: Path) -> Path:
    """``source`` itself, or the newest readable bundle in the directory ``source``."""
    if source.is_file():
        return source
    if not source.is_dir():
        raise StateError(f"{source} does not exist - no pipeline state to import")
    found: list[tuple[str, Path]] = []
    for path in sorted(source.glob(f"{BUNDLE_PREFIX}*{BUNDLE_SUFFIX}")):
        try:
            found.append((read_header(path)["created_at"], path))
        except StateError as exc:
            log.warning("state-import: skipping %s", exc)
    if not found:
        raise StateError(f"no state bundle ({BUNDLE_PREFIX}*{BUNDLE_SUFFIX}) in {source}")
    return max(found)[1]


def _safe_name(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if p.is_absolute() or not p.parts or any(part in ("", ".", "..") for part in p.parts) \
            or "\\" in name or p.parts[0] not in (*TOP_LEVEL, HEADER, MANIFEST) \
            or (p.parts[0] not in DIRS and len(p.parts) != 1):
        raise StateError(f"bundle member with an unexpected path: {name!r}")
    return p


def _existing(cfg: Config) -> list[Path]:
    out = []
    for name in TOP_LEVEL:
        path = cfg.data_dir / name
        if path.is_file() or (path.is_dir() and any(path.iterdir())):
            out.append(path)
    return out


def import_state(cfg: Config, source: Path, force: bool = False) -> BundleInfo:
    """Unpack a bundle into ``cfg.data_dir``. Existing state is only replaced with
    ``force``. Nothing is moved into place before the whole bundle has been verified."""
    bundle = find_bundle(source)
    existing = _existing(cfg)
    if existing and not force:
        raise StateError(f"{cfg.data_dir} already holds state ({', '.join(p.name for p in existing)}"
                         f") - use --force to replace it, or another --data-dir")
    header = read_header(bundle)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=cfg.data_dir, prefix=".state-import-"))
    try:
        seen: dict[str, tuple[int, str]] = {}
        manifest: dict[str, Any] | None = None
        try:
            with tarfile.open(bundle, mode="r|gz") as tar:
                for member in tar:
                    name = _safe_name(member.name)
                    if not member.isreg():
                        raise StateError(f"bundle member {member.name!r} is not a regular file")
                    fh = tar.extractfile(member)
                    assert fh is not None
                    if member.name in (HEADER, MANIFEST):
                        data = fh.read()
                        if member.name == MANIFEST:
                            manifest = json.loads(data.decode("utf-8"))
                        continue
                    if member.name in seen:
                        raise StateError(f"bundle member {member.name!r} occurs twice")
                    target = staging.joinpath(*name.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    sha, size = hashlib.sha256(), 0
                    with target.open("wb") as out:
                        while chunk := fh.read(_CHUNK):
                            sha.update(chunk)
                            size += len(chunk)
                            out.write(chunk)
                    seen[member.name] = (size, sha.hexdigest())
        except (OSError, tarfile.TarError, ValueError, EOFError) as exc:
            raise StateError(f"{bundle}: damaged or truncated bundle ({exc})") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
            raise StateError(f"{bundle}: truncated bundle (no {MANIFEST})")
        expected = {f["path"]: (f["size"], f["sha256"]) for f in manifest["files"]}
        if expected != seen:
            bad = sorted(set(expected) ^ set(seen)) or \
                sorted(k for k in expected if expected[k] != seen[k])
            raise StateError(f"{bundle}: content does not match its manifest "
                             f"({len(bad)} files, e.g. {bad[0]})")
        if DB_NAME not in seen or not any(k.startswith("raw/") for k in seen):
            raise StateError(f"{bundle}: bundle without database or cache")
        for name in TOP_LEVEL:
            part, target = staging / name, cfg.data_dir / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            if part.exists():
                part.replace(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    counts = {k: int(v) for k, v in (manifest.get("counts") or {}).items()}
    return BundleInfo(bundle, header["created_at"], len(seen),
                      sum(s for s, _ in seen.values()), counts)
