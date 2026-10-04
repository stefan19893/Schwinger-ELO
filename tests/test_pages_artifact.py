"""``pack-site``: the Pages archive (src/exporter/pages_artifact.py).

The archive replaces the one ``actions/upload-pages-artifact@v4`` made with a verbose
``tar`` (which listed every ``history_<id>.json`` in the public log). Pinned down here:
its contents are ``dist/`` minus hidden entries, the bytes are a function of the tree, the
log holds counts only, and what GitHub Pages does not accept (links, special files, a
missing or oversized site) is refused. Where GNU tar is installed, the archive is compared
member by member and header by header with the output of the action's own command.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pandas as pd
import pytest

from src import cli
from src.exporter import pages_artifact as pa

# the `tar` call of actions/upload-pages-artifact@v4 (action.yml, Linux), without `-v`
# so that the comparison itself prints nothing
ACTION_TAR = ["tar", "--dereference", "--hard-dereference", "--directory", "{dist}",
              "-cf", "{out}", "--exclude=.git", "--exclude=.github", "--exclude=.[^/]*", "."]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("SCHWINGEN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(tmp_path / "dist"))
    monkeypatch.setenv("SCHWINGEN_DATA_DIR", str(tmp_path / "data"))


def _site(root: Path) -> Path:
    """A small site with everything the exclusions and the naming have to get right."""
    files = {
        "index.html": "<html>",
        "athlete.html": "<html>a",
        "data/meta.json": "{}",
        "data/history/history_muster-hans.json": "[1]",
        "data/history/history_zaugg-ueli.json": "[2, 3]",
        "data/bouts/bouts_muster-hans.json": "[]",
        "js/app.v2.js": "x",                              # a dot inside a name is not hidden
        "data/" + "d" * 60 + "/" + "f" * 70 + ".json": "long",   # > 100 chars: GNU long name
        "data/zürich.json": "non-ascii",
        # hidden, at every depth - none of these may be packed
        ".nojekyll": "",
        ".git/config": "secret",
        ".github/workflows/x.yml": "y",
        ".well-known/security.txt": "z",
        "data/.cache/tmp.json": "{}",
        "data/history/.history_draft.json": "{}",
        "js/..odd": "",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "empty").mkdir()
    os.link(root / "index.html", root / "copy.html")       # two names, one inode
    for i, path in enumerate(sorted(root.rglob("*"))):     # distinct, whole-second mtimes
        os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i), follow_symlinks=False)
    return root


def _visible(root: Path) -> dict[str, bytes | None]:
    """`./relative/name` -> content (None for a directory) of everything not hidden."""
    out: dict[str, bytes | None] = {".": None}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        out["./" + rel.as_posix()] = None if path.is_dir() else path.read_bytes()
    return out


def _members(archive: Path) -> dict[str, tuple]:
    out = {}
    with tarfile.open(archive, "r:") as tar:
        for m in tar:
            data = tar.extractfile(m).read() if m.isreg() else None
            out[m.name] = (m.type, m.size, m.mtime, m.linkname, data)
    return out


def _headers(archive: Path) -> list[bytes]:
    """Every 512-byte header block as stored (GNU long-name records included), with the
    fields that differ on purpose blanked: mode, uid, gid, checksum, user and group name."""
    out = []
    with archive.open("rb") as fh:
        while True:
            block = fh.read(512)
            if len(block) < 512 or block == b"\0" * 512:
                break
            size = int(block[124:135].rstrip(b"\0 ") or b"0", 8)
            data = fh.read((size + 511) // 512 * 512)
            masked = bytearray(block)
            for start, end in ((100, 124), (148, 156), (265, 329)):
                masked[start:end] = b"?" * (end - start)
            out.append(bytes(masked) + (data if block[156:157] == b"L" else b""))
    return out


# ------------------------------------------------------------------- contents
def test_archive_is_the_site_minus_hidden_entries(tmp_path: Path) -> None:
    dist = _site(tmp_path / "dist")
    info = pa.pack_site(dist, tmp_path / "out" / "artifact.tar")
    want = _visible(dist)
    got = _members(info.path)
    assert list(got)[0] == "." and sorted(got) == sorted(want)
    assert {k: v[4] for k, v in got.items()} == want
    assert not any("/." in name for name in got)            # nothing hidden, at any depth
    assert "./js/app.v2.js" in got and "./empty" in got and "./copy.html" in got
    n_dirs = sum(v is None for v in want.values()) - 1
    assert (info.n_files, info.n_dirs) == (len(want) - n_dirs - 1, n_dirs)
    assert info.n_hidden == 7                               # entries, not the files below them
    assert info.n_bytes == sum(len(v) for v in want.values() if v is not None)
    assert info.tar_bytes == info.path.stat().st_size and info.tar_bytes % 512 == 0
    with tarfile.open(info.path, "r:") as tar:
        for m in tar:
            # only files and directories; the hard-linked file is a file under both names
            assert m.isreg() or m.isdir(), m.type
            assert m.mode == (0o755 if m.isdir() else 0o644)
            assert (m.uid, m.gid, m.uname, m.gname) == (0, 0, "", "")
            assert m.mtime == int(os.lstat(dist / m.name).st_mtime)
    with pytest.raises(tarfile.ReadError):                  # a plain tar, not compressed
        tarfile.open(info.path, "r:gz")
    assert not list(info.path.parent.glob("*.part"))


def test_same_tree_gives_the_same_bytes(tmp_path: Path) -> None:
    dist = _site(tmp_path / "dist")
    first = pa.pack_site(dist, tmp_path / "a.tar").path.read_bytes()
    # creation order and the readdir order must not matter: rebuild the tree in reverse
    twin = tmp_path / "twin" / "dist"
    for path in sorted(dist.rglob("*"), reverse=True):
        target = twin / path.relative_to(dist)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    for path in sorted(dist.rglob("*"), reverse=True):      # children before their parents
        st = os.lstat(path)
        os.utime(twin / path.relative_to(dist), (st.st_mtime, st.st_mtime))
    os.utime(twin, (os.lstat(dist).st_mtime,) * 2)
    os.chmod(twin / "index.html", 0o600)                    # the build's umask does not show
    assert pa.pack_site(twin, tmp_path / "b.tar").path.read_bytes() == first
    assert pa.pack_site(dist, tmp_path / "a.tar").path.read_bytes() == first    # overwrite
    (dist / "data" / "meta.json").write_text('{"x": 1}', encoding="utf-8")
    assert pa.pack_site(dist, tmp_path / "c.tar").path.read_bytes() != first


@pytest.mark.skipif(shutil.which("tar") is None or b"GNU tar" not in subprocess.run(
    ["tar", "--version"], capture_output=True).stdout, reason="needs GNU tar")
def test_same_archive_as_the_tar_command_of_upload_pages_artifact(tmp_path: Path) -> None:
    """The reference: what `actions/upload-pages-artifact@v4` packed. Same members (names
    as tar lists them, types, sizes, mtimes, contents) and the same header blocks apart
    from owner, mode and checksum."""
    dist = _site(tmp_path / "dist")
    mine = pa.pack_site(dist, tmp_path / "mine.tar").path
    ref = tmp_path / "ref.tar"
    subprocess.run([a.format(dist=dist, out=ref) for a in ACTION_TAR], check=True)

    def listing(p: Path) -> list[str]:
        return sorted(subprocess.run(["tar", "-tf", str(p)], capture_output=True, check=True,
                                     text=True).stdout.splitlines())

    assert listing(mine) == listing(ref) and "./" in listing(mine)
    assert _members(mine) == _members(ref)
    assert sorted(_headers(mine)) == sorted(_headers(ref))
    assert any(h[156:157] == b"L" for h in _headers(mine))  # the long name was exercised
    assert mine.read_bytes()[:512][257:265] == ref.read_bytes()[:512][257:265] == b"ustar  \0"
    assert mine.read_bytes()[:2] == ref.read_bytes()[:2] == b"./"
    assert mine.stat().st_size == ref.stat().st_size


# -------------------------------------------------------------------- refusals
def _refused(dist: Path, out: Path, **kw: int) -> str:
    with pytest.raises(pa.PagesArtifactError) as err:
        pa.pack_site(dist, out, **kw)
    assert not out.exists() and not list(out.parent.glob("*.part"))
    return str(err.value)


def test_links_and_special_files_are_refused(tmp_path: Path,
                                             caplog: pytest.LogCaptureFixture) -> None:
    dist, out = _site(tmp_path / "dist"), tmp_path / "artifact.tar"
    private = tmp_path / "data" / "schwingen.db"
    private.parent.mkdir()
    private.write_text("birthdays")
    # a followed link would publish what it points to (`tar --dereference` does that)
    (dist / "data" / "history" / "history_link-to-state.json").symlink_to(private)
    (dist / "state").symlink_to(private.parent, target_is_directory=True)
    (dist / "dangling.html").symlink_to(tmp_path / "nowhere")
    with caplog.at_level(logging.DEBUG):
        msg = _refused(dist, out)
    assert "3 symbolic links" in msg and "only regular files and directories" in msg
    assert "history_link" not in msg and "state" not in msg.replace("the site", "")
    # the names are there for the owner at -v, and only there
    assert "history_link-to-state" in "".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    assert "history_link" not in "".join(
        r.getMessage() for r in caplog.records if r.levelno > logging.DEBUG)
    for link in ("data/history/history_link-to-state.json", "state", "dangling.html"):
        (dist / link).unlink()
    pa.pack_site(dist, out)
    out.unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(dist / "data" / "pipe")
        assert "1 special file" in _refused(dist, out)
        (dist / "data" / "pipe").unlink()
    # a hidden link is not packed, so it is not a reason to fail
    (dist / ".link").symlink_to(private)
    assert "./.link" not in _members(pa.pack_site(dist, out).path)


def test_missing_or_unbuilt_site_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "artifact.tar"
    assert "is not a directory" in _refused(tmp_path / "dist", out)
    (tmp_path / "dist").mkdir()
    assert "no index.html" in _refused(tmp_path / "dist", out)
    (tmp_path / "file").write_text("x")
    assert "is not a directory" in _refused(tmp_path / "file", out)
    real = _site(tmp_path / "real")
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    assert "is not a directory" in _refused(tmp_path / "alias", out)   # the site dir itself
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / "index.html").symlink_to(real / "index.html")
    assert "no index.html" in _refused(tmp_path / "linked", out)


def test_output_inside_the_site_and_oversize_are_refused(tmp_path: Path) -> None:
    dist = _site(tmp_path / "dist")
    for inside in (dist / "artifact.tar", dist / "data" / "x" / "artifact.tar", dist):
        with pytest.raises(pa.PagesArtifactError, match="outside the site directory"):
            pa.pack_site(dist, inside)
    assert not (dist / "artifact.tar").exists() and not (dist / "data" / "x").exists()
    (tmp_path / "adir").mkdir()
    with pytest.raises(pa.PagesArtifactError, match="is a directory"):
        pa.pack_site(dist, tmp_path / "adir")
    assert "limit for a Pages site" in _refused(dist, tmp_path / "artifact.tar", max_bytes=4096)
    assert pa.MAX_TAR_BYTES == 1_000_000_000               # GitHub: 1 GB supported, 10 GB hard
    # a failed run leaves an earlier archive alone
    good = pa.pack_site(dist, tmp_path / "keep.tar").path.read_bytes()
    (dist / "bad").symlink_to(dist / "index.html")
    with pytest.raises(pa.PagesArtifactError):
        pa.pack_site(dist, tmp_path / "keep.tar")
    assert (tmp_path / "keep.tar").read_bytes() == good


def _tar_with(path: Path, extra: tarfile.TarInfo | None, first: str = ".") -> Path:
    with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as tar:
        root = tarfile.TarInfo(first)
        root.type, root.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(root)
        page = tarfile.TarInfo("./index.html")
        page.mode = 0o644
        tar.addfile(page)
        if extra is not None:
            tar.addfile(extra)
    return path


@pytest.mark.parametrize("name, kind, mode, expect", [
    ("./a.html", tarfile.SYMTYPE, 0o644, "contains a link"),
    ("./a.html", tarfile.LNKTYPE, 0o644, "contains a link"),
    ("./a", tarfile.FIFOTYPE, 0o644, "neither a file nor a directory"),
    ("./.hidden", tarfile.REGTYPE, 0o644, "hidden or outside"),
    ("./data/.x/a.json", tarfile.REGTYPE, 0o644, "hidden or outside"),
    ("./../data/schwingen.db", tarfile.REGTYPE, 0o644, "hidden or outside"),
    ("/etc/passwd", tarfile.REGTYPE, 0o644, "hidden or outside"),
    ("a.html", tarfile.REGTYPE, 0o644, "hidden or outside"),
    ("./a.html", tarfile.REGTYPE, 0o600, "not world-readable"),
    ("./a", tarfile.DIRTYPE, 0o700, "not world-readable"),
])
def test_finished_archive_is_checked(tmp_path: Path, name: str, kind: bytes, mode: int,
                                     expect: str) -> None:
    """`archive_problems` reads the written file: what GitHub documents as invalid is
    found whatever produced it."""
    extra = tarfile.TarInfo(name)
    extra.type, extra.mode, extra.linkname = kind, mode, "index.html"
    is_file, is_dir = kind == tarfile.REGTYPE, kind == tarfile.DIRTYPE
    bad = _tar_with(tmp_path / "bad.tar", extra)
    found = pa.archive_problems(bad, 1 + is_file, int(is_dir))
    assert any(expect in line for line in found), found
    assert name.rsplit("/", 1)[-1] not in " ".join(found) or name == "./a"   # no member name
    good = _tar_with(tmp_path / "good.tar", None)
    assert pa.archive_problems(good, 1, 0) == []
    assert any("holds 1 files" in line for line in pa.archive_problems(good, 2, 0))
    assert any("root directory" in line for line in pa.archive_problems(
        _tar_with(tmp_path / "noroot.tar", None, first="./data"), 1, 1))
    with tarfile.open(tmp_path / "pax.tar", "w", format=tarfile.PAX_FORMAT) as tar:
        for info in tarfile.open(good, "r:"):
            tar.addfile(info)
    assert any("GNU-format" in line for line in pa.archive_problems(tmp_path / "pax.tar", 1, 0))


# ----------------------------------------------------------------------- CLI
def test_cli_packs_the_site_directory_and_only_that(tmp_path: Path) -> None:
    dist = _site(tmp_path / "dist")                          # = SCHWINGEN_DIST_DIR
    out = tmp_path / "runner-temp" / "artifact.tar"
    assert cli.main(["pack-site", str(out)]) == 0
    assert sorted(_members(out)) == sorted(_visible(dist))
    # there is no way to name another source
    for extra in (["--source", "data"], ["data"], ["--dist-dir", "data"]):
        with pytest.raises(SystemExit):
            cli.main(["pack-site", str(out), *extra])
    with pytest.raises(SystemExit):
        cli.main(["pack-site"])
    # --data-dir moves the data, not the site
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "index.html").write_text("state")
    before = out.read_bytes()
    assert cli.main(["pack-site", str(out), "--data-dir", str(tmp_path / "other")]) == 0
    assert out.read_bytes() == before


def test_cli_fails_without_a_site(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    out = tmp_path / "artifact.tar"
    with caplog.at_level(logging.INFO):
        assert cli.main(["pack-site", str(out)]) == 1
    assert "pack-site:" in caplog.text and "nothing written" in caplog.text
    assert not out.exists()
    _site(tmp_path / "dist")
    (tmp_path / "dist" / "data" / "history" / "history_x.json").symlink_to(out)
    assert cli.main(["pack-site", str(out)]) == 1 and not out.exists()


def test_log_of_a_real_site_names_no_athlete(tmp_path: Path, caplog: pytest.LogCaptureFixture,
                                             capfd: pytest.CaptureFixture[str]) -> None:
    """The defect this command fixes: 12,612 file names in the public log of the first
    deployment. A sample site holds one history file per published athlete; neither their
    ids nor any file name may appear at the default log level - also when packing fails."""
    data = str(tmp_path / "data")
    assert cli.main(["all", "--sample", "--data-dir", data]) == 0
    dist = tmp_path / "dist"
    names = sorted(p.name for p in dist.rglob("*") if p.is_file())
    ids = [n[len("history_"):-len(".json")] for n in names if n.startswith("history_")]
    assert len(ids) > 100 and len(names) > 1000
    athletes = pd.read_parquet(tmp_path / "data" / "processed" / "athletes.parquet")
    assert set(ids) <= set(athletes["athlete_id"])            # file names are athlete ids
    out = tmp_path / "artifact.tar"
    caplog.clear()
    capfd.readouterr()
    with caplog.at_level(logging.INFO):
        assert cli.main(["pack-site", str(out), "--sample", "--data-dir", data]) == 0
        (dist / "data" / "history" / f"history_{ids[0]}.html").symlink_to(dist / "index.html")
        assert cli.main(["pack-site", str(tmp_path / "second.tar")]) == 1
    stdout, stderr = capfd.readouterr()
    records = [r for r in caplog.records if r.levelno >= logging.INFO]
    text = "\n".join(r.getMessage() for r in records) + stdout + stderr
    assert len(records) == 2 and "pack-site:" in text
    n_visible = len([n for n in names if not n.startswith(".")])
    assert f"({n_visible} files in " in records[0].getMessage()      # counts, and they are right
    assert "1 symbolic link " in records[1].getMessage()
    leaked = [n for n in names if n in text] + [i for i in ids if i in text]
    assert leaked == []
    assert "history_" not in text and "bouts_" not in text and ".json" not in text
    # the check can fail: the archive itself holds every one of these names
    listing = "\n".join(_members(out))
    assert all(f"history_{i}.json" in listing for i in ids)
