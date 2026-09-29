"""Copies web/ and writes JSON slices into dist/.

Phase 0: only produces a placeholder site. JSON exports arrive in Phase 5.
"""

from __future__ import annotations

import datetime as _dt
import shutil
from pathlib import Path

from src.config import REPO_ROOT, Config

_IGNORED = shutil.ignore_patterns(".gitkeep", "__pycache__", "*.pyc")

PLACEHOLDER_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Schwinger-ELO</title>
</head>
<body>
  <h1>Schwinger-ELO</h1>
  <p>Placeholder site &mdash; ratings are not computed yet.</p>
  <p>Mode: {mode} &middot; built {built}</p>
</body>
</html>
"""


def build_site(cfg: Config) -> Path:
    """(Re)build ``cfg.dist_dir`` and return its path.

    Copies ``web/`` (if present) and writes a placeholder ``index.html`` when
    ``web/`` does not provide one. Only relative URLs are used (spec §6.3).
    """
    dist = cfg.dist_dir.resolve()
    protected = {cfg.web_dir.resolve(), cfg.data_dir.resolve(), REPO_ROOT, Path.home()}
    if dist in protected or dist in REPO_ROOT.parents:
        raise ValueError(f"refusing to wipe {dist}: not a safe dist directory")
    if dist.exists():
        shutil.rmtree(dist)
    if cfg.web_dir.is_dir():
        shutil.copytree(cfg.web_dir, dist, ignore=_IGNORED)
    else:
        dist.mkdir(parents=True)
    index = dist / "index.html"
    if not index.exists():
        built = _dt.datetime.now().isoformat(timespec="seconds")
        mode = "sample" if cfg.sample else "real data"
        index.write_text(PLACEHOLDER_HTML.format(mode=mode, built=built), encoding="utf-8")
    # GitHub Pages: serve files as-is (no Jekyll processing).
    (dist / ".nojekyll").touch()
    return dist
