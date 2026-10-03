#!/usr/bin/env python3
"""Write ``requirements-lock.txt``: every installed distribution pinned with the sha256
of the wheel it was installed from - without network access.

    .venv/bin/python scripts/make_lock.py            # after `pip install -r requirements.txt`

Where the hashes come from: pip keeps the wheels it downloaded in its HTTP cache
(``pip cache dir``/http-v2/**.body). For every installed distribution the script looks for
a cached wheel of the same name and version and **verifies it against the installation**:
every file in the wheel must have the same sha256 as the installed file. Only then the
wheel's own sha256 is written as ``--hash``. A distribution without a verified wheel
aborts the run (nothing is written) - no hash is ever taken on trust.

The lock is for the platform it was made on (CPython minor version, Linux x86_64): pure
Python wheels are the same everywhere, binary wheels (numpy, pandas, scipy, pyarrow,
pypdfium2, selectolax) are not. ``pip install --require-hashes`` fails loudly when the
index offers the runner a different file. With network access the usual way is
``pip-compile --generate-hashes`` (pip-tools) on the deployment's Python version.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as md
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "requirements-lock.txt"
SKIP = {"pip", "setuptools", "wheel"}          # the installer itself is not locked


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def wheel_identity(zf: zipfile.ZipFile) -> tuple[str, str, list[str]] | None:
    meta = [n for n in zf.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
    if len(meta) != 1:
        return None
    head = zf.read(meta[0]).decode("utf-8", "replace")
    name = re.search(r"^Name: (.+)$", head, re.M)
    version = re.search(r"^Version: (.+)$", head, re.M)
    wheel = zf.read(meta[0].replace("METADATA", "WHEEL")).decode("utf-8", "replace")
    if not name or not version:
        return None
    return norm(name.group(1).strip()), version.group(1).strip(), re.findall(r"^Tag: (.+)$",
                                                                             wheel, re.M)


def matches_installation(zf: zipfile.ZipFile, dist: md.Distribution) -> bool:
    """Every file of the wheel is installed with identical content."""
    base = Path(str(dist.locate_file("")))
    checked = 0
    for info in zf.infolist():
        if info.is_dir() or info.filename.endswith(".dist-info/RECORD"):
            continue
        name = info.filename
        m = re.match(r"[^/]+\.data/(purelib|platlib)/(.+)", name)
        if m:
            name = m.group(2)
        elif ".data/" in name.split("/")[0] + "/":
            continue                    # scripts / headers / data: installed elsewhere
        target = base / name
        if not target.is_file():
            return False
        if hashlib.sha256(zf.read(info)).hexdigest() != sha256(target):
            return False
        checked += 1
    return checked > 0


def main() -> int:
    cache = Path(subprocess.run([sys.executable, "-m", "pip", "cache", "dir"], check=True,
                                capture_output=True, text=True).stdout.strip()) / "http-v2"
    installed = {norm(d.metadata["Name"]): d for d in md.distributions()
                 if norm(d.metadata["Name"]) not in SKIP}
    found: dict[str, tuple[str, list[str]]] = {}
    for body in sorted(cache.rglob("*.body")):
        with body.open("rb") as fh:
            if fh.read(2) != b"PK":
                continue
        try:
            with zipfile.ZipFile(body) as zf:
                ident = wheel_identity(zf)
                if ident is None or ident[0] not in installed or ident[0] in found:
                    continue
                dist = installed[ident[0]]
                if dist.version != ident[1] or not matches_installation(zf, dist):
                    continue
        except zipfile.BadZipFile:
            continue
        found[ident[0]] = (sha256(body), ident[2])
    missing = sorted(set(installed) - set(found))
    if missing:
        print(f"no verified wheel in {cache} for: {', '.join(missing)} - nothing written",
              file=sys.stderr)
        return 1
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    lines = [
        "# Exact versions and wheel hashes for the workflows that publish (deploy_pages.yml,",
        "# scrape_and_update.yml) - installed there with `pip install --require-hashes`.",
        f"# Made by scripts/make_lock.py on CPython {py}, {sys.platform} x86_64, from the",
        "# wheels in pip's cache, each verified file by file against the installation.",
        "# Binary wheels are platform-specific: a runner that is offered another file fails",
        "# the install (it never installs something unpinned). requirements.txt stays the",
        "# source of the version ranges and is what scripts/deploy_local.sh installs.",
        "",
    ]
    for name in sorted(found):
        digest, tags = found[name]
        lines.append(f"{name}=={installed[name].version} \\")
        lines.append(f"    --hash=sha256:{digest}  # {', '.join(tags)}")
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(found)} distributions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
