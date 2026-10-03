"""Frontend sources in ``web/`` (Phase 5): what can be checked without a browser.

Not covered here: that the scripts run. They were exercised by hand in headless Chrome
(see the Phase 5 handoff notes); no JavaScript engine is available to the test suite."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from src.config import REPO_ROOT
from tests.site_checks import check_page, css_defines, js_classes, js_ids, js_literals

WEB = REPO_ROOT / "web"
PAGES = ["index.html", "athlete.html", "fests.html", "about.html"]
SCRIPTS = sorted(p.name for p in (WEB / "js").glob("*.js"))
ECHARTS_SHA256 = "55974cf42cc160e6cf9099cd3ed8cdedfaa9779919fcee7e2272c85f6ca30e27"


def test_expected_files() -> None:
    assert sorted(p.name for p in WEB.glob("*.html")) == sorted(PAGES)
    assert SCRIPTS == ["about.js", "app.js", "athlete.js", "charts.js", "fests.js", "index.js"]
    assert (WEB / "css" / "style.css").is_file()


@pytest.mark.parametrize("name", PAGES)
def test_page_links_are_relative_and_resolve(name: str) -> None:
    page = check_page(WEB / name)
    scripts = [u for t, _, u in page.links if t == "script"]
    assert scripts and "js/app.js" in scripts
    assert scripts.index("js/app.js") < len(scripts) - 1  # app.js before the page script
    assert {"se-banner", "se-asof"} <= page.ids
    hrefs = {u for t, _, u in page.links if t == "a"}
    assert {"index.html", "fests.html", "about.html"} <= hrefs  # navigation on every page
    assert any(u.endswith("/issues") for u in hrefs)             # correction route


def test_only_the_athlete_page_loads_echarts() -> None:
    for name in PAGES:
        text = (WEB / name).read_text(encoding="utf-8")
        assert ("vendor/echarts" in text) == (name == "athlete.html")
        assert "cdn." not in text and "unpkg" not in text  # no CDN at run time


def test_vendored_echarts_is_the_pinned_build() -> None:
    data = (WEB / "vendor" / "echarts.common.min.js").read_bytes()
    assert hashlib.sha256(data).hexdigest() == ECHARTS_SHA256
    assert (WEB / "vendor" / "echarts.LICENSE.txt").is_file()
    assert (WEB / "vendor" / "echarts.NOTICE.txt").is_file()
    assert b"new Function" not in data and b"eval(" not in data  # works under the CSP


def test_stylesheet_defines_every_class_in_use() -> None:
    """Fails when classes were added without running scripts/build_css.sh."""
    css = (WEB / "css" / "style.css").read_text(encoding="utf-8")
    used: set[str] = set()
    ids: set[str] = set()
    for name in PAGES:
        page = check_page(WEB / name)
        used |= set(page.classes)
        ids |= page.ids
    for name in SCRIPTS:
        text = (WEB / "js" / name).read_text(encoding="utf-8")
        used |= js_classes(text)
        ids |= js_ids(text)
    used -= ids  # 'se-view' etc. are element ids passed to SE.$, not classes
    assert len(used) > 60 and {"se-chip-on", "se-tab-on", "se-up", "hidden"} <= used
    missing = sorted(t for t in used if not css_defines(css, t))
    assert missing == []
    assert "prefers-color-scheme:dark" in css.replace(" ", "")  # dark mode follows the system
    assert "min-width:640px" in css.replace(" ", "")            # phone-first breakpoints


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_use_relative_urls_and_no_unsafe_apis(name: str) -> None:
    text = (WEB / "js" / name).read_text(encoding="utf-8")
    for banned in ("eval(", "new Function", "document.write", "outerHTML", "insertAdjacentHTML"):
        assert banned not in text
    for lit in js_literals(text):
        assert not re.search(r'(href|src)="/', lit), lit
        assert not lit.startswith("/data") and "http://" not in lit, lit
        for m in re.finditer(r"https://[^\s\"'<>)]+", lit):
            assert m.group(0).startswith(("https://www.schlussgang.ch", "https://github.com/"
                                          "stefan19893/Schwinger-ELO")), lit
    # ids from the URL are validated before they become part of a file path
    if name == "athlete.js":
        assert "SE.ID_RE.test(id)" in text
    if name == "fests.js":
        assert r"/^\d{1,9}$/.test(id)" in text


def test_scripts_only_fetch_files_the_builder_writes() -> None:
    from src.exporter import static_builder as sb
    text = "".join((WEB / "js" / n).read_text(encoding="utf-8") for n in SCRIPTS)
    fetched = set(re.findall(r"getJSON\('(data/[a-z_]+\.json)'\)", text))
    fetched |= {f"data/{n}.json" for n in re.findall(r"load\('([a-z_0-9]+)'\)", text)}
    assert fetched == {"data/meta.json", "data/athletes.json", "data/festivals.json",
                       "data/rankings_latest.json", "data/seasons.json",
                       "data/alltime_top200.json"}
    assert "'data/history/history_' + id + '.json'" in text
    assert "'data/fests/fest_' + id + '.json'" in text
    # the column names the scripts read exist in the contracts
    cols = set(sb.RANKING_COLS + sb.SEARCH_COLS + sb.ALLTIME_COLS + sb.SEASON_COLS
               + sb.FESTIVAL_COLS + sb.FEST_ATHLETE_COLS + sb.FEST_BOUT_COLS + sb.HISTORY_COLS
               + sb.ATHLETE_SEASON_COLS)
    for flag, value in (("F_RANKED", sb.F_RANKED), ("F_FEW", sb.F_FEW_BOUTS),
                        ("F_INACTIVE", sb.F_INACTIVE), ("F_UNCERTAIN", sb.F_UNCERTAIN)):
        assert f"SE.{flag} = {value};" in text
    for flag in ("B_SCHLUSSGANG", "B_EXTRA", "B_NO_GRADE", "B_GANG_UNCERTAIN"):
        assert f"{flag} = {getattr(sb, flag)}" in text
    assert {"rank", "idle", "last", "unc", "pos", "before", "after", "pts", "gang", "res", "ga",
            "gb", "exp", "score", "fest", "fest_id", "peak"} <= cols


def test_all_data_strings_are_escaped_in_templates() -> None:
    """Every data field concatenated into HTML goes through SE.esc / a formatter."""
    raw = re.compile(
        r"\+\s*((?:r|a|h|s|f|n|x|o|b\.opp|m|c|meta)\.(?:name|club|fest|loc|location|tv|id|url))\s*(?:\+|;|\))")
    for name in SCRIPTS:
        for i, line in enumerate((WEB / "js" / name).read_text(encoding="utf-8").splitlines(), 1):
            for m in raw.finditer(line):
                before = line[:m.start(1)]
                assert re.search(r"(SE\.esc|SE\.athleteUrl|SE\.festUrl|encodeURIComponent|"
                                 r"SE\.norm|SE\.subline|SE\.athleteLink)\($", before.rstrip()), \
                    f"{name}:{i}: {m.group(1)} is put into HTML unescaped"


def test_ui_is_swiss_german() -> None:
    for name in PAGES:
        text = (WEB / name).read_text(encoding="utf-8")
        assert "ß" not in text and 'lang="de-CH"' in text
    for name in SCRIPTS:
        text = (WEB / "js" / name).read_text(encoding="utf-8")
        assert text.count("ß") == (1 if name == "app.js" else 0)  # only the search folding
    about = (WEB / "about.html").read_text(encoding="utf-8")
    for needle in ("schlussgang.ch", "inoffizielle", 'id="grenzen"', "Namensvetter", "2020",
                   "Teilverb", "Bestenliste aller Zeiten", "github.com/stefan19893/Schwinger-ELO"):
        assert needle in about, needle
    index_js = (WEB / "js" / "index.js").read_text(encoding="utf-8")
    assert "Keine Bestenliste aller Zeiten" in index_js and "ohne Rangierung" in index_js
