"""Central configuration: paths, year range, rate limits and ELO parameters.

Precedence: CLI flag > environment variable (``SCHWINGEN_<FIELD>``) > defaults here.
Every scalar field of :class:`Config` can be overridden via the environment, e.g.
``SCHWINGEN_FROM_YEAR=2015`` or ``SCHWINGEN_DATA_DIR=/tmp/data``.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

ENV_PREFIX = "SCHWINGEN_"

# A plain address only: it is written into meta.json and becomes a mailto: link.
CONTACT_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")

REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# --sample runs write here unless a data dir is given explicitly, so they never
# touch the real SQLite db / Parquet files in data/.
SAMPLE_DATA_DIR: Path = REPO_ROOT / "data" / "sample"

# The --sample dataset has five festivals (at most ~20 bouts per athlete): a lower
# provisional threshold keeps its rankings non-empty (explicit settings still win).
SAMPLE_PROVISIONAL_MIN_BOUTS = 6
# The --sample dataset has almost no birth years (46 trimmed portraits) and four seasons:
# the rule for unknown birth years would withhold 152 of its 547 athletes (130 of the 143
# ranked). The local demo therefore runs without it unless it is set explicitly.
SAMPLE_PUBLISH_UNKNOWN_RECENT_SEASONS = 0
# The --sample dataset starts in 2011: its burn-in season is 2011, not the real data's 2004.
SAMPLE_FIRST_RANKED_SEASON = 2012

# Politeness floor (spec §4.1): no configuration may go below this delay.
MIN_REQUEST_DELAY = 0.5

# Festival category -> K-factor (spec §4.2.3, user decisions 2026-09-29). These are the
# base values; the engine multiplies every one of them by Config.elo_k_scale.
DEFAULT_K_FACTORS: dict[str, float] = {
    "ESAF": 48.0,  # ESAF + Kilchberg, Unspunnen, ESV-Jubiläumsschwingfest (eidg. Kranz)
    "Bergkranz": 40.0,  # the six Bergkranzfeste: Brünig, Rigi, Schwägalp, Schwarzsee, Stoos, Weissenstein
    "Teilverband": 32.0,
    "Kantonal": 24.0,
    "Gauverband": 24.0,  # same Kranzfest tier as Kantonal (reference data)
    "Regional": 16.0,
}


@dataclass(frozen=True)
class Config:
    # --- Paths -------------------------------------------------------------
    data_dir: Path = REPO_ROOT / "data"
    dist_dir: Path = REPO_ROOT / "dist"
    web_dir: Path = REPO_ROOT / "web"
    sample_dir: Path = REPO_ROOT / "tests" / "fixtures" / "sample"

    # --- Run mode ----------------------------------------------------------
    sample: bool = False
    refresh: bool = False
    offline: bool = False  # crawl from data/raw only; cache misses are reported, never fetched

    # --- Data range --------------------------------------------------------
    # First season of the data set: `crawl` lists festivals from this year on and `clean`
    # puts the festivals from this year on into data/processed (earlier seasons that are
    # in the staging database stay there). 2004 since Phase 10 (was 2011): the first
    # season whose Kranzfest sheets on schlussgang.ch are mostly complete (27-37 of 38
    # per season 2004-2010; no Regional festivals before 2011). 2002 and 2003 are
    # crawled and parsed too, but their sheets are extracts of the first ranks (half of
    # the bouts are seen from one side only); 2001 has a sheet for 10 of 38 festivals.
    from_year: int = 2004
    to_year: int = _dt.date.today().year

    # --- Politeness (spec §4.1) ---------------------------------------------
    request_delay_min: float = 0.5
    request_delay_max: float = 1.0
    request_timeout: float = 30.0
    max_retries: int = 4
    # Safety cap on network requests per crawl run (listing pages only in Phase 1).
    crawl_max_requests: int = 1500
    # A year's listing pages are re-fetched when older than this, until they
    # were fetched after Dec 31 of that year + listing_final_grace_days
    # (late festivals / late PDF uploads); after that they are cached forever.
    listing_max_age_hours: float = 24.0
    listing_final_grace_days: int = 60
    # Statistic PDFs (bout source, Phase 2): own request cap per run; PDFs of
    # past seasons are cached forever, current-season ones are re-checked when
    # older than pdf_max_age_hours until fetched pdf_final_grace_days after the
    # festival date.
    crawl_pdfs: bool = True  # `crawl` also downloads statistic PDFs (--no-pdfs to skip)
    pdf_max_requests: int = 2500
    pdf_max_age_hours: float = 24.0
    pdf_final_grace_days: int = 14
    # Borderline festivals (festivals.event_flags) excluded from ELO; proposed
    # default awaiting user confirmation (Phase 2): team events and Ausland.
    elo_exclude_flags: str = "team,ausland"
    # Parse (Phase 2): sheets where fewer than this share of entries pair into
    # bouts are treated as structurally unreliable (no bouts imported).
    parse_min_pair_rate: float = 0.5
    # Backfill of old seasons (`crawl --backfill`, Phase 10): a one-time bulk of old files
    # nobody is waiting for, so it runs clearly slower than the normal crawl - a random
    # 2-4 s between requests (~20 requests a minute at most) - with its own, smaller cap
    # per run (listings + statistic + ranking PDFs together), and it gives up instead of
    # insisting: HTTP 403 / 429 stop the run at once (no retry), other errors are retried
    # once and the run stops after backfill_max_errors failed files in a row.
    backfill: bool = False
    backfill_delay_min: float = 2.0
    backfill_delay_max: float = 4.0
    backfill_max_requests: int = 400
    backfill_max_errors: int = 3
    # Upper bound for honouring a server's Retry-After header (seconds).
    retry_after_max: float = 300.0
    user_agent: str = (
        "Schwinger-ELO/0.1 (non-commercial research project; "
        "historical Schwingen ELO ratings; "
        "+https://github.com/stefan19893/Schwinger-ELO)"
    )

    # --- ELO (spec §4.2) -----------------------------------------------------
    elo_initial: float = 1500.0
    elo_scale: float = 400.0
    # How a festival's bouts are applied (src/pipeline/elo_engine.py): "festival" = all
    # bouts from the pre-festival ratings, "phase" = Gänge 1-4 then 5+, "gang" = Gang by
    # Gang, "sequential" = bout by bout.
    elo_update_mode: str = "festival"
    elo_phase_split_gang: int = 4
    # Multiplier on every K-factor of k_factors (tier ratios unchanged): effective K =
    # elo_k_scale * k_factors[category] = 96 / 80 / 64 / 48 / 48 / 32. Decision of
    # 2026-10-03: at the spec's values (scale 1.0, delta 0.10) the ratings are compressed
    # and under-confident. Reverting = elo_k_scale 1.0 and season_reversion_delta 0.10.
    elo_k_scale: float = 2.0
    # MoV multiplier for wins: lambda = 1 + alpha * (grade_winner - grade_loser -
    # baseline_diff), clamped to [mov_lambda_min, mov_lambda_max]; NULL grade -> 1.
    mov_alpha: float = 1.0
    mov_baseline_diff: float = 1.36
    mov_lambda_min: float = 0.5
    mov_lambda_max: float = 2.0
    # Mean reversion before the first festival on/after 1 <season_start_month>.
    season_start_month: int = 4
    season_reversion_delta: float = 0.05  # spec: ~0.10; see elo_k_scale
    season_reversion_mean: float = 1500.0
    # Provisional: no bout for more than this many seasons, or fewer rated career
    # bouts than provisional_min_bouts.
    provisional_inactive_seasons: float = 1.5
    provisional_min_bouts: int = 24
    # Seasons (calendar years) before this one are rated but not ranked (burn-in) and do
    # not count for peak ratings: the first season of the data (from_year = 2004), in
    # which everybody starts at 1500. Phase 10 (2026-10-04): one season settles the
    # *order* (a start one season later gives 19 of the same top 20 from its second
    # season on); the *level* of the scale needs about five seasons and then widens
    # again from 2012 (Regional festivals) until about 2016 - a caveat, not a burn-in.
    elo_first_ranked_season: int = 2005
    # Weight (0..1, multiplies K) of the bouts flagged `unlisted_opponent`: on the sheets
    # before 2011 the opponent of a printed athlete is not printed himself (he did not
    # finish the festival), so the bout is known from one side. 1.0 = like any other bout
    # (outcome only, lambda = 1), 0 = not rated. Phase 10: full weight - every bout
    # between a printed and an unprinted athlete is on the sheet (only bouts among the
    # unprinted are missing), and cutting recent complete sheets the same way shows that
    # full weight reproduces the complete-data ratings best (RMSE 27 points against 45 /
    # 60 / 65 at weight 0.5 / 0.25 / 0).
    elo_one_sided_weight: float = 1.0
    # Identity uncertainty (Phase 3): identity_map rows with confidence <= this value
    # are "low confidence"; an athlete is marked uncertain with >= min_rows such rows
    # or >= min_share of his rows.
    identity_low_confidence: float = 0.4
    identity_uncertain_min_rows: int = 10
    identity_uncertain_min_share: float = 0.25
    k_factors: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_K_FACTORS))

    # --- Publication (what the site shows; defaults of 2026-10-03, the owner may overrule) --
    # Athletes who are not certainly this old at the data date (the date of the last rated
    # festival) are not published by name: they count in the ratings but get no profile, no
    # search entry and no rank, and a festival lists them without name. Only the birth year
    # is known, so the rule is `data year - birth year > publish_min_age` (reached the age
    # before 1 January of the data year). 0 publishes everyone.
    publish_min_age: int = 18
    # Athletes without a known birth year are withheld as well when their first season lies
    # within the last N seasons of the data year (first season > data year - N): they may be
    # minors. N = 3 follows from the debut age: 97.7 % of the debutants since 2016 with a
    # known birth year were at least 16 in their first season (70 % exactly 16), and an
    # athlete who debuts at 16 in season S is not certainly 18 before data year S + 3. All
    # athletes withheld by birth year debuted within these three seasons, and 78 % of the
    # debutants of these seasons with a known birth year are withheld. Chosen for
    # publish_min_age = 18; raise it together with the age (N = age - 16 + 1). 0 switches
    # the rule off (unknown birth years are published); ignored when publish_min_age is 0.
    publish_unknown_recent_seasons: int = 3
    # `<meta name="robots" content="noindex">` on every page plus a robots.txt.
    site_noindex: bool = True
    # Non-public route for corrections and objections, shown on the about page beside the
    # GitHub issues link. Empty = nothing is shown. No address is invented here.
    contact_email: str = ""

    # --- Deploy guard (`check-site`) -----------------------------------------
    # A build may be this much smaller than the last accepted one (meta.json counts) before
    # `check-site` fails. Festivals and bouts only grow (small corrections aside); athletes
    # shrink a little when identities are merged; the number of ranked athletes moves with
    # the season: the inactive rule is applied at the data date, and when a new season
    # starts everyone who stopped after the previous one leaves the ranking at once (18 %
    # of the ranked had no bout for more than 300 days at the end of season 2026).
    guard_max_drop: float = 0.02
    guard_max_drop_ranked: float = 0.25
    # The other direction guards the age filter: a site that suddenly publishes more
    # athletes, or withholds fewer, than the last accepted one has most likely lost its
    # birth years (the reviewer's case: schlussgang stops exposing birthdays -> 718 withheld
    # became 87, published athletes +10 %, ranked +29 %, and the old guard passed).
    # Measured on weekly cuts of the real history 2022-2026: published athletes rise by at
    # most 0.2 % a week (0.5 % in four weeks), published ranked athletes by at most 2.5 % a
    # week (7 % in four weeks), the number of withheld athletes never falls within a data
    # year, and the share of rated athletes with a known birth year never falls by more
    # than 0.02 points. When the data year advances by one, the cohort that becomes
    # certainly old enough is released: the guard allows exactly the number the baseline
    # announced (meta.publish.release_next_year), so January needs no override.
    guard_max_rise: float = 0.03
    guard_max_rise_ranked: float = 0.10
    guard_max_drop_withheld: float = 0.02
    # Inputs of the filter: share of rated athletes with a known birth year (absolute
    # fall, 0.02 = two percentage points) and number of portraits with a birthday in the
    # database (relative fall).
    guard_max_drop_birth_known: float = 0.02
    # `crawl` / `all` refuse to run without the state of earlier runs (festivals in the db,
    # cached responses): a cold start would re-request the whole archive. Set in CI.
    require_state: bool = False

    # --- Serve ---------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000

    # Derived paths ---------------------------------------------------------
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "schwingen.db"


def _coerce(value: str, target: Any) -> Any:
    """Convert an env-var string to the type of the field's default value."""
    if isinstance(target, bool):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError(f"not a boolean: {value!r}")
    if isinstance(target, int):
        return int(value)
    if isinstance(target, float):
        return float(value)
    if isinstance(target, Path):
        return Path(value).expanduser()
    return value


