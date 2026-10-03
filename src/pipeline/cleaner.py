"""Identity resolution orchestration (spec §4.1, Phase 3).

Flow (``run_clean``)::

    SQLite (read-only: sheets + athlete_evidence)  ->  ResolverInput(raw, bouts)
        ->  Resolver.resolve()
        -> Resolution(identity, athletes)  ->  validate  ->  remap bouts
        -> CleanResult (frames for src.pipeline.export)

The resolver is pluggable: anything with a ``name`` and a
``resolve(ResolverInput) -> Resolution`` method (:class:`Resolver`). The
shipped :class:`BaselineResolver` is a placeholder (exact cleaned-name key);
the real resolver (names.py / clubs.py) replaces it via :func:`default_resolver`.

Nothing is dropped silently: raw rows a resolver leaves unmapped stay in the
identity map with ``athlete_id = NULL``; bouts whose sides are unmapped, or
whose both sides resolve to the same athlete (``self_bout``, a red flag for
over-merging), go to ``bout_rejects`` with a reason.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

log = logging.getLogger("schwingen.clean")

# ----------------------------------------------------------------------------- inputs
#: athletes_raw columns passed to resolvers (all of them)
RAW_COLUMNS: tuple[str, ...] = (
    "athlete_raw_id", "fest_id", "idx", "rank", "name_raw", "name", "name_key",
    "name_base_key", "status", "mark", "sennen_turner", "withdrawn", "points",
    "points_mismatch", "n_entries", "grade_sum", "birth_year", "association", "place", "flags")
#: festival columns joined onto every raw row, renamed with a ``fest_`` prefix
FESTIVAL_JOIN: dict[str, str] = {
    "name": "fest_name", "date": "fest_date", "category": "fest_category",
    "eidg_type": "fest_eidg_type", "location": "fest_location",
    "association": "fest_association", "event_flags": "fest_event_flags",
    "elo_eligible": "fest_elo_eligible"}
#: per-raw-row evidence other sources may contribute (club lists, portraits).
#: Always present in ``ResolverInput.raw`` (NULL when no source provides them).
#: ``club`` / ``club_key``: canonical Schwingklub (src.pipeline.clubs); ``sub_association``:
#: BKSV / ISV / NOSV / NWSV / SWSV with its source in ``sub_assoc_source`` (code > club >
#: portrait > festival = weakest); ``portrait_id`` / ``portrait_slug``: schlussgang portrait.
OPTIONAL_RAW_COLUMNS: tuple[str, ...] = (
    "club", "sub_association", "residence", "portrait_slug",
    "club_key", "sub_assoc_source", "portrait_id")
#: athlete_evidence columns read by :func:`load_evidence` (schema v6, built by ``parse``)
EVIDENCE_COLUMNS: tuple[str, ...] = (*OPTIONAL_RAW_COLUMNS, "birth_year")
BOUT_COLUMNS: tuple[str, ...] = (
    "bout_id", "fest_id", "gang_nr", "athlete_a_id", "athlete_b_id", "outcome",
    "grade_a", "grade_b", "schlussgang", "flags")
#: athletes_raw.flags that make a row unusable as identity evidence (Phase 2 review)
NO_EVIDENCE_FLAGS: frozenset[str] = frozenset({"entries_overflow"})


@dataclass(frozen=True)
class ResolverInput:
    """What a resolver sees.

    ``raw``: one row per ``athletes_raw`` row = :data:`RAW_COLUMNS` +
    ``fest_year`` (int) + :data:`FESTIVAL_JOIN` columns + :data:`OPTIONAL_RAW_COLUMNS`
    (object dtype, NULL if unknown). ``bouts``: the ``bouts`` table
    (:data:`BOUT_COLUMNS`, athlete ids are ``athlete_raw_id`` values) for
    co-occurrence evidence (opponents per sheet).
    """

    raw: pd.DataFrame
    bouts: pd.DataFrame


#: columns of Resolution.identity (confidence/evidence optional, default NULL)
IDENTITY_COLUMNS: tuple[str, ...] = ("athlete_raw_id", "athlete_id", "confidence", "evidence")
#: columns of Resolution.athletes (all but athlete_id/full_name may be NULL)
ATHLETE_ATTR_COLUMNS: tuple[str, ...] = (
    "athlete_id", "full_name", "club", "sub_association", "birth_year", "slug",
    "confidence", "evidence")


@dataclass
class Resolution:
    """Resolver output.

    ``identity``: ``athlete_raw_id -> athlete_id`` (one row per mapped raw row;
    raw rows missing here count as unmapped). Optional per-row ``confidence``
    (0..1) and ``evidence`` (short text, e.g. ``"name+birth_year"``).

    ``athletes``: one row per ``athlete_id`` with :data:`ATHLETE_ATTR_COLUMNS`
    (missing optional columns are filled with NULL). ``athlete_id`` must be a
    stable, URL-safe string (see :func:`slugify`).
    """

    identity: pd.DataFrame
    athletes: pd.DataFrame


@runtime_checkable
class Resolver(Protocol):
    name: str

    def resolve(self, inp: ResolverInput) -> Resolution: ...


# ----------------------------------------------------------------------------- baseline
_PAREN_RE = re.compile(r"\([^)]*\)")
_TRAIL_NUM_RE = re.compile(r"(?:\s+\d{1,2})+$")  # sheet suffix " 1"/" 2", leftovers " 10"


def clean_name(name: str) -> str:
    """Display form: no parentheses / ', place' / stars / trailing sheet suffix."""
    s = unicodedata.normalize("NFC", name)
    s = _PAREN_RE.sub(" ", s)
    s = re.sub(r",.*$", "", s)
    s = s.replace("*", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return _TRAIL_NUM_RE.sub("", s).strip()


def name_key(name: str) -> str:
    """Baseline identity key: casefolded :func:`clean_name` (diacritics kept)."""
    return clean_name(name).casefold()


def slugify(text: str) -> str:
    """ASCII, URL-safe id fragment: 'Müller Jörg' -> 'muller-jorg'."""
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "x"


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def _mode(values: pd.Series) -> object:
    """Most frequent non-null value; ties -> the latest occurrence (input order)."""
    v = values.dropna()
    if v.empty:
        return None
    counts = v.value_counts(sort=False)
    best = counts.max()
    for x in reversed(v.tolist()):
        if counts[x] == best:
            return x
    return None  # pragma: no cover


class BaselineResolver:
    """Placeholder: one athlete per exact :func:`name_key` across all sheets.

    Rows sharing a key *within one sheet* are different people by construction;
    they become singleton athletes (confidence 0, evidence
    ``in_sheet_duplicate``) instead of being merged. No fuzzy matching, no
    splitting of namesakes across sheets: expect both over- and under-merging.
    """

    name = "baseline"

    def resolve(self, inp: ResolverInput) -> Resolution:
        raw = inp.raw.sort_values(["fest_date", "fest_id", "idx"], kind="stable")
        key = raw["name"].map(name_key)
        dup = pd.DataFrame({"f": raw["fest_id"], "k": key}).duplicated(keep=False)
        group = key.where(~dup, key + "\x00" + raw["athlete_raw_id"])
        # athlete_id = slug of the key; distinct keys with the same slug get a hash
        slug = key.map(slugify)
        base = slug.where(~dup, slug + "--" + raw["athlete_raw_id"])
        clash = pd.DataFrame({"b": base, "g": group}).groupby("b")["g"].transform("nunique") > 1
        ids = base.where(~clash, base + "-" + group.map(_short_hash))
        identity = pd.DataFrame({
            "athlete_raw_id": raw["athlete_raw_id"].to_numpy(),
            "athlete_id": ids.to_numpy(),
            "confidence": (~dup).astype(float).to_numpy(),
            "evidence": dup.map({True: "in_sheet_duplicate", False: "exact_name"}).to_numpy(),
        })
        df = raw.assign(athlete_id=ids, display=raw["name"].map(clean_name))
        g = df.groupby("athlete_id", sort=True)
        athletes = pd.DataFrame({
            "full_name": g["display"].agg(_mode),
            "club": g["club"].agg(_mode),
            "sub_association": g["sub_association"].agg(_mode),
            "birth_year": g["birth_year"].agg(_mode),
            "slug": g["portrait_slug"].agg(_mode),
        }).reset_index()
        conf = identity.groupby("athlete_id")["confidence"].min()
        athletes["confidence"] = athletes["athlete_id"].map(conf)
        athletes["evidence"] = athletes["confidence"].map(
            lambda c: "exact_name" if c == 1.0 else "in_sheet_duplicate")
        return Resolution(identity=identity, athletes=athletes)


def default_resolver() -> Resolver:
    """The resolver ``cli clean`` uses (swap in the real one in Phase 3 task 2)."""
    return BaselineResolver()


# ----------------------------------------------------------------------------- loading
def connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open the staging DB read-only (``clean`` never writes to SQLite)."""
    if not db_path.is_file():
        raise FileNotFoundError(f"{db_path} not found - run `parse` first")
    return sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)


