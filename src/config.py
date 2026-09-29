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

# Festival category -> K-factor (spec §4.2.3).
DEFAULT_K_FACTORS: dict[str, float] = {
    "ESAF": 48.0,
    "Bergkranz": 40.0,  # incl. Unspunnen, Kilchberg, Brünig, Rigi, Stoos, ...
    "Teilverband": 32.0,
    "Kantonal": 24.0,
    "Gauverband": 16.0,
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

    # --- Crawl range -------------------------------------------------------
    from_year: int = 2011
    to_year: int = _dt.date.today().year

    # --- Politeness (spec §4.1) ---------------------------------------------
    request_delay_min: float = 0.5
    request_delay_max: float = 1.0
    request_timeout: float = 30.0
    max_retries: int = 4
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
    if not 0 < cfg.request_delay_min <= cfg.request_delay_max:
        raise ValueError("require 0 < request_delay_min <= request_delay_max")
    if not 1 <= cfg.port <= 65535:
        raise ValueError(f"port {cfg.port} out of range 1-65535")
    return cfg
