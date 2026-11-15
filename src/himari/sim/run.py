"""Running backtests: one run per cost multiplier on shared point-in-time data, the outputs,
and a trial-ledger row per run (rule 6: every run is appended, demos included)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from himari.core.ids import trial_id
from himari.data.ingest import SeriesNames, ingest_actions, ingest_bars, ingest_series_source
from himari.data.sources import CsvSource
from himari.data.storage import ACTIONS, BARS, SERIES, VersionedStore
from himari.execution.costs import load_cost_model
from himari.gates.events import load_events
from himari.quality.confirmations import ConfirmationStore
from himari.research.metrics import excess_returns, measured_series
from himari.research.trials import Trial, TrialLedger
from himari.risk.state_machine import SafetyStore
from himari.sim.engine import (
    ALL_SIGNALS_ON,
    EQUAL_WEIGHT_TIMING,
    Backtest,
    BacktestResult,
    BacktestSettings,
)
from himari.sim.market import MarketTruth, PointInTimeData
from himari.sim.report import html_report, summarize, write_run

if TYPE_CHECKING:
    from himari.core.config import FrozenConfig
    from himari.data.calendars import HolidayFile

DEMO_START, DEMO_END = date(2024, 1, 2), date(2025, 12, 31)
DEMO_OBSERVED = datetime(2026, 1, 5, tzinfo=UTC)
# v1.7: the fixture set's SYNTHETIC cash-sleeve fund; its half spread and duration are invented
# for the demo (the 10 bp per-side floor applies whatever the half spread)
DEMO_SLEEVE, DEMO_SLEEVE_HALF_SPREAD_BPS, DEMO_SLEEVE_DURATION = "TBILL.SYN", 1.0, 0.15
BROKER_CASH = "cash at the broker"  # the sensitivity label when the sleeve is on
# M5: the §12.3 benchmarks and the robustness pack's delay variant, as run labels
BENCHMARK_LABELS = {ALL_SIGNALS_ON: "all signals on", EQUAL_WEIGHT_TIMING: "equal-weight timing"}
DELAYED = "fills one session later"


def demo_config(cfg: FrozenConfig) -> FrozenConfig:
    """The frozen config with the fixture set's synthetic sleeve fund in place of the real one
    (CBIL from v1.8, which the fixture set does not have)."""
    sleeve = cfg.cash_sleeve.model_copy(
        update={"symbol": DEMO_SLEEVE, "duration_years": DEMO_SLEEVE_DURATION}
    )
    return cfg.model_copy(update={"cash_sleeve": sleeve})


def broker_cash_config(cfg: FrozenConfig) -> FrozenConfig:
    """The same config with the cash sleeve off: idle cash stays at the broker."""
    off = cfg.cash_sleeve.model_copy(update={"enabled": False})
    return cfg.model_copy(update={"cash_sleeve": off})


def fixture_root(project_root: Path) -> Path:
    return project_root / "tests" / "fixtures" / "market"


def demo_confirmations(project_root: Path) -> ConfirmationStore:
    """The synthetic fixture set's Q10 confirmations (read only)."""
    return ConfirmationStore(fixture_root(project_root) / "confirmations.jsonl")


def initial_nav(cfg: FrozenConfig, override: Decimal | None = None) -> Decimal:
    """Starting cash: an explicit amount, else account.initial_nav_cad (v1.6)."""
    if override is not None:
        return override
    if cfg.account.initial_nav_cad is None:
        raise ValueError("set account.initial_nav_cad in the config or pass --initial-cad")
    return Decimal(repr(cfg.account.initial_nav_cad))