def load_config(
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Build a Config: defaults, then ``SCHWINGEN_*`` env vars, then ``overrides``.

    ``None`` values in ``overrides`` are ignored so unset CLI flags fall through.
    In sample mode ``data_dir`` defaults to :data:`SAMPLE_DATA_DIR`.
    Non-scalar fields (``k_factors``) are not overridable from the environment.
    """
    env = os.environ if env is None else env
    defaults = Config()
    values: dict[str, Any] = {}
    for f in dataclasses.fields(Config):
        default = getattr(defaults, f.name)
        if not isinstance(default, (bool, int, float, str, Path)):
            continue
        raw = env.get(ENV_PREFIX + f.name.upper())
        if raw is not None:
            try:
                values[f.name] = _coerce(raw, default)
            except ValueError as exc:
                raise ValueError(f"invalid {ENV_PREFIX}{f.name.upper()}: {exc}") from exc
    for key, val in (overrides or {}).items():
        if val is None:
            continue
        if key not in {f.name for f in dataclasses.fields(Config)}:
            raise KeyError(f"unknown config key: {key}")
        values[key] = Path(val) if isinstance(getattr(defaults, key), Path) else val
    if values.get("sample") and "data_dir" not in values:
        values["data_dir"] = SAMPLE_DATA_DIR
    if values.get("sample") and "provisional_min_bouts" not in values:
        values["provisional_min_bouts"] = SAMPLE_PROVISIONAL_MIN_BOUTS
    if values.get("sample") and "elo_first_ranked_season" not in values:
        values["elo_first_ranked_season"] = SAMPLE_FIRST_RANKED_SEASON
    if values.get("sample") and "publish_unknown_recent_seasons" not in values:
        values["publish_unknown_recent_seasons"] = SAMPLE_PUBLISH_UNKNOWN_RECENT_SEASONS
    cfg = Config(**values)
    if cfg.from_year > cfg.to_year:
        raise ValueError(f"from_year {cfg.from_year} > to_year {cfg.to_year}")
    if not MIN_REQUEST_DELAY <= cfg.request_delay_min <= cfg.request_delay_max:
        raise ValueError(f"require {MIN_REQUEST_DELAY} <= request_delay_min <= "
                         f"request_delay_max (politeness floor)")
    if not MIN_REQUEST_DELAY <= cfg.backfill_delay_min <= cfg.backfill_delay_max:
        raise ValueError(f"require {MIN_REQUEST_DELAY} <= backfill_delay_min <= "
                         f"backfill_delay_max (politeness floor)")
    if cfg.backfill_delay_min < cfg.request_delay_min:
        raise ValueError("backfill_delay_min must not be below request_delay_min "
                         "(the backfill is the slower crawl)")
    if cfg.backfill_max_requests < 0 or cfg.backfill_max_errors < 1:
        raise ValueError("backfill_max_requests must be >= 0, backfill_max_errors >= 1")
    if cfg.backfill and cfg.refresh:
        raise ValueError("--backfill and --refresh are mutually exclusive "
                         "(a backfill never fetches a file twice)")
    if cfg.offline and cfg.refresh:
        raise ValueError("--offline and --refresh are mutually exclusive")
    if cfg.listing_final_grace_days < 0 or cfg.listing_max_age_hours <= 0:
        raise ValueError("listing_final_grace_days must be >= 0 and "
                         "listing_max_age_hours > 0")
    if cfg.pdf_max_requests < 0 or cfg.pdf_max_age_hours <= 0 or cfg.pdf_final_grace_days < 0:
        raise ValueError("pdf_max_requests/pdf_final_grace_days must be >= 0, "
                         "pdf_max_age_hours > 0")
    from src.db import EVENT_FLAGS
    unknown = set(filter(None, (x.strip() for x in cfg.elo_exclude_flags.split(",")))) \
        - set(EVENT_FLAGS)
    if unknown:
        raise ValueError(f"elo_exclude_flags: unknown flags {sorted(unknown)} "
                         f"(allowed: {', '.join(EVENT_FLAGS)})")
    if not 0 <= cfg.parse_min_pair_rate <= 1:
        raise ValueError("parse_min_pair_rate must be within 0..1")
    if cfg.retry_after_max <= 0:
        raise ValueError("retry_after_max must be > 0")
    from src.pipeline.elo_engine import EloParams
    EloParams.from_config(cfg)  # validates the ELO parameters (raises ValueError)
    if cfg.provisional_min_bouts < 0 or cfg.provisional_inactive_seasons <= 0:
        raise ValueError("provisional_min_bouts must be >= 0 and "
                         "provisional_inactive_seasons > 0")
    if not 0 <= cfg.publish_min_age <= 120:
        raise ValueError("publish_min_age must be within 0..120")
    if not 0 <= cfg.publish_unknown_recent_seasons <= 50:
        raise ValueError("publish_unknown_recent_seasons must be within 0..50")
    if cfg.contact_email and not CONTACT_EMAIL_RE.fullmatch(cfg.contact_email):
        raise ValueError(f"contact_email {cfg.contact_email!r} is not a plain e-mail address")
    if not (0 <= cfg.guard_max_drop < 1 and 0 <= cfg.guard_max_drop_ranked < 1):
        raise ValueError("guard_max_drop / guard_max_drop_ranked must be within 0..1")
    for name in ("guard_max_rise", "guard_max_rise_ranked", "guard_max_drop_withheld",
                 "guard_max_drop_birth_known"):
        if not 0 <= getattr(cfg, name) < 1:
            raise ValueError(f"{name} must be within 0..1")
    if not 1 <= cfg.port <= 65535:
        raise ValueError(f"port {cfg.port} out of range 1-65535")
    return cfg
