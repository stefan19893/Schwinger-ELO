"""Deploy guard (``python -m src.cli check-site``): is the built site fit to be published?

Run between ``build`` and the upload of the Pages artifact. It reads
``dist/data/meta.json`` and compares it with the *baseline*: the ``meta.json`` of the last
site that passed this check, kept in ``<data_dir>/published_meta.json`` (it travels with
the pipeline state, see :mod:`src.state_bundle`, so it needs no request to the live site
and exists before the first deployment once the owner has accepted a local build).

Never acceptable (no override):

* no ``meta.json``, an empty site (``meta.empty``), a count of athletes, ranked athletes,
  festivals or bouts that is zero, a missing page or core data file;
* per-athlete files (``data/history``, ``data/bouts``) that do not match the published
  athletes one to one: a selectable athlete would end in a 404 on his profile or on the
  comparison page, and a file too many would be an athlete who is not in the search index;
* a data file that refers to an athlete id which is not in the search index
  (``athletes.json``): the ranking, the all-time and season lists, the athlete rows of the
  festival files, the opponent lists of ``data/bouts`` and the namesakes of
  ``data/history``. The published athletes are exactly the search index; any other id is
  somebody the site must not name (withheld by the age rule, unnamed, unrated);
* a file of ``data/bouts`` that holds more, or something else, than its contract: a key
  beyond :data:`BOUT_KEYS`, a row that is not the seven values of :data:`BOUT_COLS`, an
  ``opp`` that is no index into the file's ``opps``, an opponent name that is not the
  search index's name of that id, a contribution at an unrated bout (or none at a rated
  one), or more rated rows at a festival than the athlete's history file counts bouts
  there. The file must say nothing about bouts against athletes who are not published; a
  row or a key too many is how such a statement would look;
* the ``--sample`` demo outside a ``--sample`` run;
* a site built with other publication settings than the configured ones;
* an age filter that withholds nobody (``publish_min_age`` > 0 and ``counts.withheld`` zero
  or missing), or a database whose portraits carry no birthday at all: the filter has
  lost its input.

Acceptable only with ``accept_changes`` (``check-site --accept-changes``), for changes that
are intended and have been looked at:

* no baseline (first deployment);
* a count that dropped by more than the tolerance (``guard_max_drop``; for the ranked
  athletes ``guard_max_drop_ranked``), e.g. after raising ``publish_min_age``;
* a data date older than the baseline's;
* publication settings looser than the baseline's (lower ``publish_min_age`` or
  ``publish_unknown_recent_seasons``, ``noindex`` switched off) - they publish more than
  the last accepted site did;
* **a site that publishes more people than the baseline**: fewer withheld athletes
  (``guard_max_drop_withheld``), more published athletes (``guard_max_rise``) or more
  ranked ones (``guard_max_rise_ranked``) - the signature of an age filter that stopped
  working (e.g. the source no longer exposes birthdays). When the data year advances by
  one, the cohort the baseline announced (``publish.release_next_year``) is allowed on top;
  any other change of the data year needs the override;
* a fall of the filter's inputs: the share of rated athletes with a known birth year, or
  the number of portraits with a birthday in the database (``guard_max_drop_birth_known``).

The baseline file is the accepted ``meta.json`` plus ``guard_inputs`` (figures read from
the database when the site was accepted; absent when there was no database).
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import Config

log = logging.getLogger(__name__)

BASELINE_NAME = "published_meta.json"
INPUTS_KEY = "guard_inputs"
GUARDED_COUNTS = ("athletes", "ranked", "festivals", "bouts")
# one file per published athlete: directory -> file name prefix
PER_ATHLETE_FILES = {"data/history": "history_", "data/bouts": "bouts_"}
REQUIRED_FILES = ("index.html", "athlete.html", "compare.html", "fests.html", "about.html",
                  "data/meta.json", "data/rankings_latest.json", "data/athletes.json",
                  "data/festivals.json", "data/seasons.json", "data/alltime_top200.json")
# the contract of data/bouts/bouts_<id>.json as the exporter writes it (static_builder:
# BOUT_SIDE_COLS, OTHER_FEST_COLS, B_UNRATED; a test keeps the two in step). Kept here so
# that the guard judges the files by its own list, not by whatever the build produced.
BOUT_KEYS = frozenset({"id", "opps", "names", "unc", "cols", "fests", "other"})
BOUT_COLS = ["gang", "opp", "res", "g", "go", "flags", "d"]
BOUT_OTHER_COLS = ["id", "name", "date", "cat"]
BOUT_UNRATED = 16


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


def db_inputs(cfg: Config) -> dict[str, int] | None:
    """Inputs of the age filter that only the database knows: portraits and how many of
    them carry a birthday. ``None`` when there is no database or no portraits table."""
    if not cfg.db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{cfg.db_path.resolve()}?mode=ro", uri=True)
        try:
            n, with_bd = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(birthday IS NOT NULL AND birthday != ''), 0) "
                "FROM portraits").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None
    return {"portraits": int(n), "portraits_with_birthday": int(with_bd)}


def _published(dist: Path) -> dict[str, Any] | None:
    """The athletes of the search index = everybody the site publishes, id -> name;
    ``None`` when the index is missing or unreadable (reported as a missing file)."""
    try:
        index = json.loads((dist / "data" / "athletes.json").read_bytes())
        col, name = index["cols"].index("id"), index["cols"].index("name")
        return {str(r[col]): r[name] for r in index["rows"]}
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None


def _published_ids(dist: Path) -> set[str] | None:
    """The ids of :func:`_published`."""
    people = _published(dist)
    return None if people is None else set(people)


@dataclass
class _Seen:
    """What the readers of :data:`ATHLETE_REFERENCES` share: the published names, and per
    athlete the rated bouts of each festival as his history file counts them."""
    names: dict[str, Any]
    fest_bouts: dict[str, dict[int, int]] = field(default_factory=dict)


def _per_athlete_problem(dist: Path) -> str | None:
    """The per-athlete directories must hold exactly the athletes of the search index."""
    ids = _published_ids(dist)
    if ids is None:
        return None
    for folder, prefix in PER_ATHLETE_FILES.items():
        path = dist / folder
        have = {p.name[len(prefix):-len(".json")] for p in path.glob(f"{prefix}*.json")} \
            if path.is_dir() else set()
        if have != ids:
            return (f"{folder}: {len(have)} files for {len(ids)} published athletes "
                    f"({len(ids - have)} missing, {len(have - ids)} without a search entry) "
                    f"- rebuild")
    return None


def _table_ids(table: dict[str, Any]) -> list[Any]:
    """The ``id`` column of a ``{cols, rows}`` table."""
    col = table["cols"].index("id")
    return [row[col] for row in table["rows"]]


def _ranking_ids(obj: dict[str, Any], stem: str, seen: _Seen) -> list[Any]:
    return _table_ids(obj)


def _season_ids(obj: dict[str, Any], stem: str, seen: _Seen) -> list[Any]:
    out: list[Any] = []
    for season in obj["seasons"]:
        out += _table_ids(season)
        if season["peak"] is not None:
            out.append(season["peak"]["id"])
    return out


def _fest_ids(obj: dict[str, Any], stem: str, seen: _Seen) -> list[Any]:
    return _table_ids(obj["athletes"])      # null = a row without profile, by design


def _is_int(v: Any) -> bool:
    return type(v) is int


def _is_grade(v: Any) -> bool:
    return v is None or (type(v) in (int, float) and math.isfinite(v))


def _bout_file_ids(obj: dict[str, Any], stem: str, seen: _Seen) -> list[Any]:
    """The athlete ids of a bouts file - after the file has been held against its
    contract. The file lists the athlete's bouts against *published* opponents and must
    say nothing about any other bout, so everything beyond the contract is refused: it is
    how a row or a remainder for a hidden bout would look."""
    if f"bouts_{obj['id']}" != stem:
        raise ValueError("the file holds another athlete's bouts")
    if set(obj) != BOUT_KEYS:
        raise ValueError("keys beyond or short of the contract")
    opps, names, unc = obj["opps"], obj["names"], obj["unc"]
    # the opponents' names stand next to their ids: one name per id, nothing else (a
    # name without an id could be anybody's and would escape the id check of the caller)
    if len(names) != len(opps) or not all(isinstance(n, str) and n for n in names):
        raise ValueError("names do not match the opponent list")
    if not all(_is_int(i) and 0 <= i < len(names) for i in unc):
        raise ValueError("unc is not a list of opponent indices")
    # ... and it is the published name of that id (an unknown id is the caller's finding)
    known = seen.names
    for o, n in zip(opps, names):
        if isinstance(o, str) and o in known and known[o] != n:
            raise ValueError("an opponent's name is not the search index's name of his id")
    if len(set(map(repr, opps))) != len(opps):
        raise ValueError("an opponent is listed twice")
    if obj["cols"] != BOUT_COLS:
        raise ValueError("other row columns than the contract's")
    other = obj["other"]
    if set(other) != {"cols", "rows"} or other["cols"] != BOUT_OTHER_COLS \
            or not all(type(r) is list and len(r) == len(BOUT_OTHER_COLS)
                       for r in other["rows"]):
        raise ValueError("the list of other festivals is not in the contract's shape")
    n_opps, width = len(opps), len(BOUT_COLS)
    history = seen.fest_bouts.get(obj["id"])
    for fest in obj["fests"]:
        if type(fest) is not list or len(fest) != 2 or not _is_int(fest[0]):
            raise ValueError("a festival entry is not [fest_id, rows]")
        rated = 0
        for row in fest[1]:
            if type(row) is not list or len(row) != width:
                raise ValueError("a row is not the seven values of the contract")
            gang, opp, res, g, go, flags, d = row
            if not (_is_int(opp) and 0 <= opp < n_opps):
                raise ValueError("opp is not an index into the opponent list")
            if not (_is_int(gang) and res in (0, 1, 2) and _is_int(res) and _is_int(flags)
                    and _is_grade(g) and _is_grade(go)):
                raise ValueError("a row holds values of another kind than the contract's")
            if flags & BOUT_UNRATED:
                if d is not None:
                    raise ValueError("a contribution at a bout that does not count")
            elif type(d) not in (int, float) or not math.isfinite(d):
                raise ValueError("a rated bout without a contribution")
            else:
                rated += 1
        # the history file counts all rated bouts of the festival, hidden ones included:
        # the listed ones can only be fewer or as many
        if history is not None and rated > history.get(fest[0], 0):
            raise ValueError("more rated rows at a festival than the history file has bouts")
    return [obj["id"], *opps]


def _history_ids(obj: dict[str, Any], stem: str, seen: _Seen) -> list[Any]:
    if f"history_{obj['id']}" != stem:
        raise ValueError("the file holds another athlete's history")
    cols = obj["history"]["cols"]
    fest, n = cols.index("fest_id"), cols.index("n")
    seen.fest_bouts[obj["id"]] = {r[fest]: r[n] for r in obj["history"]["rows"]
                                  if _is_int(r[n])}
    return [obj["id"], *(n["id"] for n in obj["namesakes"])]


# every data file that names athletes by id: (path or directory below data/, file pattern,
# reader). A festival row may carry no id (null); every id that is there must be published.
# The history files come before the bout files, which are held against them.
ATHLETE_REFERENCES = (
    ("rankings_latest.json", None, _ranking_ids),
    ("alltime_top200.json", None, _ranking_ids),
    ("seasons.json", None, _season_ids),
    ("fests", "fest_*.json", _fest_ids),
    ("history", "history_*.json", _history_ids),
    ("bouts", "bouts_*.json", _bout_file_ids),
)


def _reference_problems(dist: Path) -> tuple[list[str], int]:
    """Athlete ids in the data files that are not in the search index, as one line per
    group of files, and the number of files read. The lines carry counts only: an id is a
    name slug and the workflow log is public (the files are named at DEBUG)."""
    people = _published(dist)
    if people is None:
        return [], 0
    ids, seen = set(people), _Seen(names=people)
    problems, n_read = [], 0
    for name, pattern, reader in ATHLETE_REFERENCES:
        path = dist / "data" / name
        files = sorted(path.glob(pattern)) if pattern else [path]
        unknown: set[str] = set()
        n_bad_files = n_unreadable = 0
        for f in files:
            try:
                found = reader(json.loads(f.read_bytes()), f.stem, seen)
            except (OSError, ValueError, KeyError, TypeError, IndexError,
                    AttributeError) as err:
                n_unreadable += 1
                # the reason names no athlete (the readers' messages carry no id or name)
                log.debug("check-site: %s is unreadable or not in the expected shape (%s)",
                          f, err if isinstance(err, ValueError) else type(err).__name__)
                continue
            n_read += 1
            bad = {repr(i) for i in found
                   if i is not None and (not isinstance(i, str) or i not in ids)}
            if bad:
                n_bad_files += 1
                unknown |= bad
                log.debug("check-site: %s names %d athlete ids outside the search index",
                          f, len(bad))
        if n_unreadable:
            problems.append(f"data/{name}: {n_unreadable} file(s) missing, unreadable or not "
                            f"in the expected shape - rebuild")
        if unknown:
            problems.append(
                f"data/{name}: {len(unknown)} athlete id(s) in {n_bad_files} file(s) are not "
                f"in data/athletes.json - the site refers to athletes it does not publish "
                f"(-v names the files); rebuild, never deploy this site")
    return problems, n_read


def _year(meta: dict[str, Any]) -> int | None:
    text = str(meta.get("as_of") or "")
    return int(text[:4]) if text[:4].isdigit() else None


def _release(baseline: dict[str, Any], key: str) -> int | None:
    v = ((baseline.get("publish") or {}).get("release_next_year") or {}).get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _check_filter(cfg: Config, meta: dict[str, Any], baseline: dict[str, Any],
                  inputs: dict[str, int] | None, rep: GuardReport) -> None:
    """Does the site publish more people than the accepted one, or did the filter's
    inputs fall? (Only called with ``publish_min_age`` > 0.)"""
    new_year, old_year = _year(meta), _year(baseline)
    allow = {"athletes": 0, "ranked": 0}
    if new_year is not None and old_year is not None and new_year != old_year:
        got = {k: _release(baseline, k) for k in allow}
        if new_year == old_year + 1 and None not in got.values():
            allow = {k: int(v) for k, v in got.items()}          # type: ignore[arg-type]
            rep.notes.append(
                f"data year {old_year} -> {new_year}: the baseline announced "
                f"{allow['athletes']} athletes ({allow['ranked']} ranked) who are now "
                f"certainly {cfg.publish_min_age} and may be published")
        else:
            rep.changes.append(
                f"data year {old_year} -> {new_year}: more than one year ahead, or the "
                f"baseline does not say who is released - the age filter cannot be compared")
    old_w, new_w = _count(baseline, "withheld"), _count(meta, "withheld")
    if old_w is None:
        rep.changes.append("counts.withheld: the baseline has no value - it predates the "
                           "age-filter check; look at the site and accept it once")
    else:
        floor = max(old_w - allow["athletes"], 0) * (1 - cfg.guard_max_drop_withheld)
        line = (f"counts.withheld: {old_w} -> {new_w} (released by the year change: "
                f"{allow['athletes']}, allowed drop {cfg.guard_max_drop_withheld:.0%})")
        if new_w is None or new_w < floor:
            rep.changes.append(line + " - fewer athletes are withheld: is the age filter "
                                      "still fed with birth years?")
        else:
            rep.notes.append(line)
    for key, tol in (("athletes", cfg.guard_max_rise), ("ranked", cfg.guard_max_rise_ranked)):
        new, old = _count(meta, key), _count(baseline, key)
        if old is None or old <= 0 or new is None:
            continue                                   # reported by the drop check
        if new > old * (1 + tol) + allow[key]:
            rep.changes.append(
                f"counts.{key}: {old} -> {new} ({(new - old) / old:+.1%}; allowed rise "
                f"{tol:.0%} plus {allow[key]} released) - more athletes are published by "
                f"name than the age filter let through before")
    shares = []
    for m in (baseline, meta):
        known, rated = _count(m, "birth_year_known"), _count(m, "rated")
        shares.append(known / rated if known is not None and rated else None)
    if shares[0] is None:
        rep.changes.append("counts.birth_year_known: the baseline has no value - it predates "
                           "the age-filter check; look at the site and accept it once")
    else:
        line = (f"share of rated athletes with a known birth year: {shares[0]:.1%} -> "
                + (f"{shares[1]:.1%}" if shares[1] is not None else "unknown")
                + f" (allowed fall {cfg.guard_max_drop_birth_known:.0%} points)")
        if shares[1] is None or shares[1] < shares[0] - cfg.guard_max_drop_birth_known:
            rep.changes.append(line + " - the age filter is losing its input")
        else:
            rep.notes.append(line)
    old_bd = (baseline.get(INPUTS_KEY) or {}).get("portraits_with_birthday")
    if isinstance(old_bd, int) and old_bd > 0:
        new_bd = inputs.get("portraits_with_birthday") if inputs else None
        line = (f"portraits with a birthday: {old_bd} -> "
                f"{new_bd if new_bd is not None else 'unknown (no database)'} "
                f"(allowed fall {cfg.guard_max_drop_birth_known:.0%})")
        if new_bd is None or new_bd < old_bd * (1 - cfg.guard_max_drop_birth_known):
            rep.changes.append(line + " - the source of the birth years is drying up")
        else:
            rep.notes.append(line)
    elif inputs is not None:
        rep.notes.append(f"portraits with a birthday: {inputs['portraits_with_birthday']} "
                         f"(no baseline value)")


def check_site(cfg: Config, dist: Path, baseline: dict[str, Any] | None,
               accept_changes: bool = False) -> GuardReport:
    """Judge the site in ``dist`` against ``baseline`` (``None`` = there is none)."""
    rep = GuardReport(accepted=accept_changes)
    inputs = db_inputs(cfg)
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
    else:
        problem = _per_athlete_problem(dist)
        if problem:
            rep.fatal.append(problem)
        problems, n_read = _reference_problems(dist)
        rep.fatal.extend(problems)
        if not problems and n_read:
            rep.notes.append(f"athlete ids: every id in {n_read} data files is in the "
                             f"search index")
    if bool(meta.get("sample")) != cfg.sample:
        rep.fatal.append("the site holds the --sample demo data" if meta.get("sample")
                         else "the site holds real data but --sample was given")
    for key in GUARDED_COUNTS:
        n = _count(meta, key)
        if n is None or n <= 0:
            rep.fatal.append(f"counts.{key} = {n!r}: the site has no {key}")
    pub = meta.get("publish") or {}
    recent = cfg.publish_unknown_recent_seasons if cfg.publish_min_age > 0 else 0
    if pub.get("min_age") != cfg.publish_min_age or pub.get("noindex") != cfg.site_noindex \
            or pub.get("unknown_recent_seasons") != recent \
            or (meta.get("contact") or "") != cfg.contact_email:
        rep.fatal.append(
            f"the site was built with other publication settings (min_age "
            f"{pub.get('min_age')!r}, unknown_recent_seasons "
            f"{pub.get('unknown_recent_seasons')!r}, noindex {pub.get('noindex')!r}, contact "
            f"{'set' if meta.get('contact') else 'not set'}) than configured "
            f"({cfg.publish_min_age}, {recent}, {cfg.site_noindex}, "
            f"{'set' if cfg.contact_email else 'not set'}) - rebuild")
    if cfg.publish_min_age > 0 and not cfg.sample:
        n = _count(meta, "withheld")
        if n is None or n <= 0:
            rep.fatal.append(
                f"publish_min_age = {cfg.publish_min_age} but counts.withheld = {n!r}: the "
                f"age filter withholds nobody - it has lost its birth years")
        if inputs is not None and inputs["portraits"] > 0 \
                and inputs["portraits_with_birthday"] == 0:
            rep.fatal.append(
                f"none of the {inputs['portraits']} portraits in {cfg.db_path} has a "
                f"birthday: the age filter has lost its input")
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
    old_recent = old_pub.get("unknown_recent_seasons")
    if isinstance(old_recent, int) and cfg.publish_min_age > 0 \
            and cfg.publish_unknown_recent_seasons < old_recent:
        rep.changes.append(
            f"publish_unknown_recent_seasons lowered: {old_recent} -> "
            f"{cfg.publish_unknown_recent_seasons} (recent debutants without a birth year "
            f"would be published by name)")
    if old_pub.get("noindex") is True and not cfg.site_noindex:
        rep.changes.append("site_noindex switched off (the site becomes indexable)")
    if cfg.publish_min_age > 0:
        _check_filter(cfg, meta, baseline, inputs, rep)
    return rep


def record_baseline(dist: Path, path: Path, inputs: dict[str, int] | None = None) -> None:
    """Make the checked site's ``meta.json`` the new baseline (atomic replace). With
    ``inputs`` (:func:`db_inputs`) the file also carries them as ``guard_inputs``."""
    data = (dist / "data" / "meta.json").read_bytes()
    if inputs is not None:
        obj = json.loads(data.decode("utf-8"))
        obj[INPUTS_KEY] = inputs
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