def ingest_demo_fixtures(
    cfg: FrozenConfig, project_root: Path, root: Path, market: Path | None = None
) -> VersionedStore:
    """The committed synthetic fixture set (or a copy at `market`) in a fresh store, with
    sources `csv` and `xcheck`."""
    market = market or fixture_root(project_root)
    if not market.exists():
        raise FileNotFoundError(f"fixture data not found at {market}; run from the repository")
    store = VersionedStore(root)
    symbols = [i.symbol for i in cfg.stage1.instruments()] + [DEMO_SLEEVE]
    eod = time.fromisoformat(cfg.schedule.daily_job_after_et)
    span = (date(2024, 1, 1), DEMO_END)
    for folder, name in (("primary", "csv"), ("crosscheck", "xcheck")):
        src = CsvSource(market / folder, name=name)
        ingest_bars(src, store, symbols, *span, DEMO_OBSERVED, eod)
        ingest_actions(src, store, symbols, *span, DEMO_OBSERVED, eod)
    names = SeriesNames(cfg.sources.fx_series, cfg.sources.cash_rate_series)
    ingest_series_source(
        CsvSource(market / "primary"), store, names, None, *span, DEMO_OBSERVED, eod
    )
    return store


def data_version(store: VersionedStore) -> str:
    """sha256 of the stored tables (all versions)."""
    h = hashlib.sha256()
    for spec in (BARS, ACTIONS, SERIES):
        path = store.root / f"{spec.name}.parquet"
        h.update(spec.name.encode())
        h.update(path.read_bytes() if path.exists() else b"")
    return h.hexdigest()


def git_commit(project_root: Path) -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head + ("-dirty" if dirty else "")


@dataclass(frozen=True)
class RunPlan:
    start: date
    end: date
    initial_cad: Decimal
    multipliers: Sequence[float]
    commissions: Path
    primary: str = "csv"
    crosscheck: str | None = None
    fill_at: str = "close"
    sleeve_half_spread_bps: float | None = None  # only for a sleeve not in costs_v1.yaml (demo)


@dataclass(frozen=True)
class Variant:
    cfg: FrozenConfig
    label: str = ""
    benchmark: str = ""
    delay_sessions: int = 0


class _Runner:
    """Shared point-in-time data and market for a set of runs; one fresh safety store each."""

    def __init__(
        self,
        cfg: FrozenConfig,
        project_root: Path,
        store: VersionedStore,
        plan: RunPlan,
        work_dir: Path,
        forward: HolidayFile | None,
        confirmations: ConfirmationStore | None,
    ) -> None:
        confirmed = confirmations.confirmed() if confirmations else None
        confirmed_actions = confirmations.confirmed("Q10") if confirmations else None
        self.data = PointInTimeData(
            store, cfg, plan.primary, plan.crosscheck, forward, confirmed, confirmed_actions
        )
        names = SeriesNames(cfg.sources.fx_series, cfg.sources.cash_rate_series)
        self.truth = MarketTruth(store, plan.primary, names)
        self.events = load_events(project_root / "config" / "events", cfg.events.map.keys())
        self.project_root, self.plan, self.work_dir = project_root, plan, work_dir

    def run(self, v: Variant, m: float) -> BacktestResult:
        plan = self.plan
        costs = load_cost_model(self.project_root / "config" / "costs_v1.yaml", m, plan.commissions)
        sleeve = v.cfg.cash_sleeve.symbol if v.cfg.cash_sleeve.active else None
        if sleeve is not None and plan.sleeve_half_spread_bps is not None:
            costs = costs.with_symbol(sleeve, plan.sleeve_half_spread_bps)
        settings = BacktestSettings(
            plan.start, plan.end, plan.initial_cad, m, plan.fill_at, v.label, v.benchmark,
            v.delay_sessions,
        )  # fmt: skip
        safety_path = self.work_dir / settings.run_name / "safety.sqlite"
        if safety_path.exists():
            raise FileExistsError(f"{safety_path} exists; each run needs a fresh safety store")
        bt = Backtest(
            v.cfg, self.data, self.truth, costs, self.events, settings, SafetyStore(safety_path)
        )
        return bt.run()


def run_backtests(
    cfg: FrozenConfig,
    project_root: Path,
    store: VersionedStore,
    plan: RunPlan,
    work_dir: Path,
    forward: HolidayFile | None = None,
    confirmations: ConfirmationStore | None = None,
) -> list[BacktestResult]:
    """One backtest per cost multiplier; each gets its own safety store under work_dir.

    With the cash sleeve on, the sleeve runs are the baseline of record and the same plan runs
    again with idle cash at the broker, labelled as a sensitivity (v1.7)."""
    runner = _Runner(cfg, project_root, store, plan, work_dir, forward, confirmations)
    variants = [Variant(cfg)]
    if cfg.cash_sleeve.active:
        variants.append(Variant(broker_cash_config(cfg), BROKER_CASH))
    return [runner.run(v, m) for v in variants for m in plan.multipliers]


