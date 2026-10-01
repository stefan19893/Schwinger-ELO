"""Central configuration: paths, year range, rate limits and ELO parameters.

Precedence: CLI flag > environment variable (``SCHWINGEN_<FIELD>``) > defaults here.
Every scalar field of :class:`Config` can be overridden via the environment, e.g.
``SCHWINGEN_FROM_YEAR=2015`` or ``SCHWINGEN_DATA_DIR=/tmp/data``.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

ENV_PREFIX = "SCHWINGEN_"

REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# --sample runs write here unless a data dir is given explicitly, so they never
# touch the real SQLite db / Parquet files in data/.
SAMPLE_DATA_DIR: Path = REPO_ROOT / "data" / "sample"

# Politeness floor (spec §4.1): no configuration may go below this delay.
MIN_REQUEST_DELAY = 0.5

# Festival category -> K-factor (spec §4.2.3, user decisions 2026-09-29).
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

    # --- Crawl range -------------------------------------------------------
    from_year: int = 2011
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
    # MoV multiplier: lambda = 1 + alpha * (grade_a - grade_b - baseline_diff).
    # Unspecified in the spec -> neutral defaults until calibrated in Phase 4.
    mov_alpha: float = 0.0
    mov_baseline_diff: float = 0.0
    season_start_month: int = 4
    season_reversion_delta: float = 0.10
    season_reversion_mean: float = 1500.0
    provisional_inactive_seasons: float = 1.5
    k_factors: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_K_FACTORS))

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
    cfg = Config(**values)
    if cfg.from_year > cfg.to_year:
        raise ValueError(f"from_year {cfg.from_year} > to_year {cfg.to_year}")
    if not MIN_REQUEST_DELAY <= cfg.request_delay_min <= cfg.request_delay_max:
        raise ValueError(f"require {MIN_REQUEST_DELAY} <= request_delay_min <= "
                         f"request_delay_max (politeness floor)")
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
    if not 1 <= cfg.port <= 65535:
        raise ValueError(f"port {cfg.port} out of range 1-65535")
    return cfg
