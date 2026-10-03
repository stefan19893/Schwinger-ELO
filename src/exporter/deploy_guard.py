"""Deploy guard (``python -m src.cli check-site``): is the built site fit to be published?

Run between ``build`` and the upload of the Pages artifact. It reads
``dist/data/meta.json`` and compares it with the *baseline*: the ``meta.json`` of the last
site that passed this check, kept in ``<data_dir>/published_meta.json`` (it travels with
the pipeline state, see :mod:`src.state_bundle`, so it needs no request to the live site
and exists before the first deployment once the owner has accepted a local build).

Never acceptable (no override):

* no ``meta.json``, an empty site (``meta.empty``), a count of athletes, ranked athletes,
  festivals or bouts that is zero, a missing page or core data file;
* the ``--sample`` demo outside a ``--sample`` run;
* a site built with other publication settings than the configured ones.

Acceptable only with ``accept_changes`` (``check-site --accept-changes``), for changes that
are intended and have been looked at:

* no baseline (first deployment);
* a count that dropped by more than the tolerance (``guard_max_drop``; for the ranked
  athletes ``guard_max_drop_ranked``), e.g. after raising ``publish_min_age``;
* a data date older than the baseline's;
* publication settings looser than the baseline's (lower ``publish_min_age``, ``noindex``
  switched off) - they publish more than the last accepted site did.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import Config

BASELINE_NAME = "published_meta.json"
GUARDED_COUNTS = ("athletes", "ranked", "festivals", "bouts")
REQUIRED_FILES = ("index.html", "athlete.html", "fests.html", "about.html",
                  "data/meta.json", "data/rankings_latest.json", "data/athletes.json",
                  "data/festivals.json", "data/seasons.json", "data/alltime_top200.json")


@dataclass
class GuardReport:
    fatal: list[str] = field(default_factory=list)       # never deployable
    changes: list[str] = field(default_factory=list)     # deployable only when accepted
    notes: list[str] = field(default_factory=list)
    accepted: bool = False

    @property
    def ok(self) -> bool:
        return not self.fatal and (self.accepted or not self.changes)


def baseline_path(cfg: Config) -> Path:
    return cfg.data_dir / BASELINE_NAME


def read_meta(path: Path) -> dict[str, Any] | None:
    """``meta.json`` as a dict, ``None`` when the file is missing or not a JSON object."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _count(meta: dict[str, Any], key: str) -> int | None:
    v = (meta.get("counts") or {}).get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def check_site(cfg: Config, dist: Path, baseline: dict[str, Any] | None,
               accept_changes: bool = False) -> GuardReport:
    """Judge the site in ``dist`` against ``baseline`` (``None`` = there is none)."""
    rep = GuardReport(accepted=accept_changes)
    meta = read_meta(dist / "data" / "meta.json")
    if meta is None:
        rep.fatal.append(f"{dist / 'data' / 'meta.json'} is missing or unreadable - run `build`")
        return rep
    missing = [n for n in REQUIRED_FILES if not (dist / n).is_file()
               or (dist / n).stat().st_size == 0]
    if missing:
        rep.fatal.append(f"missing or empty files in {dist}: {', '.join(missing)}")
    if meta.get("empty") is not False:
        rep.fatal.append("the site is empty (meta.empty) - it has no rating data")
    if bool(meta.get("sample")) != cfg.sample:
        rep.fatal.append("the site holds the --sample demo data" if meta.get("sample")
                         else "the site holds real data but --sample was given")
    for key in GUARDED_COUNTS:
        n = _count(meta, key)
        if n is None or n <= 0:
            rep.fatal.append(f"counts.{key} = {n!r}: the site has no {key}")
    pub = meta.get("publish") or {}
    if pub.get("min_age") != cfg.publish_min_age or pub.get("noindex") != cfg.site_noindex \
            or (meta.get("contact") or "") != cfg.contact_email:
        rep.fatal.append(
            f"the site was built with other publication settings (min_age "
            f"{pub.get('min_age')!r}, noindex {pub.get('noindex')!r}, contact "
            f"{'set' if meta.get('contact') else 'not set'}) than configured "
            f"({cfg.publish_min_age}, {cfg.site_noindex}, "
            f"{'set' if cfg.contact_email else 'not set'}) - rebuild")
    if cfg.site_noindex and not (dist / "robots.txt").is_file():
        rep.fatal.append("site_noindex is on but robots.txt is missing - rebuild")
    if rep.fatal:
        return rep
    if cfg.sample:
        rep.notes.append("--sample: demo site, not compared with a baseline")
        return rep
    if baseline is None:
        rep.changes.append(
            "no baseline: no site has been accepted yet (first deployment, or the pipeline "
            "state lost its published_meta.json) - look at the built site, then accept it")
        return rep

    for key in GUARDED_COUNTS:
        new, old = _count(meta, key), _count(baseline, key)
        if old is None or old <= 0:
            rep.notes.append(f"counts.{key}: no usable baseline value ({old!r})")
            continue
        tol = cfg.guard_max_drop_ranked if key == "ranked" else cfg.guard_max_drop
        change = (new - old) / old
        line = f"counts.{key}: {old} -> {new} ({change:+.1%}, allowed drop {tol:.0%})"
        if new < old * (1 - tol):
            rep.changes.append(line)
        else:
            rep.notes.append(line)
    new_date, old_date = str(meta.get("as_of") or ""), str(baseline.get("as_of") or "")
    if old_date and new_date < old_date:
        rep.changes.append(f"data date went back: {old_date} -> {new_date}")
    else:
        rep.notes.append(f"data date: {old_date or '-'} -> {new_date}")
    old_pub = baseline.get("publish") or {}
    old_age = old_pub.get("min_age")
    if isinstance(old_age, int) and cfg.publish_min_age < old_age:
        rep.changes.append(f"publish_min_age lowered: {old_age} -> {cfg.publish_min_age} "
                           f"(younger athletes would be published by name)")
    if old_pub.get("noindex") is True and not cfg.site_noindex:
        rep.changes.append("site_noindex switched off (the site becomes indexable)")
    return rep


def record_baseline(dist: Path, path: Path) -> None:
    """Make the checked site's ``meta.json`` the new baseline (atomic replace)."""
    data = (dist / "data" / "meta.json").read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
