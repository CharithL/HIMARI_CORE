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
from himari.research.trials import Trial, TrialLedger
from himari.risk.state_machine import SafetyStore
from himari.sim.engine import Backtest, BacktestResult, BacktestSettings
from himari.sim.market import MarketTruth, PointInTimeData
from himari.sim.report import html_report, summarize, write_run

if TYPE_CHECKING:
    from himari.core.config import FrozenConfig
    from himari.data.calendars import HolidayFile

DEMO_START, DEMO_END = date(2024, 1, 2), date(2025, 12, 31)
DEMO_OBSERVED = datetime(2026, 1, 5, tzinfo=UTC)


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
    symbols = [i.symbol for i in cfg.stage1.instruments()]
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


def run_backtests(
    cfg: FrozenConfig,
    project_root: Path,
    store: VersionedStore,
    plan: RunPlan,
    work_dir: Path,
    forward: HolidayFile | None = None,
    confirmations: ConfirmationStore | None = None,
) -> list[BacktestResult]:
    """One backtest per cost multiplier; each gets its own safety store under work_dir."""
    confirmed = confirmations.confirmed() if confirmations else None
    confirmed_actions = confirmations.confirmed("Q10") if confirmations else None
    data = PointInTimeData(
        store, cfg, plan.primary, plan.crosscheck, forward, confirmed, confirmed_actions
    )
    truth = MarketTruth(
        store, plan.primary, SeriesNames(cfg.sources.fx_series, cfg.sources.cash_rate_series)
    )
    events = load_events(project_root / "config" / "events", cfg.events.map.keys())
    results = []
    for m in plan.multipliers:
        costs = load_cost_model(project_root / "config" / "costs_v1.yaml", m, plan.commissions)
        safety_path = work_dir / f"{m:g}x" / "safety.sqlite"
        if safety_path.exists():
            raise FileExistsError(f"{safety_path} exists; each run needs a fresh safety store")
        settings = BacktestSettings(plan.start, plan.end, plan.initial_cad, m, plan.fill_at)
        results.append(
            Backtest(cfg, data, truth, costs, events, settings, SafetyStore(safety_path)).run()
        )
    return results


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
        write_run(r, out_dir / f"{r.settings.cost_multiplier:g}x")
    report = out_dir / "report.html"
    report.write_text(
        html_report(results, title, data_label, commission_label, notices),
        encoding="utf-8",
        newline="\n",
    )
    return report


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
                notes=notes,
            )
        )
        ids.append(tid)
    return ids