def _table(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn)


@dataclass
class CleanInputs:
    raw: pd.DataFrame        # ResolverInput.raw
    bouts: pd.DataFrame      # bouts table
    festivals: pd.DataFrame  # festivals table + festival_parse status


def load_inputs(conn: sqlite3.Connection,
                extra_raw: Iterable[pd.DataFrame] = ()) -> CleanInputs:
    """Read everything ``clean`` needs in one read transaction.

    ``extra_raw``: frames keyed on ``athlete_raw_id`` carrying any of
    :data:`OPTIONAL_RAW_COLUMNS` (e.g. club / portrait sources); left-joined,
    later frames fill gaps left by earlier ones. A ``birth_year`` column in a
    frame fills gaps of the statistic sheet's birth year (never overrides it).
    """
    conn.execute("BEGIN")  # consistent snapshot while other processes write
    try:
        fests = _table(conn, "SELECT * FROM festivals ORDER BY date, fest_id")
        has_parse = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'festival_parse'"
                                 ).fetchone()
        parse = (_table(conn, "SELECT fest_id, status AS parse_status, n_gaenge "
                              "FROM festival_parse") if has_parse
                 else pd.DataFrame(columns=["fest_id", "parse_status", "n_gaenge"]))
        raw = _table(conn, f"SELECT {', '.join(RAW_COLUMNS)} FROM athletes_raw")
        bouts = _table(conn, f"SELECT {', '.join(BOUT_COLUMNS)} FROM bouts ORDER BY bout_id")
    finally:
        conn.rollback()
    fests = fests.merge(parse, on="fest_id", how="left")
    fj = fests[["fest_id", *FESTIVAL_JOIN]].rename(columns=FESTIVAL_JOIN)
    raw = raw.merge(fj, on="fest_id", how="left", validate="many_to_one")
    missing = raw["fest_date"].isna()
    if missing.any():
        raise ValueError(f"{int(missing.sum())} athletes_raw rows reference unknown festivals")
    raw["fest_year"] = raw["fest_date"].str[:4].astype(int)
    for col in OPTIONAL_RAW_COLUMNS:
        raw[col] = pd.Series([None] * len(raw), index=raw.index, dtype=object)
    for extra in extra_raw:
        cols = [c for c in EVIDENCE_COLUMNS if c in extra.columns]
        if extra["athlete_raw_id"].duplicated().any():
            raise ValueError("extra_raw frames must be unique per athlete_raw_id")
        m = raw[["athlete_raw_id"]].merge(extra[["athlete_raw_id", *cols]],
                                          on="athlete_raw_id", how="left")
        for c in cols:
            fill = m[c]
            if c == "birth_year":  # same text form as the statistic sheets ('1995')
                fill = fill.map(lambda v: None if pd.isna(v) else str(int(v))).astype(object)
            raw[c] = raw[c].where(raw[c].notna(), fill.to_numpy())
    raw = raw.sort_values(["fest_date", "fest_id", "idx"], kind="stable").reset_index(drop=True)
    return CleanInputs(raw=raw, bouts=bouts, festivals=fests)


