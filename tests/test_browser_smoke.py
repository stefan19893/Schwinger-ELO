"""Headless-browser smoke test: the scripts of the four pages run on a built site.

The rest of the suite never executes JavaScript. This module builds the ``--sample`` site,
serves it from a loopback server under ``/Schwinger-ELO/`` and loads every page in a
headless Chromium-family browser (``--dump-dom``). A page fails when the script-error
banner is set (``#se-banner[data-error]``), a loading error is shown, or its view stays
empty.

Browser lookup: ``SCHWINGEN_BROWSER`` (path), else ``google-chrome`` / ``chromium`` on the
PATH, else the Windows Chrome / Edge reachable from WSL. Without a browser the tests are
skipped - a fresh clone still needs only Python - unless ``SCHWINGEN_REQUIRE_BROWSER=1``
(set in CI, where the runner image ships Chrome): then a missing browser is a failure.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

import pytest

from src import cli

SUBPATH = "Schwinger-ELO"
PATH_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
WSL_PATHS = ("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
             "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
             "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
        "track", "wbr"}
LOAD_ERROR = "Die Daten konnten nicht geladen werden"


def find_browser() -> str | None:
    explicit = os.environ.get("SCHWINGEN_BROWSER")
    if explicit:
        return explicit if Path(explicit).is_file() else shutil.which(explicit)
    for name in PATH_NAMES:
        if found := shutil.which(name):
            return found
    return next((p for p in WSL_PATHS if Path(p).is_file()), None)


class Dom(HTMLParser):
    """Attributes and text content of every element that has an id."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.attrs: dict[str, dict[str, str]] = {}
        self.text: dict[str, str] = {}
        self.tags: list[str] = []
        self._open: list[tuple[str, str | None]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        self.tags.append(tag)
        if "id" in a:
            self.attrs[a["id"]] = a
            self.text.setdefault(a["id"], "")
        if tag not in VOID:
            self._open.append((tag, a.get("id")))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                del self._open[i:]
                return

    def handle_data(self, data: str) -> None:
        for _, ident in self._open:
            if ident is not None:
                self.text[ident] += data


def page_problems(html: str, view: str = "se-view") -> list[str]:
    """Why a dumped page counts as broken (empty list = fine)."""
    dom = Dom(html)
    problems = []
    banner = dom.attrs.get("se-banner")
    if banner is None:
        problems.append("no #se-banner: not one of the site's pages")
    elif "data-error" in banner:
        problems.append(f"script error banner: {dom.text['se-banner'].strip()[:200]}")
    if LOAD_ERROR in html:
        problems.append("a data file could not be loaded")
    if view not in dom.attrs:
        problems.append(f"no #{view}")
    elif len(dom.text[view].strip()) < 20:
        problems.append(f"#{view} is empty")
    return problems


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def browser() -> str:
    found = find_browser()
    if found is None:
        if os.environ.get("SCHWINGEN_REQUIRE_BROWSER", "").lower() in ("1", "true", "yes"):
            pytest.fail("SCHWINGEN_REQUIRE_BROWSER is set but no Chrome / Chromium was found")
        pytest.skip("no Chrome / Chromium available (set SCHWINGEN_BROWSER to a binary)")
    return found


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`all --sample` built into <root>/Schwinger-ELO; returns <root>."""
    root = tmp_path_factory.mktemp("smoke")
    mp = pytest.MonkeyPatch()
    for key in list(os.environ):
        if key.startswith("SCHWINGEN_") and key not in ("SCHWINGEN_BROWSER",
                                                         "SCHWINGEN_REQUIRE_BROWSER"):
            mp.delenv(key)
    mp.setenv("SCHWINGEN_DIST_DIR", str(root / SUBPATH))
    try:
        assert cli.main(["all", "--sample", "--data-dir", str(root / "data")]) == 0
    finally:
        mp.undo()
    return root


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


def _serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(_Quiet, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://localhost:{httpd.server_address[1]}/{SUBPATH}/"


@pytest.fixture(scope="module")
def base_url(site: Path) -> Iterator[str]:
    httpd, url = _serve(site)
    yield url
    httpd.shutdown()
    httpd.server_close()


def dump_dom(browser: str, url: str, profile: Path) -> str:
    """The page's DOM after its scripts ran (headless, throw-away profile, no extras)."""
    profile.mkdir(parents=True, exist_ok=True)
    profile_arg = str(profile)
    if browser.lower().endswith(".exe"):          # Windows browser driven from WSL
        profile_arg = subprocess.run(["wslpath", "-w", str(profile)], capture_output=True,
                                     text=True, check=True).stdout.strip()
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--disable-background-networking",
           "--disable-component-update", "--disable-sync", "--disable-extensions",
           "--disable-dev-shm-usage", f"--user-data-dir={profile_arg}",
           "--virtual-time-budget=10000", "--dump-dom", url]
    if os.environ.get("GITHUB_ACTIONS") or (hasattr(os, "geteuid") and os.geteuid() == 0):
        cmd.insert(1, "--no-sandbox")              # CI containers: no user namespaces
    html = ""
    for _ in range(3):   # a first start on a fresh profile sometimes prints nothing
        res = subprocess.run(cmd, capture_output=True, timeout=120)
        html = res.stdout.decode("utf-8", errors="replace")
        if "</html>" in html:
            break
    return html