@dataclass(frozen=True)
class ResearchRuns:
    """The M5 research set: the baseline of record (one run per multiplier), the cash-at-the-
    broker sensitivity, the §12.3 benchmarks 2 and 3, and the delay variant (§12.3, v1.7)."""

    baseline: list[BacktestResult]
    broker_cash: list[BacktestResult]
    benchmarks: dict[str, list[BacktestResult]]  # label -> one run per multiplier
    delayed: list[BacktestResult]

    def all(self) -> list[BacktestResult]:
        out = [*self.baseline, *self.broker_cash]
        for runs in self.benchmarks.values():
            out.extend(runs)
        return [*out, *self.delayed]


def run_research_pack(
    cfg: FrozenConfig,
    project_root: Path,
    store: VersionedStore,
    plan: RunPlan,
    work_dir: Path,
    forward: HolidayFile | None = None,
    confirmations: ConfirmationStore | None = None,
) -> ResearchRuns:
    runner = _Runner(cfg, project_root, store, plan, work_dir, forward, confirmations)
    ms = plan.multipliers
    broker = (
        [runner.run(Variant(broker_cash_config(cfg), BROKER_CASH), m) for m in ms]
        if cfg.cash_sleeve.active
        else []
    )
    return ResearchRuns(
        baseline=[runner.run(Variant(cfg), m) for m in ms],
        broker_cash=broker,
        benchmarks={
            label: [runner.run(Variant(cfg, label, bench), m) for m in ms]
            for bench, label in BENCHMARK_LABELS.items()
        },
        delayed=[runner.run(Variant(cfg, DELAYED, delay_sessions=1), m) for m in ms],
    )


def write_outputs(
    results: Sequence[BacktestResult],
    out_dir: Path,
    title: str,
    data_label: str,
    commission_label: str,
    notices: Sequence[str] = (),
) -> Path:
    """Per-run files under out_dir/<m>x/ and one HTML report for all runs."""
    for r in results:
        write_run(r, out_dir / r.settings.run_name)
    report = out_dir / "report.html"
    report.write_text(
        html_report(results, title, data_label, commission_label, notices),
        encoding="utf-8",
        newline="\n",
    )
    return report


def _role_note(s: BacktestSettings) -> str:
    if s.benchmark:
        return f" | benchmark: {s.label}"
    if s.delay_sessions:
        return f" | robustness: {s.label}"
    return f" | sensitivity: {s.label}" if s.label else ""


def log_trials(
    ledger: TrialLedger,
    results: Sequence[BacktestResult],
    project_root: Path,
    hashes: Mapping[str, str],
    data_ver: str,
    hypothesis_id: str,
    notes: str,
    now_utc: datetime,
) -> list[str]:
    ids = []
    commit = git_commit(project_root)
    for r in results:
        s = summarize(r)
        metrics = {k: v for k, v in asdict(s).items() if k not in ("start", "end", "gate_binds")}
        metrics["metric_start"] = s.metric_start.isoformat()
        metrics["gate_binds"] = {g: list(b) for g, b in s.gate_binds.items()}
        tid = trial_id()
        try:  # M5: the daily excess returns, for PBO; none when the cash index has a gap
            days, nav, cash = measured_series(r)
            returns = list(zip(days[1:], excess_returns(nav, cash).tolist(), strict=True))
        except ValueError:
            returns = []
        ledger.append(
            Trial(
                trial_id=tid,
                hypothesis_id=hypothesis_id,
                utc=now_utc.isoformat(),
                git_commit=commit,
                code_hash=hashes["code"],
                config_hash=hashes["config"],
                data_version=data_ver,
                cost_multiplier=r.settings.cost_multiplier,
                window=f"{r.settings.start.isoformat()}..{r.settings.end.isoformat()}",
                metrics_json=json.dumps(metrics, sort_keys=True),
                p_value=None,
                result="completed",
                notes=notes + _role_note(r.settings),
            ),
            returns,
        )
        ids.append(tid)
    return ids
