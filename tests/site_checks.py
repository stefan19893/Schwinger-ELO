"""Checks on a set of static pages (``web/`` or a built ``dist/``) that need no browser."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ALLOWED_EXTERNAL = ("https://www.schlussgang.ch/", "https://github.com/stefan19893/Schwinger-ELO")
CLASS_TOKEN = re.compile(r"^[a-z0-9][a-z0-9:/.\-\[\]%]*$")


class Page(HTMLParser):
    def __init__(self, path: Path) -> None:
        super().__init__(convert_charrefs=True)
        self.path = path
        self.links: list[tuple[str, str, str]] = []   # (tag, attribute, url)
        self.classes: list[str] = []
        self.ids: set[str] = set()
        self.inline_scripts = 0
        self.inline_handlers: list[str] = []
        self.meta: dict[str, str] = {}
        self.lang: str | None = None
        self._in_script = False
        self.feed(path.read_text(encoding="utf-8"))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "meta":
            key = a.get("name") or a.get("http-equiv")
            if key:
                self.meta[key.lower()] = a.get("content", "")
        for attr in ("href", "src"):
            if attr in a:
                self.links.append((tag, attr, a[attr]))
        if "class" in a:
            self.classes += a["class"].split()
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "script" and "src" not in a:
            self.inline_scripts += 1
        self.inline_handlers += [k for k in a if k.startswith("on")]
        if "style" in a:
            self.inline_handlers.append("style")


def local_target(url: str) -> str | None:
    """Path part of a relative URL, ``None`` for external / data / fragment-only URLs."""
    if url.startswith(("https://", "http://", "data:", "mailto:")) or url.startswith("#"):
        return None
    return url.split("#", 1)[0].split("?", 1)[0]


def check_page(path: Path) -> Page:
    """Relative URLs only, every local target exists, CSP-compatible markup."""
    page = Page(path)
    root = path.parent
    assert page.lang and page.lang.startswith("de"), path
    assert "width=device-width" in page.meta.get("viewport", ""), path
    assert "script-src 'self'" in page.meta.get("content-security-policy", ""), path
    assert page.inline_scripts == 0 and not page.inline_handlers, (path, page.inline_handlers)
    for tag, attr, url in page.links:
        assert not url.startswith("/"), f"{path}: absolute URL {url!r} breaks under /Schwinger-ELO/"
        assert not url.startswith("http://"), f"{path}: insecure URL {url!r}"
        if url.startswith("https://"):
            assert tag == "a" and url.startswith(ALLOWED_EXTERNAL), f"{path}: external {url!r}"
            continue
        target = local_target(url)
        if target is None or target == "":
            continue
        assert ".." not in target, f"{path}: {url!r}"
        assert (root / target).is_file(), f"{path}: {url!r} does not exist"
    return page


def css_defines(css: str, token: str) -> bool:
    escaped = re.sub(r"([:/.\[\]%])", r"\\\\\1", token)
    return re.search(r"\." + escaped + r"(?![a-zA-Z0-9_\\-])", css) is not None


def js_literals(text: str) -> list[str]:
    """Contents of the single-quoted string literals of a script (comments removed)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return [m.group(1) for m in re.finditer(r"'((?:[^'\\\n]|\\.)*)'", text)]


def js_classes(text: str) -> set[str]:
    """Class names a script puts into the page: `class="..."` fragments inside string
    literals, plus literals that consist of class names only (classList / conditional)."""
    out: set[str] = set()
    for lit in js_literals(text):
        for m in re.finditer(r'class="([^"]*)', lit):
            out |= {t for t in m.group(1).split() if CLASS_TOKEN.match(t)}
        words = lit.split()
        if words and all(re.match(r"^se-[a-z-]+$", w) or w in ("hidden",) for w in words):
            out |= set(words)
    return out


def js_ids(text: str) -> set[str]:
    """Element ids a script creates (`id="..."` inside string literals)."""
    out: set[str] = set()
    for lit in js_literals(text):
        out |= set(re.findall(r'id="([^"\s]+)', lit))
    return out
