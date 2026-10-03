"""Workflow logs of a public repository are world-readable: at the default log level no
stage may print the name or the id (a name slug) of an athlete - he may be one the site
withholds. Names are for `-v` and for a person at a terminal (Phase 6 review)."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pandas as pd
import pytest

from src import cli
from src.config import REPO_ROOT


def _leaks(text: str, athletes: pd.DataFrame) -> list[str]:
    found = []
    for aid, name in zip(athletes["athlete_id"], athletes["full_name"]):
        if aid in text or (isinstance(name, str) and len(name) > 5 and name in text):
            found.append(aid)
    return found


def test_pipeline_logs_no_athlete_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        caplog: pytest.LogCaptureFixture,
                                        capfd: pytest.CaptureFixture[str]) -> None:
    for key in list(os.environ):
        if key.startswith("SCHWINGEN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("SCHWINGEN_DIST_DIR", str(tmp_path / "dist"))
    data = str(tmp_path / "data")
    with caplog.at_level(logging.INFO):
        assert cli.main(["all", "--sample", "--data-dir", data]) == 0
        assert cli.main(["parse", "--sample", "--force", "--data-dir", data]) == 0  # re-parse
        assert cli.main(["all", "--sample", "--skip-crawl", "--data-dir", data]) == 0
        assert cli.main(["check-site", "--sample", "--data-dir", data]) == 0
    out, err = capfd.readouterr()
    text = "\n".join(r.getMessage() for r in caplog.records
                     if r.levelno >= logging.INFO) + out + err
    assert "elo:" in text and "parse:" in text and "build:" in text and len(text) > 2000
    athletes = pd.read_parquet(tmp_path / "data" / "processed" / "athletes.parquet")
    assert len(athletes) > 300
    assert _leaks(text, athletes) == []
    # the check can fail: the same run at DEBUG prints the top of the ranking
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert cli.main(["elo", "--sample", "--data-dir", data]) == 0
    assert _leaks("\n".join(r.getMessage() for r in caplog.records), athletes)


def test_workflows_never_run_verbose() -> None:
    for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"src\.cli[^\n]*( -v\b| --verbose\b)", text), path.name
        assert "SCHWINGEN_VERBOSE" not in text and "ACTIONS_STEP_DEBUG" not in text