def load_evidence(conn: sqlite3.Connection) -> pd.DataFrame | None:
    """The ``athlete_evidence`` table (ranking lists, portraits, normalised clubs;
    built by ``parse``) as an ``extra_raw`` frame; None when the staging DB predates
    schema v6 or ``parse`` has not built it yet."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'athlete_evidence'").fetchone():
        return None
    ev = _table(conn, f"SELECT athlete_raw_id, {', '.join(EVIDENCE_COLUMNS)} "
                      "FROM athlete_evidence")
    return ev if len(ev) else None


# ----------------------------------------------------------------------------- validation
def validate_resolution(res: Resolution, raw: pd.DataFrame) -> Resolution:
    """Normalise columns and enforce the contract; raise on programming errors."""
    ident = res.identity.copy()
    for col in IDENTITY_COLUMNS:
        if col not in ident.columns:
            ident[col] = None
    ident = ident[list(IDENTITY_COLUMNS)]
    ident = ident[ident["athlete_id"].notna()]
    if ident["athlete_raw_id"].duplicated().any():
        dups = ident.loc[ident["athlete_raw_id"].duplicated(), "athlete_raw_id"].head(5).tolist()
        raise ValueError(f"resolver mapped raw rows twice, e.g. {dups}")
    unknown = set(ident["athlete_raw_id"]) - set(raw["athlete_raw_id"])
    if unknown:
        raise ValueError(f"resolver returned {len(unknown)} unknown raw ids, "
                         f"e.g. {sorted(unknown)[:5]}")
    ath = res.athletes.copy()
    for col in ATHLETE_ATTR_COLUMNS:
        if col not in ath.columns:
            ath[col] = None
    ath = ath[list(ATHLETE_ATTR_COLUMNS)]
    if ath["athlete_id"].isna().any() or ath["athlete_id"].duplicated().any():
        raise ValueError("resolver athletes need unique, non-null athlete_id")
    if ath["full_name"].isna().any():
        raise ValueError("resolver athletes need a full_name")
    undefined = set(ident["athlete_id"]) - set(ath["athlete_id"])
    if undefined:
        raise ValueError(f"{len(undefined)} athlete ids in identity without attributes, "
                         f"e.g. {sorted(undefined)[:5]}")
    orphans = set(ath["athlete_id"]) - set(ident["athlete_id"])
    if orphans:
        log.warning("clean: %d athletes without any raw row dropped (e.g. %s)",
                    len(orphans), sorted(orphans)[:5])
        ath = ath[~ath["athlete_id"].isin(orphans)]
    return Resolution(identity=ident.reset_index(drop=True), athletes=ath.reset_index(drop=True))


# ----------------------------------------------------------------------------- remapping
@dataclass
class RemapResult:
    bouts: pd.DataFrame    # canonical ids + athlete_{a,b}_raw_id
    rejects: pd.DataFrame  # same columns + reason
    counts: dict[str, int] = field(default_factory=dict)


def remap_bouts(bouts: pd.DataFrame, identity: pd.DataFrame) -> RemapResult:
    """Replace per-sheet raw ids by canonical athlete ids.

    Rejects (reason): ``unmapped_a`` / ``unmapped_b`` / ``unmapped_both`` (a side
    without identity) and ``self_bout`` (both sides map to one athlete, i.e. the
    resolver merged two people who fought each other).
    """
    m = dict(zip(identity["athlete_raw_id"], identity["athlete_id"]))
    out = bouts.rename(columns={"athlete_a_id": "athlete_a_raw_id",
                                "athlete_b_id": "athlete_b_raw_id"})
    out["athlete_a_id"] = out["athlete_a_raw_id"].map(m)
    out["athlete_b_id"] = out["athlete_b_raw_id"].map(m)
    na_a, na_b = out["athlete_a_id"].isna(), out["athlete_b_id"].isna()
    reason = pd.Series(None, index=out.index, dtype=object)
    reason[na_a & na_b] = "unmapped_both"
    reason[na_a & ~na_b] = "unmapped_a"
    reason[~na_a & na_b] = "unmapped_b"
    reason[~na_a & ~na_b & (out["athlete_a_id"] == out["athlete_b_id"])] = "self_bout"
    bad = reason.notna()
    rejects = out[bad].assign(reason=reason[bad]).reset_index(drop=True)
    counts = {"bouts_in": len(out), "bouts_out": int((~bad).sum()),
              **{f"rejected_{k}": int(v) for k, v in reason[bad].value_counts().items()}}
    return RemapResult(bouts=out[~bad].reset_index(drop=True), rejects=rejects, counts=counts)


# ----------------------------------------------------------------------------- assembling
@dataclass
class CleanResult:
    bouts: pd.DataFrame
    bout_rejects: pd.DataFrame
    athletes: pd.DataFrame
    festivals: pd.DataFrame
    identity_map: pd.DataFrame
    counts: dict[str, int]
    resolver: str


def _athlete_stats(raw: pd.DataFrame, identity: pd.DataFrame,
                   bouts: pd.DataFrame) -> pd.DataFrame:
    rows = raw[["athlete_raw_id", "fest_id", "fest_year"]].merge(identity, on="athlete_raw_id")
    g = rows.groupby("athlete_id")
    stats = pd.DataFrame({
        "active_years": g["fest_year"].agg(lambda s: sorted(set(int(x) for x in s))),
        "first_season": g["fest_year"].min(),
        "last_season": g["fest_year"].max(),
        "n_festivals": g["fest_id"].nunique(),
        "n_raw": g.size(),
    })
    sides = pd.concat([bouts["athlete_a_id"], bouts["athlete_b_id"]])
    stats["n_bouts"] = sides.value_counts().reindex(stats.index, fill_value=0)
    return stats.reset_index()


def assemble(inputs: CleanInputs, resolution: Resolution, resolver_name: str) -> CleanResult:
    """Validate a resolution and build all output frames (pure, no I/O)."""
    res = validate_resolution(resolution, inputs.raw)
    remap = remap_bouts(inputs.bouts, res.identity)
    fest = inputs.festivals.set_index("fest_id")
    bouts = remap.bouts.join(fest[["date", "category", "event_flags", "elo_eligible"]],
                             on="fest_id")
    athletes = res.athletes.merge(_athlete_stats(inputs.raw, res.identity, bouts),
                                  on="athlete_id", how="left")
    athletes = athletes.sort_values("athlete_id").reset_index(drop=True)
    ident = inputs.raw[["athlete_raw_id", "fest_id", "name_raw", "name", "birth_year", "club",
                        "sub_association", "residence", "portrait_slug"]].merge(
        res.identity, on="athlete_raw_id", how="left")
    ident["resolver"] = resolver_name
    festivals = inputs.festivals.copy()
    festivals["n_athletes_raw"] = festivals["fest_id"].map(
        inputs.raw["fest_id"].value_counts()).fillna(0).astype(int)
    festivals["n_bouts"] = festivals["fest_id"].map(
        bouts["fest_id"].value_counts()).fillna(0).astype(int)
    self_bouts = remap.rejects[remap.rejects["reason"] == "self_bout"]
    counts = {
        "raw_athletes": len(inputs.raw),
        "raw_unmapped": int(ident["athlete_id"].isna().sum()),
        "athletes": len(athletes),
        "festivals": len(festivals),
        **remap.counts,
        "self_bouts": len(self_bouts),
        "unmapped_bouts": int(remap.rejects["reason"].str.startswith("unmapped").sum()),
        "bouts_elo_eligible": int(bouts["elo_eligible"].astype(bool).sum()),
    }
    return CleanResult(bouts=bouts, bout_rejects=remap.rejects, athletes=athletes,
                       festivals=festivals, identity_map=ident, counts=counts,
                       resolver=resolver_name)


def run_clean(db_path: Path, out_dir: Path, resolver: Resolver | None = None,
              extra_raw: Sequence[pd.DataFrame] = ()) -> CleanResult:
    """Read the staging DB (read-only), resolve identities, write Parquet outputs."""
    from src.pipeline.export import write_outputs

    resolver = resolver or default_resolver()
    conn = connect_ro(db_path)
    try:
        evidence = load_evidence(conn)
        # caller-supplied frames first: the stored evidence only fills their gaps
        inputs = load_inputs(conn, [*extra_raw, *([] if evidence is None else [evidence])])
    finally:
        conn.close()
    log.info("clean: %d raw athletes, %d bouts, %d festivals from %s",
             len(inputs.raw), len(inputs.bouts), len(inputs.festivals), db_path)
    if evidence is None:
        log.warning("clean: no athlete_evidence in %s (run `parse`) - club, Teilverband, "
                    "residence and portrait stay empty", db_path)
    else:
        n = len(inputs.raw)
        log.info("clean: evidence per raw row: %s", ", ".join(
            f"{c} {100 * inputs.raw[c].notna().sum() / max(n, 1):.1f}%"
            for c in ("club", "sub_association", "residence", "birth_year", "portrait_slug")))
    resolution = resolver.resolve(ResolverInput(raw=inputs.raw, bouts=inputs.bouts))
    result = assemble(inputs, resolution, resolver.name)
    report(result)
    write_outputs(result, out_dir)
    return result


def report(result: CleanResult) -> None:
    c = result.counts
    log.info("clean: resolver=%s: %d raw athletes -> %d athletes (%d raw rows unmapped)",
             result.resolver, c["raw_athletes"], c["athletes"], c["raw_unmapped"])
    log.info("clean: %d of %d bouts kept (%d elo-eligible), %d unmapped, %d self-bouts",
             c["bouts_out"], c["bouts_in"], c["bouts_elo_eligible"], c["unmapped_bouts"],
             c["self_bouts"])
    if c["raw_unmapped"]:
        log.warning("clean: %d raw athlete rows have no athlete_id (see identity_map)",
                    c["raw_unmapped"])
    if c["unmapped_bouts"]:
        log.warning("clean: %d bouts with an unmapped side -> bout_rejects", c["unmapped_bouts"])
    if c["self_bouts"]:
        sample = result.bout_rejects.loc[result.bout_rejects["reason"] == "self_bout",
                                         "athlete_a_id"].value_counts().head(5)
        log.warning("clean: %d self-bouts (over-merged identities?) -> bout_rejects; "
                    "top: %s", c["self_bouts"],
                    ", ".join(f"{k}={v}" for k, v in sample.items()))