def _load_all(browser: str, urls: dict[str, str], tmp: Path) -> dict[str, str]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {name: pool.submit(dump_dom, browser, url, tmp / f"profile-{i}")
                   for i, (name, url) in enumerate(urls.items())}
        return {name: f.result() for name, f in futures.items()}


@pytest.fixture(scope="module")
def pages(browser: str, site: Path, base_url: str,
          tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    data = site / SUBPATH / "data"
    ranked = json.loads((data / "rankings_latest.json").read_text(encoding="utf-8"))["rows"]
    history = json.loads((data / "history" / f"history_{ranked[0][1]}.json")
                         .read_text(encoding="utf-8"))["history"]["rows"]
    fest = history[-1][1]          # a festival of the top-ranked athlete (the profile's link)
    urls = {
        "index": base_url + "index.html",
        "seasons": base_url + "index.html#saison",
        "peaks": base_url + "index.html#bestwerte",
        "athlete": base_url + f"athlete.html?id={ranked[0][1]}",
        "athlete-unknown": base_url + "athlete.html?id=niemand-p0",
        "fests": base_url + "fests.html",
        "fest": base_url + f"fests.html?id={fest}&a={ranked[0][1]}",
        "about": base_url + "about.html",
    }
    return _load_all(browser, urls, tmp_path_factory.mktemp("profiles"))


# --------------------------------------------------------------------------- tests
def test_dom_helper_detects_broken_pages() -> None:
    ok = ('<div id="se-banner" class="x"></div><section id="se-view"><table><tr><td>'
          'Rang 1 Muster Hans 1712</td></tr></table></section>')
    assert page_problems(ok) == []
    assert page_problems(ok.replace('class="x"', 'class="x" data-error="1"'))[0].startswith(
        "script error banner")
    assert page_problems('<div id="se-banner"></div><section id="se-view">  </section>') == [
        "#se-view is empty"]
    assert "a data file could not be loaded" in page_problems(ok + LOAD_ERROR)
    assert page_problems("<html></html>")[0].startswith("no #se-banner")


@pytest.mark.parametrize("name", ["index", "seasons", "peaks", "athlete", "athlete-unknown",
                                  "fests", "fest"])
def test_page_renders_without_error(pages: dict[str, str], name: str) -> None:
    assert page_problems(pages[name]) == [], name


def test_about_page_is_filled_from_meta(pages: dict[str, str]) -> None:
    html = pages["about"]
    assert page_problems(html, view="se-facts") == []
    dom = Dom(html)
    assert "gewertete Gänge" in dom.text["se-facts"] and "K = 96" in dom.text["se-k"]
    # the age rule is explained (the sample withholds a few athletes), no contact invented
    assert "hidden" not in dom.attrs["se-minors"].get("class", "")
    assert "nicht mit Namen veröffentlicht" in dom.text["se-minors"]
    assert "hidden" in dom.attrs["se-contact"]["class"] and "mailto:" not in html
    assert '<meta name="robots" content="noindex">' in html


def test_pages_show_their_content(pages: dict[str, str], site: Path) -> None:
    data = site / SUBPATH / "data"
    ranked = json.loads((data / "rankings_latest.json").read_text(encoding="utf-8"))["rows"]
    top_name = ranked[0][2]
    assert top_name in Dom(pages["index"]).text["se-view"]
    assert "Demo-Daten" in Dom(pages["index"]).text["se-banner"]     # sample notice, no error
    assert "Datenstand" in Dom(pages["index"]).text["se-asof"]
    athlete = Dom(pages["athlete"])
    assert top_name in athlete.text["se-view"] and "canvas" in athlete.tags  # chart drawn
    assert "nicht gefunden" in Dom(pages["athlete-unknown"]).text["se-view"]
    assert "Feste" in Dom(pages["fests"]).text["se-view"]
    fest = Dom(pages["fest"])
    assert top_name in fest.text["se-view"] and "se-focus" in fest.attrs
    assert "Bestenliste aller Zeiten" in Dom(pages["peaks"]).text["se-view"]


def test_withheld_athletes_appear_without_name(browser: str, site: Path, base_url: str,
                                               tmp_path: Path) -> None:
    data = site / SUBPATH / "data"
    for path in sorted((data / "fests").iterdir()):
        rows = json.loads(path.read_text(encoding="utf-8"))["athletes"]["rows"]
        if any(r[-1] for r in rows):
            break
    else:
        pytest.skip("the sample has no athlete under the publication age")
    fest_id = path.stem.removeprefix("fest_")
    html = dump_dom(browser, base_url + f"fests.html?id={fest_id}", tmp_path / "p")
    assert page_problems(html) == []
    assert html.count("Jungschwinger, Name nicht veröffentlicht") >= sum(1 for r in rows if r[-1])
    assert "Name nicht lesbar" not in html


def test_smoke_check_catches_a_script_error(browser: str, site: Path, tmp_path: Path) -> None:
    """The check itself: a throwing page script and a missing data file are both caught."""
    broken = tmp_path / "site" / SUBPATH
    shutil.copytree(site / SUBPATH, broken)
    with (broken / "js" / "index.js").open("a", encoding="utf-8") as fh:
        fh.write("\nthrow new Error('smoke test');\n")
    (broken / "data" / "festivals.json").unlink()
    httpd, url = _serve(tmp_path / "site")
    try:
        got = _load_all(browser, {"index": url + "index.html", "fests": url + "fests.html"},
                        tmp_path / "profiles")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert any(p.startswith("script error banner") for p in page_problems(got["index"]))
    assert "a data file could not be loaded" in page_problems(got["fests"])
