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
PAGES = ["index.html", "athlete.html", "compare.html", "fests.html", "about.html"]
CHART_PAGES = {"athlete.html", "compare.html"}
SCRIPTS = sorted(p.name for p in (WEB / "js").glob("*.js"))
ECHARTS_SHA256 = "55974cf42cc160e6cf9099cd3ed8cdedfaa9779919fcee7e2272c85f6ca30e27"


def test_expected_files() -> None:
    assert sorted(p.name for p in WEB.glob("*.html")) == sorted(PAGES)
    assert SCRIPTS == ["about.js", "app.js", "athlete.js", "charts.js", "compare.js", "fests.js",
                       "index.js"]
    assert (WEB / "css" / "style.css").is_file()


@pytest.mark.parametrize("name", PAGES)
def test_page_links_are_relative_and_resolve(name: str) -> None:
    page = check_page(WEB / name)
    scripts = [u for t, _, u in page.links if t == "script"]
    assert scripts and "js/app.js" in scripts
    assert scripts.index("js/app.js") < len(scripts) - 1  # app.js before the page script
    assert {"se-banner", "se-asof"} <= page.ids
    hrefs = {u for t, _, u in page.links if t == "a"}
    # navigation on every page
    assert {"index.html", "fests.html", "compare.html", "about.html"} <= hrefs
    assert any(u.endswith("/issues") for u in hrefs)             # correction route


def test_only_the_chart_pages_load_echarts() -> None:
    for name in PAGES:
        text = (WEB / name).read_text(encoding="utf-8")
        assert ("vendor/echarts" in text) == (name in CHART_PAGES)
        assert ("js/charts.js" in text) == (name in CHART_PAGES)
        assert ("js/compare.js" in text) == (name == "compare.html")
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
    if name == "compare.js":
        parse = text[text.index("function parseIds"):text.index("function compareUrl")]
        assert "SE.ID_RE.test(id)" in parse and "out.length >= MAX" in parse
        assert "if (!SE.ID_RE.test(id) || entries.length >= MAX" in text       # picker
        assert "if (SE.ID_RE.test(id) && list.indexOf(id) === -1)" in text     # suggestion


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
    assert "'data/bouts/bouts_' + id + '.json'" in text
    compare = (WEB / "js" / "compare.js").read_text(encoding="utf-8")
    assert f"B_UNRATED = {sb.B_UNRATED}" in compare
    # compare.js reads the bout rows by position
    assert sb.BOUT_SIDE_COLS == ["gang", "opp", "res", "g", "go", "flags"]
    assert "row = [gang, opp, res, g, go, flags]" in compare
    assert sb.OTHER_FEST_COLS == ["id", "name", "date", "cat"]
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
        r"\+\s*((?:r|a|b|d|e|h|e\.h|s|f|n|x|o|b\.opp|m|c|meta|info|winner)\."
        r"(?:name|club|fest|loc|location|tv|id|url|by|first|last))\s*(?:\+|;|\))")
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


def test_review_fixes_in_the_scripts() -> None:
    """Static checks for the Phase 5 review fixes (the scripts are not executed here)."""
    app = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    athlete = (WEB / "js" / "athlete.js").read_text(encoding="utf-8")
    index = (WEB / "js" / "index.js").read_text(encoding="utf-8")
    # the identity marker wherever an athlete is listed: namesake list, season peak
    assert "SE.uncertainMark(n.unc)" in athlete
    assert "SE.uncertainMark(s.peak.unc)" in index
    # an athlete without id but with a name is "ohne Wertung", not "Name nicht lesbar"
    link = app[app.index("SE.athleteLink = function"):app.index("SE.note = function")]
    assert link.index("!o.id && o.name") < link.index("ohne Wertung") < link.index("Name nicht lesbar")
    assert "SE.esc(o.name)" in link.split("Name nicht lesbar")[0]
    # day counts run to the data date and say so; nothing claims "today"
    for text in (athlete, index):
        assert not re.search(r"seit ' \+ SE\.num\((r|h)\.idle\)", text)
        assert "Datenstand" in text and "heute bei" not in text
        assert "Date.now" not in text and "new Date" not in text
    about = (WEB / "about.html").read_text(encoding="utf-8")
    assert "Jungaktiven- und U20-Anlässe" in about
    assert "Nachwuchs- und Frauenanlässe, Mannschaftsanlässe" not in about


