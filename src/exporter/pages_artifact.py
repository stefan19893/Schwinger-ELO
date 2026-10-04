"""``pack-site``: the built site as the tar file GitHub Pages deploys - written quietly.

Why this exists: ``actions/upload-pages-artifact`` packs the site with a verbose ``tar``,
so the (world-readable) log of a workflow run lists every file of ``dist/`` - one
``history_<id>.json`` and ``bouts_<id>.json`` per athlete, and athlete ids are name slugs.
This module writes the same archive without printing a single file name; the workflow
then uploads that one file with ``actions/upload-artifact`` under the name
``github-pages``, which is all the composite action does after its ``tar`` step.

What is reproduced from ``upload-pages-artifact@v4`` (``action.yml``; GNU tar on Linux:
``tar --dereference --hard-dereference --directory dist -cvf artifact.tar --exclude=.git
--exclude=.github --exclude=".[^/]*" .``):

* an **uncompressed GNU-format tar** (the artifact upload compresses it);
* member names relative to the site root with a leading ``./``, the first member being
  the root directory ``./``; directories are members of their own;
* **hidden entries are left out**: every file or directory whose name starts with a dot,
  at any depth, with everything below it (that is what the three ``--exclude`` patterns
  amount to);
* a file with several hard links is packed as an ordinary file under each of its names
  (``--hard-dereference``); modification times are those of the files.

What differs, on purpose:

* **nothing is listed** (no ``-v``); the caller logs counts and sizes only;
* **a symbolic link is refused** instead of followed (``--dereference``): the builder
  never writes one, and a followed link could pull a file from outside the site - the
  pipeline state, say - into a public deployment;
* members come in **sorted order**, are owned by ``0:0`` and have the modes ``0644`` /
  ``0755``: the same tree gives the same bytes on every machine, and every member is
  readable, whatever the umask of the build was.

GitHub's requirements for a Pages artifact (README of ``actions/upload-pages-artifact``,
"Artifact validation"; docs, "Using custom workflows with GitHub Pages") are checked here,
before and after writing: the tar holds **only regular files and directories** - no
symbolic or hard links - and stays under :data:`MAX_TAR_BYTES`.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import stat
import tarfile
from pathlib import Path

log = logging.getLogger("schwingen")

#: GitHub: "be under 10GB in size (we recommend under 1 GB!)" - 1 GB is the officially
#: supported size of a Pages site; a larger tar is "not guaranteed to succeed". The site is
#: some 50 MB, so an archive near this limit is a defect, not a site.
MAX_TAR_BYTES = 1_000_000_000
FILE_MODE, DIR_MODE = 0o644, 0o755
_CHUNK = 1 << 20


class PagesArtifactError(Exception):
    """The site cannot be packed as a valid Pages artifact. The message never holds a file
    name of the site (names go to DEBUG)."""


@dataclasses.dataclass(frozen=True)
class PackInfo:
    path: Path
    n_files: int
    n_dirs: int            # without the root
    n_hidden: int          # hidden entries left out (each with everything below it)
    n_bytes: int           # content bytes of the files
    tar_bytes: int


@dataclasses.dataclass(frozen=True)
class _Member:
    name: str              # "./a/b.json"; "." for the root
    path: Path
    is_dir: bool


def _scan(dist: Path) -> tuple[list[_Member], int, dict[str, list[str]]]:
    """Members in a fixed order (parents first, names sorted by code point), the number of
    hidden entries skipped, and what must not be packed (kind -> relative names)."""
    members = [_Member(".", dist, True)]
    hidden = 0
    problems: dict[str, list[str]] = {}

    def walk(directory: Path, prefix: str) -> None:
        nonlocal hidden
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            name = f"{prefix}/{entry.name}"
            if entry.name.startswith("."):
                hidden += 1
            elif entry.is_symlink():
                problems.setdefault("symbolic link", []).append(name)
            elif entry.is_dir(follow_symlinks=False):
                members.append(_Member(name, Path(entry.path), True))
                walk(Path(entry.path), name)
            elif entry.is_file(follow_symlinks=False):
                members.append(_Member(name, Path(entry.path), False))
            else:
                problems.setdefault("special file (device, socket, pipe)", []).append(name)

    walk(dist, ".")
    return members, hidden, problems


def _tarinfo(name: str, is_dir: bool, size: int, mtime: float) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    info.mode = DIR_MODE if is_dir else FILE_MODE
    info.size = 0 if is_dir else size
    info.mtime = int(mtime)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _write(members: list[_Member], target: Path) -> int:
    """Write the tar; returns the content bytes. A file that became a link or something
    else since the scan is refused (opened without following links)."""
    total = 0
    with tarfile.open(target, "w", format=tarfile.GNU_FORMAT) as tar:
        for m in members:
            if m.is_dir:
                st = os.lstat(m.path)
                if not stat.S_ISDIR(st.st_mode):
                    log.debug("pack-site: changed while packing: %s", m.name)
                    raise PagesArtifactError("the site changed while it was packed")
                tar.addfile(_tarinfo(m.name, True, 0, st.st_mtime))
                continue
            try:
                fd = os.open(m.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError as exc:
                log.debug("pack-site: cannot read %s: %s", m.name, exc)
                raise PagesArtifactError(
                    f"a file of the site cannot be read ({exc.strerror or 'error'})") from None
            with os.fdopen(fd, "rb") as fh:
                st = os.fstat(fh.fileno())
                if not stat.S_ISREG(st.st_mode):
                    log.debug("pack-site: changed while packing: %s", m.name)
                    raise PagesArtifactError("the site changed while it was packed")
                try:
                    tar.addfile(_tarinfo(m.name, False, st.st_size, st.st_mtime), fh)
                except OSError:                      # shorter than its size a moment ago
                    log.debug("pack-site: changed while packing: %s", m.name)
                    raise PagesArtifactError("the site changed while it was packed") from None
            total += st.st_size
    return total


def archive_problems(path: Path, n_files: int, n_dirs: int,
                     max_bytes: int = MAX_TAR_BYTES) -> list[str]:
    """What makes the tar at ``path`` unfit for GitHub Pages (empty = fit). Reads the
    finished file, so it holds whatever wrote it. No member name in the messages."""
    problems: list[str] = []
    size = path.stat().st_size
    if size >= max_bytes:
        problems.append(f"the archive has {size / 1e6:.0f} MB, the limit for a Pages site is "
                        f"{max_bytes / 1e6:.0f} MB")
    with path.open("rb") as fh:
        head = fh.read(512)
    if head[257:265] != b"ustar  \0":
        problems.append("the archive is not a GNU-format tar")
    files = dirs = 0
    with tarfile.open(path, "r:") as tar:           # "r:" = uncompressed only
        for i, m in enumerate(tar):
            parts = m.name.split("/")
            if i == 0:
                if not (m.isdir() and m.name == "."):
                    problems.append("the first member is not the root directory `./`")
                    break
                continue
            if m.issym() or m.islnk():
                problems.append("the archive contains a link")
            elif not (m.isreg() or m.isdir()):
                problems.append("the archive contains a member that is neither a file nor "
                                "a directory")
            if parts[0] != "." or len(parts) < 2 or any(
                    p == "" or p.startswith(".") for p in parts[1:]):
                problems.append("a member is hidden or outside the site root")
            if m.mode != (DIR_MODE if m.isdir() else FILE_MODE):
                problems.append("a member is not world-readable (0644 / 0755)")
            files += m.isreg()
            dirs += m.isdir()
    if (files, dirs) != (n_files, n_dirs) and not problems:
        problems.append(f"the archive holds {files} files / {dirs} directories, the site has "
                        f"{n_files} / {n_dirs}")
    return sorted(set(problems))


def pack_site(dist: Path, output: Path, max_bytes: int = MAX_TAR_BYTES) -> PackInfo:
    """Pack the site in ``dist`` into the tar file ``output`` (written beside it first and
    moved into place only when it passed :func:`archive_problems`). Raises
    :class:`PagesArtifactError`; nothing is left behind then."""
    if dist.is_symlink() or not dist.is_dir():
        raise PagesArtifactError(f"{dist} is not a directory - run `build` first")
    if not (dist / "index.html").is_file() or (dist / "index.html").is_symlink():
        raise PagesArtifactError(f"{dist} has no index.html - run `build` first")
    dist, output = dist.resolve(), output.resolve()
    if output == dist or dist in output.parents:
        raise PagesArtifactError("the archive must be written outside the site directory")
    if output.is_dir():
        raise PagesArtifactError(f"{output} is a directory - give the tar file to write")

    members, hidden, problems = _scan(dist)
    for kind, names in problems.items():
        for name in names:
            log.debug("pack-site: %s: %s", kind, name)
    if problems:
        what = ", ".join(f"{len(names)} {kind}{'s' if len(names) > 1 else ''}"
                         for kind, names in sorted(problems.items()))
        raise PagesArtifactError(
            f"{dist} contains {what} - a Pages artifact holds only regular files and "
            "directories (the names are logged with -v)")
    n_dirs = sum(m.is_dir for m in members) - 1
    n_files = len(members) - n_dirs - 1

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".part")
    try:
        n_bytes = _write(members, tmp)
        found = archive_problems(tmp, n_files, n_dirs, max_bytes)
        if found:
            raise PagesArtifactError("; ".join(found))
        tar_bytes = tmp.stat().st_size
        os.replace(tmp, output)
    finally:
        tmp.unlink(missing_ok=True)
    return PackInfo(output, n_files, n_dirs, hidden, n_bytes, tar_bytes)
