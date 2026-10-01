"""Batch parse into SQLite (offline: fixture PDFs served via MockTransport)."""

from __future__ import annotations

import datetime as dt
import json
import random
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from tenacity import wait_none

from src.db import Festival, connect, upsert_festivals
from src.scraper import parse_runner as pr
from src.scraper.client import HttpClient

from tests.fixture_paths import STATISTIC as FIX
from tests.fixture_paths import sheet_file

META = json.loads((FIX / "festivals.json").read_text(encoding="utf-8"))
TODAY = dt.date(2026, 9, 30)


def fest(fid: int) -> Festival:
    m = {k: v for k, v in META[str(fid)].items() if k != "fixture_note"}
    return Festival(**m)


def client(tmp_path: Path, pdfs: dict[str, bytes], offline: bool) -> HttpClient:
    def handler(req: httpx.Request) -> httpx.Response:
        body = pdfs.get(str(req.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)
    return HttpClient(tmp_path / "raw", "test", transport=httpx.MockTransport(handler),
                      offline=offline, retry_wait=wait_none(), sleep=lambda s: None,
                      rng=random.Random(0))


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, list[Festival]]:
    """DB with 2 real festivals (PDF cached), 1 without PDF, 1 not cached, 1 corrupt."""
    kle, kir = fest(46055), fest(45965)
    no_pdf = replace(fest(37052), statistic_pdf_url=None)
    missing = replace(fest(21055), statistic_pdf_url="https://www.schlussgang.ch/x/missing.pdf")
    corrupt = replace(fest(24110), statistic_pdf_url="https://www.schlussgang.ch/x/corrupt.pdf")
    fests = [kle, kir, no_pdf, missing, corrupt]
    conn = connect(tmp_path / "s.db")
    upsert_festivals(conn, fests)
    conn.close()
    pdfs = {kle.statistic_pdf_url: sheet_file("46055.pdf").read_bytes(),
            kir.statistic_pdf_url: sheet_file("45965.pdf").read_bytes(),
            corrupt.statistic_pdf_url: b"%PDF-1.4 not really a pdf"}
    with client(tmp_path, pdfs, offline=False) as c:  # simulate `crawl` filling the cache
        for f in (kle, kir, corrupt):
            c.get(f.statistic_pdf_url)  # type: ignore[arg-type]
    return tmp_path, fests


def run(tmp_path: Path, **kw: object) -> pr.ParseRunReport:
    conn = connect(tmp_path / "s.db")
    with client(tmp_path, {}, offline=True) as c:
        rep = pr.parse_all(conn, c, today=TODAY, **kw)  # type: ignore[arg-type]
    conn.close()
    return rep


def test_parse_all_stores_everything(setup: tuple[Path, list[Festival]]) -> None:
    tmp_path, _ = setup
    rep = run(tmp_path)
    assert rep.status == {"ok": 2, "no_pdf": 1, "pdf_not_cached": 1, "pdf_error": 1}
    conn = connect(tmp_path / "s.db")
    q = lambda s: [tuple(r) for r in conn.execute(s).fetchall()]  # noqa: E731
    assert q("SELECT COUNT(*) FROM bouts") == [(274 + 198,)]
    assert q("SELECT COUNT(*) FROM athletes_raw") == [(99 + 67,)]
    status = dict(q("SELECT fest_id, status FROM festival_parse"))
    assert status == {46055: "ok", 45965: "ok", 37052: "no_pdf", 21055: "pdf_not_cached",
                      24110: "pdf_error"}
    reasons = {r for (r,) in q("SELECT DISTINCT reason FROM parse_rejects")}
    assert {"no_statistic_pdf", "pdf_not_cached", "pdf_unreadable"} <= reasons
    # Kirchberg's two 0.00 lines are extra bouts with a NULL grade (schema v4 CHECK)
    assert q("SELECT COUNT(*) FROM bouts WHERE grade_a IS NULL OR grade_b IS NULL") == [(2,)]
    assert q("SELECT COUNT(*) FROM bouts WHERE flags LIKE '%extra_bout%'") == [(2,)]
    assert q("SELECT n_gaenge FROM festival_parse WHERE fest_id=45965") == [(6,)]
    assert q("SELECT n_bouts, n_entries, parser_version FROM festival_parse WHERE fest_id=46055") \
        == [(274, 548, pr.PARSER_VERSION)]


def test_parse_is_incremental(setup: tuple[Path, list[Festival]]) -> None:
    tmp_path, _ = setup
    run(tmp_path)
    rep = run(tmp_path)
    # unchanged: 2 parsed sheets, no_pdf, and the corrupt PDF (same sha); only the
    # missing PDF is re-checked (it may have been downloaded meanwhile)
    assert rep.unchanged == 4 and rep.status == {"pdf_not_cached": 1}
    forced = run(tmp_path, force=True)
    assert forced.unchanged == 0 and forced.bouts == 274 + 198


def test_reparse_replaces_rows(setup: tuple[Path, list[Festival]]) -> None:
    tmp_path, _ = setup
    run(tmp_path)
    run(tmp_path, force=True)
    conn = connect(tmp_path / "s.db")
    assert conn.execute("SELECT COUNT(*) FROM bouts").fetchone()[0] == 274 + 198


def test_low_pair_rate_setting_is_applied(setup: tuple[Path, list[Festival]]) -> None:
    tmp_path, _ = setup
    rep = run(tmp_path, min_pair_rate=1.01)  # both sheets pair 100 % -> both below
    assert rep.status["failed"] == 2 and rep.rejects["low_pair_rate"] == 2