def test_publication_switches_in_the_pages() -> None:
    """Static checks for the publication defaults (age filter, contact, indexing)."""
    app = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    about_js = (WEB / "js" / "about.js").read_text(encoding="utf-8")
    about = (WEB / "about.html").read_text(encoding="utf-8")
    # a withheld athlete is labelled before anything else is looked at, never linked
    link = app[app.index("SE.athleteLink = function"):app.index("SE.note = function")]
    assert link.index("o.anon") < link.index("Name nicht veröffentlicht") < link.index("!o.id")
    # the about page explains the rule with numbers from meta.json; hidden until filled
    assert '<p id="se-minors" class="hidden"></p>' in about
    for needle in ("pub.min_age > 0", "withheld_from_birth_year", "c.withheld_ranked",
                   "Jahrgang nicht bekannt", "nur die veröffentlichten Schwinger"):
        assert needle in about_js, needle
    # the e-mail route: no address in the sources, set as text / href from meta.contact
    assert '<p id="se-contact" class="hidden">' in about and "mailto:" not in about
    assert "@" not in about.split('id="se-contact"')[1].split("</p>")[0]
    assert "link.textContent = meta.contact" in about_js
    assert "link.setAttribute('href', 'mailto:' + meta.contact)" in about_js
    # indexing is a build setting (static_builder.apply_indexing), not part of the sources
    for name in PAGES:
        assert 'name="robots"' not in (WEB / name).read_text(encoding="utf-8")


def test_comparison_page_rules() -> None:
    """Static checks for the comparison page (its behaviour is covered by the browser
    smoke test)."""
    js = (WEB / "js" / "compare.js").read_text(encoding="utf-8")
    html = (WEB / "compare.html").read_text(encoding="utf-8")
    assert "var MAX = 6;" in js and "?ids=" in js
    # the identity marker wherever a name is shown: chips, legend, table heads, direct
    # bouts (nameHtml / head), the picker and the suggestions for an outdated id
    name_html = js[js.index("function nameHtml"):js.index("function head")]
    assert "SE.uncertainMark(e.h.unc)" in name_html
    assert js[js.index("function head"):js.index("function badges")].count("SE.uncertainMark(e.h.unc)") == 2
    assert js.count("(a.flags & SE.F_UNCERTAIN) ? SE.uncertainMark(1)") == 2
    assert "Identität unsicher" in js[js.index("formatter: function (p)"):js.index("xAxis:")]
    # the chart keeps the profile's honesty: the reversion comes from SE.careerSeries
    assert "SE.careerSeries(e.h)" in js and "s.reversion" in js and "1. April" in js
    # where the comparison invites it: eras, no common time
    for needle in ("Keine gemeinsame Zeit", "bis etwa 2016", "nicht direkt vergleichbar",
                   "about.html#grenzen", "jeder Gang einmal gezählt", "ohne Note",
                   "Noch niemand ausgewählt", "höchstens"):
        assert needle in js, needle
    # review fixes: the scale note only where it matters (not for every career that began
    # before 2016); "nicht gewertet" explained in visible text, not only in a tooltip; a
    # failed download names the id of the link and never shows the parser's message; a
    # hint to tap the legend from four athletes on
    assert "if (mixed || (ended && late))" in js and "early && late" not in js
    duels = js[js.index("function renderDuels"):js.index("function renderCommon")]
    assert "«nicht gewertet»" in duels and "zählen nicht für die Wertung" in duels
    assert "x.flags & B_UNRATED" in duels
    slots = js[js.index("function renderSlots"):js.index("function suggest")]
    assert "Die Daten zur Kennung «' + SE.esc(e.id)" in slots and ".message" not in js
    assert "if (shown.length > 3)" in js and "Einen Namen antippen" in js
    # day counts relate to the data date, nothing is computed from today's date
    assert "Date.now" not in js and "new Date" not in js and "Datenstand" in js
    # the search index and the ranking are only loaded on demand
    assert js.count("SE.loadSearchIndex()") == 3 and js.count("data/rankings_latest.json") == 1
    assert js.index("data/rankings_latest.json") > js.index("el.id === 'se-example'")
    # entry points: the navigation (test_page_links...) and the profile; the ranking rows
    # stay as they are
    athlete = (WEB / "js" / "athlete.js").read_text(encoding="utf-8")
    assert "href=\"compare.html?ids=' + encodeURIComponent(h.id) + '\"" in athlete
    assert "compare.html" not in (WEB / "js" / "index.js").read_text(encoding="utf-8")
    about = (WEB / "about.html").read_text(encoding="utf-8")
    assert "<strong>Vergleich.</strong>" in about and "einmal gezählt" in about
    for ident in ("se-picked", "se-add", "se-add-results", "se-add-hint", "se-slots", "se-view"):
        assert f'id="{ident}"' in html, ident


