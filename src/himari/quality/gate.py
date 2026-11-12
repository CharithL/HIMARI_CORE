"""Data-quality gate (§6): per-instrument OK/FAULT, applied as §8.1 gate 1."""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from himari.core.types import (
    Bar,
    CorporateAction,
    Currency,
    GateLog,
    Instrument,
    SeriesObservation,
)
from himari.gates.invariants import check_cut_only
from himari.quality.checks import (
    CheckResult,
    Severity,
    check_actions_reconcile,
    check_cash_rate,
    check_cross_source,
    check_fresh,
    check_fx,
    check_fx_bridges,
    check_jumps,
    check_ohlc,
    check_order,
    check_zero_volume,
)

if TYPE_CHECKING:
    from himari.core.config import DataQualityConfig
    from himari.data.calendars import BocBusinessDays


class InstrumentQuality(BaseModel):
    symbol: str
    status: Literal["OK", "FAULT"]
    results: list[CheckResult]

    def count(self, check: str, severity: Severity) -> int:
        return sum(1 for r in self.results if r.check == check and r.severity == severity)

    @property
    def faults(self) -> list[CheckResult]:
        return [r for r in self.results if r.severity == Severity.FAULT]


class QualityReport(BaseModel):
    instruments: dict[str, InstrumentQuality]
    portfolio: list[CheckResult]  # Q7 for USD cash valuation, Q8 for CORRA


@dataclass(frozen=True)
class QualityInputs:
    instruments: Sequence[Instrument]
    bars: Mapping[str, Sequence[Bar]]  # primary source, as-of view
    actions: Mapping[str, Sequence[CorporateAction]]
    crosscheck: Mapping[str, Sequence[Bar]]
    fx: Sequence[SeriesObservation]
    corra: Sequence[SeriesObservation]
    last_sessions: Mapping[str, date]  # each instrument's last completed session
    master_session: date  # last completed XTSE session
    as_of_date: date
    boc_days: BocBusinessDays
    fx_series: str  # FX outage confirmations are keyed by this name
    confirmed: Mapping[str, Set[date]] = field(default_factory=dict)


def run_quality_gate(inp: QualityInputs, cfg: DataQualityConfig) -> QualityReport:
    rate = check_cash_rate(
        inp.corra, inp.master_session, inp.boc_days, cfg.rate_max_age_business_days
    )
    usd_cash = check_fx("USD_CASH", inp.fx, inp.master_session, cfg.fx_max_age_calendar_days)
    instruments: dict[str, InstrumentQuality] = {}
    for inst in inp.instruments:
        sym = inst.symbol
        bars = inp.bars.get(sym, [])
        actions = inp.actions.get(sym, [])
        results = [
            *check_fresh(sym, bars, inp.last_sessions[sym]),
            *check_ohlc(sym, bars),
            *check_order(sym, bars, inp.as_of_date),
            *check_jumps(
                sym, bars, actions, inp.confirmed.get(sym, set()), cfg.max_jump_without_action
            ),
            *check_actions_reconcile(sym, bars, actions, cfg.action_reconcile_tol),
            *check_cross_source(sym, bars, inp.crosscheck.get(sym, []), cfg.cross_source_tol),
            *check_zero_volume(sym, bars),
            *(r.model_copy(update={"symbol": sym}) for r in rate),
        ]
        if inst.currency == Currency.USD:
            results += check_fx(sym, inp.fx, inp.last_sessions[sym], cfg.fx_max_age_calendar_days)
            results += check_fx_bridges(
                sym,
                [b.session_date for b in bars],
                inp.fx,
                cfg.fx_max_age_calendar_days,
                cfg.fx_bridge_max_calendar_days,
                inp.confirmed.get(inp.fx_series, set()),
                inp.fx_series,
            )
        status: Literal["OK", "FAULT"] = (
            "FAULT" if any(r.severity == Severity.FAULT for r in results) else "OK"
        )
        instruments[sym] = InstrumentQuality(symbol=sym, status=status, results=results)
    return QualityReport(instruments=instruments, portfolio=[*usd_cash, *rate])


def _no_additions(target: float, held: float) -> float:
    """Exposure in the target's direction may not exceed what is held now."""
    if target > 0:
        return min(target, max(held, 0.0))
    if target < 0:
        return max(target, min(held, 0.0))
    return 0.0


def apply_data_gate(
    weights_in: Mapping[str, float],
    holdings: Mapping[str, float],
    report: QualityReport,
) -> tuple[dict[str, float], GateLog]:
    """Gate 1 (§8.1): a FAULT (or unchecked) instrument may not add; cuts always pass."""
    out: dict[str, float] = {}
    details: dict[str, str] = {}
    for sym, w in weights_in.items():
        quality = report.instruments.get(sym)
        if quality is not None and quality.status == "OK":
            out[sym] = w
            continue
        out[sym] = _no_additions(w, holdings.get(sym, 0.0))
        if out[sym] != w:
            details[sym] = f"data fault: target {w} capped at {out[sym]}"
    check_cut_only("data", weights_in, out)
    log = GateLog(
        gate_name="data",
        weights_in=dict(weights_in),
        weights_out=out,
        bound=bool(details),
        details=details,
    )
    return out, log
