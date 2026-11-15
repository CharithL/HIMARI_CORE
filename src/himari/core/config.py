"""Config loader, validation, and SHA-256 hashing.

Every run records the config hash. Frozen parameters must never be edited
to improve a result — a change means a new config version.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from himari.core.types import AssetClass, Currency, Exchange, Instrument


class CalendarConfig(BaseModel):
    master: str
    by_exchange: dict[str, str]


class ExecutionConfig(BaseModel):
    session_offset: int = 1
    window_et: list[str]
    cancel_unfilled_et: str
    max_retry_sessions: int = 2
    buy_limit: dict[str, float]
    sell_limit: dict[str, float]


class ScheduleConfig(BaseModel):
    daily_job_after_et: str
    weekly_decision: str
    execution: ExecutionConfig


class CapGroupConfig(BaseModel):
    name: str
    members: list[str]
    max: float = Field(alias="max")

    model_config = {"populate_by_name": True}


class CapsConfig(BaseModel):
    per_instrument: float
    groups: list[CapGroupConfig]


class Stage1Config(BaseModel):
    long_only: bool = True
    max_gross: float = 1.0
    min_history_sessions: int = 252
    universe: list[dict[str, str]]
    class_budget: dict[str, float]
    within_class_split: str = "equal"
    caps: CapsConfig

    def instruments(self) -> list[Instrument]:
        result = []
        for u in self.universe:
            result.append(
                Instrument(
                    symbol=u["symbol"],
                    exchange=Exchange(u["exchange"]),
                    currency=Currency(u["currency"]),
                    **{"class": AssetClass(u["class"])},
                )
            )
        return result

    def budget_for(self, instrument: Instrument) -> float:
        class_budget = self.class_budget[instrument.asset_class.value]
        n_in_class = sum(1 for u in self.universe if u["class"] == instrument.asset_class.value)
        return class_budget / n_in_class


class SignalConfig(BaseModel):
    type: str
    lookback_sessions: int = 252
    zero_is_off: bool = True


class RiskConfig(BaseModel):
    ewma_vol_half_life: int = 20
    ewma_corr_half_life: int = 60
    corr_shrink_to_identity: float = 0.50
    init_sessions: int = 252
    annualization: int = 252
    vol_floor: dict[str, float]
    portfolio_vol_ceiling: float = 0.08
    daily_safety_multiple: float = 1.25

    def vol_decay(self) -> float:
        return float(0.5 ** (1.0 / self.ewma_vol_half_life))

    def corr_decay(self) -> float:
        return float(0.5 ** (1.0 / self.ewma_corr_half_life))


class RebalanceConfig(BaseModel):
    band_abs: float = 0.005
    band_rel: float = 0.10
    min_order_cad: float = 100
    usd_surplus_convert_above_nav: float = 0.02
    fx_funding_buffer: float = 0.01
    gate_tolerance_rel: float = 0.10  # v1.6: post-trade sigma_p and stress may exceed by this


class EventsConfig(BaseModel):
    window: str = "decision_close_to_execution_end"
    rule: str = "no_additions"
    map: dict[str, list[str]] = Field(default_factory=dict)


class StressFactor(BaseModel):
    shock: float | None = None  # return move, e.g. 0.07 for +/-7%
    shock_bp: float | None = None  # yield move in basis points


class BondLoading(BaseModel):
    duration: float
    convexity: float


class StressConfig(BaseModel):
    factors: dict[str, StressFactor] = Field(default_factory=dict)
    loadings: dict[str, dict[str, float | BondLoading]] = Field(default_factory=dict)
    single_factor_limit: float = 0.03
    joint_scenario: dict[str, float] = Field(default_factory=dict)
    joint_limit: float = 0.05
    diagnostic_all_adverse_including_fx: bool = True


class CashSleeveConfig(BaseModel):
    """Idle CAD cash in one CAD-listed T-bill or money-market ETF (§7.4, v1.7). Cash
    management only: no signal, no risk budget, not part of w, not gated, left out of sigma_p
    and the stress gate."""

    enabled: bool = False
    symbol: str | None = None  # null keeps idle cash at the broker
    buffer_nav: float = 0.02
    buffer_min_cad: float = 200.0
    band_nav: float = 0.03
    band_min_cad: float = 1000.0
    duration_years: float | None = None  # the fund's duration, for the +50 bp diagnostic

    @property
    def active(self) -> bool:
        return self.enabled and self.symbol is not None

    def buffer(self, nav: float) -> float:
        return max(self.buffer_nav * nav, self.buffer_min_cad)

    def band(self, nav: float) -> float:
        return max(self.band_nav * nav, self.band_min_cad)


class AccountConfig(BaseModel):
    loss_floor_cad: float | None = None  # null allowed in backtest and paper; required live
    initial_nav_cad: float | None = None  # v1.6: planned account size; sets the interest tiers


class DrawdownConfig(BaseModel):
    halve_at: float = -0.10
    suspend_at: float = -0.15
    halve_multiplier: float = 0.5
    resume: str = "manual_with_reason"


class DataQualityConfig(BaseModel):
    max_jump_without_action: float = 0.15
    action_reconcile_tol: float = 0.001
    cross_source_tol: float = 0.005
    fx_max_age_calendar_days: int = 3
    rate_max_age_business_days: int = 5
    fx_bridge_max_calendar_days: int = 7


class SourcesConfig(BaseModel):
    boc_valet_base: str
    fx_series: str
    cash_rate_series: str


class ResearchWindowsConfig(BaseModel):
    development_end: date
    evaluation: list[date]
    replication: list[date]


class BootstrapConfig(BaseModel):
    type: str = "stationary"
    mean_block: int = 20
    resamples: int = 10000


class ResearchConfig(BaseModel):
    seed: int = 20261111
    bootstrap: BootstrapConfig
    risk_aversion: float = 3.0
    es_level: float = 0.975
    holm_alpha: float = 0.05
    early_decision_p: float = 0.005
    dsr_min: float = 0.95
    dsr_sensitivity_n: list[int] = Field(default_factory=lambda: [10, 100, 1000, 10000])
    legacy_trials_estimate: int | None = None
    windows: ResearchWindowsConfig


class PromotionResearchConfig(BaseModel):
    min_common_history_years: int = 5
    min_oos_annual_blocks: int = 3
    require_positive_excess_return_at_2x: bool = True
    require_positive_cer_at_2x: bool = True


class PromotionOperationalConfig(BaseModel):
    min_calendar_days: int = 90
    min_sessions: int = 63
    min_weekly_decisions: int = 13
    reconciliation_pass_rate: float = 1.0
    max_stale_input_additions: int = 0
    min_job_completion: float = 0.99


class PromotionConfig(BaseModel):
    research: PromotionResearchConfig
    operational: PromotionOperationalConfig
    live_ramp_fraction: float = 0.25


class FrozenConfig(BaseModel):
    """Top-level frozen configuration. Validated and hashed."""

    version: str
    created: str

    @field_validator("created", mode="before")
    @classmethod
    def _coerce_created(cls, v: Any) -> str:
        return str(v) if isinstance(v, date) else v

    base_currency: str = "CAD"
    display_timezone: str = "America/Toronto"
    calendars: CalendarConfig
    schedule: ScheduleConfig
    stage1: Stage1Config
    signal: SignalConfig
    risk: RiskConfig
    rebalance: RebalanceConfig
    account: AccountConfig = Field(default_factory=AccountConfig)
    cash_sleeve: CashSleeveConfig = Field(default_factory=CashSleeveConfig)
    drawdown: DrawdownConfig
    data_quality: DataQualityConfig
    sources: SourcesConfig
    research: ResearchConfig
    promotion: PromotionConfig

    events: EventsConfig = Field(default_factory=EventsConfig)
    stress: StressConfig = Field(default_factory=StressConfig)
    crypto_comparator: dict[str, Any] = Field(default_factory=dict)  # M9

    model_config = {"extra": "forbid"}


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def load_config(path: Path) -> FrozenConfig:
    raw = load_yaml(path)
    return FrozenConfig(**raw)


def config_hash(path: Path) -> str:
    """SHA-256 hash of the config file's canonical JSON representation.

    Uses sorted keys and consistent formatting so the hash is stable
    across loads (YAML key order doesn't matter).
    """
    raw = load_yaml(path)
    canonical = json.dumps(raw, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def tracked_py_files(src_dir: Path) -> list[str]:
    """Git-tracked .py files under src_dir, as sorted POSIX paths relative to it."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=src_dir,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"code_hash needs a git checkout: {src_dir}") from exc
    return sorted(p for p in out.decode("utf-8").split("\0") if p)


def code_hash(src_dir: Path) -> str:
    """SHA-256 over the git-tracked .py files under src_dir.

    Untracked files are excluded so a local checkout and CI hash the same set.
    CRLF is normalized to LF so Windows and Linux agree on content.
    """
    h = hashlib.sha256()
    for rel in tracked_py_files(src_dir):
        content = (src_dir / rel).read_bytes().replace(b"\r\n", b"\n")
        h.update(f"{rel}\0{len(content)}\0".encode())
        h.update(content)
    return h.hexdigest()


def run_hashes(project_root: Path) -> dict[str, str]:
    """Return hashes for every file that defines a run's identity.

    Every run record must include these so any parameter or code change
    is traceable in the audit trail.
    """
    files = {
        "config": project_root / "config" / "frozen_v1.yaml",
        "costs": project_root / "config" / "costs_v1.yaml",
        "register": project_root / "research" / "register.yaml",
    }
    hashes = {name: config_hash(path) for name, path in files.items()}
    hashes["code"] = code_hash(project_root / "src" / "himari")
    return hashes