def test_comparison_axis_rules() -> None:
    """Static checks for the axis switch of the comparison chart (time, bouts, age, career
    season); that the four axes draw is covered by the browser smoke test."""
    js = (WEB / "js" / "compare.js").read_text(encoding="utf-8")
    # the URL value is compared with three constants and nothing else; it is read only
    # through parseX, so it can never reach the page or a file path
    assert "var X_PARAM = { bouts: 'gaenge', age: 'alter', season: 'saison' };" in js
    parse = js[js.index("function parseX"):js.index("function compareUrl")]
    assert parse.count("raw ===") == 3 and ": 'time';" in parse
    for forbidden in ("SE.esc(raw", "+ raw", "indexOf", "toLowerCase", "["):
        assert forbidden not in parse, forbidden
    assert js.count("SE.param('x')") == 2 and js.count("parseX(SE.param('x'))") == 2
    assert js.count("SE.param(") == 4                      # ids and x: first load and popstate
    # time is the default and leaves the address alone: x is appended for the three only
    url = js[js.index("function compareUrl"):js.index("function ids()")]
    assert "mode === 'bouts' || mode === 'age' || mode === 'season' ? X_PARAM[mode] : ''" in url
    assert "(v ? (ids.length ? '&' : '?') + 'x=' + v : '')" in url
    # the switch: four real links with German labels, a click switches in place and is in
    # the browser history
    assert ("var AXES = [['time', 'Zeit'], ['bouts', 'Gänge'], ['age', 'Alter'], "
            "['season', 'Karrieresaison']];") in js
    switch = js[js.index("function axisSwitch"):js.index("function names")]
    assert "SE.esc(compareUrl(ids(), a[0]))" in switch and "aria-current" in switch
    click = js[js.index("if (el.hasAttribute('data-x'))"):js.index("var y = window.scrollY;")]
    assert "parseX(X_PARAM[el.getAttribute('data-x')])" in click and "pushState" in click
    assert "ev.metaKey || ev.ctrlKey" in click
    # bout axis: cumulative n of the history rows; the reversion is its own dashed step
    bouts = js[js.index("function boutSeries"):js.index("function hasBirthYear")]
    assert "c += r.n;" in bouts and "reversion.push([c, prev.after], [c, r.before], [c, null]);" in bouts
    assert "tail: []" in bouts
    # age axis: the time series moved by the birth year; no birth year, no curve
    age = js[js.index("function ageSeries"):js.index("function seasonSeries")]
    assert "SE.careerSeries(e.h)" in age and "yearPos(p[0]) - by" in age
    assert "ui.x === 'age' ? list.filter(hasBirthYear) : list" in js
    # career seasons: the exporter's season rows, nothing re-derived from the festivals
    season = js[js.index("function seasonSeries"):js.index("function seriesOf")]
    assert "SE.table(e.h.seasons)" in season and "s.rating" in season and "e.rows" not in season
    assert "p.season.pos !== null ? p" in js               # no place = hollow point
    # caveats in visible text, each only when it applies to the selection
    notes = js[js.index("function axisNotes"):js.index("function chartSection")]
    assert notes.count("if (early.length)") == 3 and "SE.esc(firstSeason)" in notes
    for needle in ("nicht zwingend der erste der Laufbahn", "nicht zwingend die erste der Laufbahn",
                   "Bekannt ist nur der Jahrgang, nicht der Geburtstag", "bis zu einem Jahr darunter",
                   "gehört zu keinem Fest", "wie in der Tabelle «Saisons»", "hohler Punkt"):
        assert needle in notes, needle
    assert "vor etwa 2016" in js and "Jahrgang unbekannt:" in js
    assert "Von keinem der ausgewählten Schwinger ist der Jahrgang bekannt" in js
    assert "Im Jahr seines ' + SE.esc(p.data.age) + '. Geburtstags" in js   # no fractional age
