"""Backtest engine: the event loop of §10.3 running the weekly cycle of §9.1.

One tick per session close of any traded exchange (XTSE, XNAS), at the daily job time
(18:30 ET), in this order:

1. The broker runs the session: interest, corporate actions, payments, conversions, then the
   fills of orders planned before today (rule 2: decide after the close of t, execute at t+1).
2. The ledger books the broker's fills, conversions and interest, then the corporate actions
   that became known since the last tick (point-in-time data), then pays what is due.
3. NAV at the Bank of Canada valuation rate; reconciliation after any activity (§9.5); the
   drawdown state (§8.6); an alert for a distribution unpaid 30 days after its ex-date.
4. The daily safety check (§8.3) on the current holdings.
5. On a weekly decision session: the quality gate, signals, risk and targets (§7), the gates
   (§8), the band and the post-trade limit check (§9.2, v1.6), and an order plan for each
   fund's next session (§9.3). Every order carries its reason and the gates that cut it.

Strategy code only sees `PointInTimeData`; the broker only sees `MarketTruth`.
"""

from __future__ import annotations

import bisect
import dataclasses
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

import numpy as np
from pydantic import BaseModel

from himari.accounting.indices import cash_index
from himari.accounting.ledger import ConversionRecord, Ledger, Valuation
from himari.core.clock import SimClock
from himari.core.ids import decision_id, safety_id
from himari.core.types import (
    AssetClass,
    CorporateAction,
    Currency,
    DrawdownState,
    Exchange,
    Instrument,
    Order,
)
from himari.data.calendars import (
    last_completed_session,
    previous_session,
    release_utc,
    session_close_utc,
    sessions,
    weekly_decision_sessions,
)
from himari.execution.band import apply_band, forced_cuts
from himari.execution.orders import USD_SURPLUS, PlannedOrder, build_orders, usd_surplus
from himari.execution.post_trade import RiskLimits, plan_trades
from himari.execution.reconcile import reconcile
from himari.execution.sleeve import expected_cad_after, plan_sleeve
from himari.gates.ceiling import daily_safety_check
from himari.gates.events import event_window, events_in_window, missing_coverage
from himari.gates.pipeline import GateContext, run_gates
from himari.quality.gate import QualityInputs, QualityReport, run_quality_gate
from himari.risk.covariance import covariance
from himari.risk.state_machine import DrawdownTracker
from himari.sim.broker import FillDetail, SimBroker, Unfilled
from himari.strategy.stage1 import Stage1Decision, stage1_decision

if TYPE_CHECKING:
    from himari.core.config import FrozenConfig
    from himari.core.types import GateLog
    from himari.execution.costs import CostModel
    from himari.gates.events import Event
    from himari.risk.state_machine import DrawdownStatus, SafetyStore
    from himari.sim.market import MarketTruth, PointInTimeData

FloatArray = np.ndarray[Any, np.dtype[np.float64]]
LOG_DECIMALS = 10  # floats in the run log; keeps the log stable across platforms
RECEIVABLE_ALERT_DAYS = 30  # v1.6: alert when a distribution is unpaid this long after ex-date
# §12.3 benchmarks (M5), run through the same engine, ledger and cash handling as the baseline
ALL_SIGNALS_ON = "all_signals_on"  # benchmark 2: every signal on, everything else unchanged
EQUAL_WEIGHT_TIMING = "equal_weight_timing"  # benchmark 3: 1/N x signal, no volatility scaling
BENCHMARKS = ("", ALL_SIGNALS_ON, EQUAL_WEIGHT_TIMING)
# benchmark 3 keeps only the data and fault gates (and no daily safety or post-trade check)
UNSCALED_SKIPS = frozenset({"vol_ceiling", "event", "stress", "drawdown", "final_ceiling"})


def jsonable(obj: Any) -> Any:
    """A JSON-ready copy with dates as ISO text, Decimals as text and floats rounded."""
    if isinstance(obj, bool) or obj is None or isinstance(obj, str | int):
        return obj
    if isinstance(obj, float | np.floating):
        x = float(obj)
        return round(x, LOG_DECIMALS) if math.isfinite(x) else None
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return jsonable(obj.value)
    if isinstance(obj, BaseModel):
        return jsonable(obj.model_dump())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return jsonable({f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)})
    if isinstance(obj, Mapping):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, Sequence | set | frozenset | np.ndarray):
        items = sorted(obj) if isinstance(obj, set | frozenset) else list(obj)
        return [jsonable(v) for v in items]
    raise TypeError(f"cannot log {type(obj).__name__}")


def dumps(record: Mapping[str, Any]) -> str:
    return json.dumps(jsonable(record), sort_keys=True, separators=(",", ":"))


def gate_scales(logs: Sequence[GateLog]) -> dict[str, list[tuple[str, float]]]:
    """Fund -> the gates that cut its weight, each with the scale it applied (out / in)."""
    out: dict[str, list[tuple[str, float]]] = {}
    for log in logs:
        for sym, w_out in log.weights_out.items():
            w_in = log.weights_in.get(sym, 0.0)
            if w_in > 0 and w_out < w_in:
                out.setdefault(sym, []).append((log.gate_name, w_out / w_in))
    return out


@dataclass(frozen=True)
class BacktestSettings:
    start: date
    end: date
    initial_cad: Decimal
    cost_multiplier: float
    fill_at: str = "close"  # "open" is the sensitivity variant, never used for pass or fail
    label: str = ""  # e.g. "cash at the broker" for the sensitivity run (v1.7)
    benchmark: str = ""  # "" for the strategy, else a §12.3 benchmark (M5)
    delay_sessions: int = 0  # robustness pack: every fill this many sessions later (M5)

    @property
    def run_name(self) -> str:
        """Folder and column name: 1x, 2x, 1x-broker-cash, ..."""
        tag = f"{self.cost_multiplier:g}x"
        return f"{tag}-{self.label.replace(' ', '-')}" if self.label else tag


@dataclass(frozen=True)
class NavPoint:
    day: date
    nav: float
    cash: dict[str, float]
    receivables_cad: float
    usd_to_cad: float
    drawdown: float
    hwm: float
    state: str
    weights: dict[str, float]  # risk positions only; the cash sleeve is cash (v1.7)
    accrued_interest_cad: float = 0.0
    sleeve_weight: float = 0.0


@dataclass
class BacktestResult:
    settings: BacktestSettings
    universe: tuple[str, ...]
    nav: list[NavPoint]
    cash_level: list[float]  # the cash index on the same days (the benchmark), NaN without data
    ledger: Ledger
    records: list[dict[str, Any]]
    fills: list[tuple[date, FillDetail]]
    unfilled: list[Unfilled]
    missing_events: dict[str, list[int]]
    alerts: list[str] = field(default_factory=list)
    sleeve: str | None = None  # the cash sleeve fund, when the run used one (v1.7)

    def log_lines(self) -> list[str]:
        return [dumps(r) for r in self.records]


class Backtest:
    def __init__(
        self,
        cfg: FrozenConfig,
        data: PointInTimeData,
        truth: MarketTruth,
        costs: CostModel,
        events: Sequence[Event],
        settings: BacktestSettings,
        safety: SafetyStore,
    ) -> None:
        if costs.multiplier != settings.cost_multiplier:
            raise ValueError("the cost model and the settings disagree on the cost multiplier")
        if settings.benchmark not in BENCHMARKS:
            raise ValueError(f"unknown benchmark {settings.benchmark!r}")
        self.unscaled = settings.benchmark == EQUAL_WEIGHT_TIMING
        self.cfg = cfg
        self.data = data
        self.costs = costs
        self.events = list(events)
        self.settings = settings
        self.safety = safety
        self.tracker = DrawdownTracker(safety, cfg.drawdown)
        self.limits = RiskLimits.from_config(cfg)
        if self.unscaled:  # benchmark 3: the post-trade check never binds
            self.limits = dataclasses.replace(
                self.limits, vol_ceiling=math.inf, single_limit=math.inf, joint_limit=math.inf
            )
        self.clock = SimClock(settings.start)
        self.eod = time.fromisoformat(cfg.schedule.daily_job_after_et)
        self.master = cfg.calendars.master
        instruments = cfg.stage1.instruments()
        self.instruments = instruments
        self.universe = tuple(i.symbol for i in instruments)
        # the cash sleeve (§7.4, v1.7): traded and valued like a fund, never a risk position
        self.sleeve_cfg = cfg.cash_sleeve
        self.sleeve: str | None = cfg.cash_sleeve.symbol if cfg.cash_sleeve.active else None
        traded = list(instruments)
        if self.sleeve is not None:
            if self.sleeve not in costs.half_spread_bps:
                raise ValueError(
                    f"no half spread for the sleeve {self.sleeve}: measure it and add it to "
                    "costs_v1.yaml (the sleeve gets no cheaper treatment)"
                )
            traded.append(
                Instrument.model_validate(
                    {
                        "symbol": self.sleeve,
                        "exchange": Exchange.TSX,
                        "currency": Currency.CAD,
                        "class": AssetClass.CASH,
                    }
                )
            )
        self.sleeve_instruments = traded
        self.calendars = {i.symbol: cfg.calendars.by_exchange[i.exchange.value] for i in traded}
        self.currencies = {i.symbol: i.currency.value for i in traded}
        us_listed = frozenset(i.symbol for i in instruments if i.exchange == Exchange.NASDAQ)
        withholding = Decimal(repr(costs.us_withholding))
        self.ledger = Ledger(
            cash={"CAD": settings.initial_cad, "USD": Decimal(0)},
            withholding_rate=withholding,
            us_listed=us_listed,
        )
        self._event_times: dict[str, list[datetime]] = {}
        for e in self.events:
            for sym in cfg.events.map.get(e.type, ()):
                self._event_times.setdefault(sym, []).append(e.release_utc)
        for times in self._event_times.values():
            times.sort()
        self.broker = SimBroker(
            truth=truth,
            costs=costs,
            calendars=self.calendars,
            currencies=self.currencies,
            exchanges={i.symbol: i.exchange.value for i in traded},
            us_listed=us_listed,
            withholding_rate=withholding,
            participation_cap=costs.participation_cap,
            carry_sessions=costs.carry_forward_sessions,
            funding_buffer=cfg.rebalance.fx_funding_buffer,
            fx_max_age_days=cfg.data_quality.fx_max_age_calendar_days,
            cash={"CAD": settings.initial_cad},
            stressed=self.stressed,
            fill_at=settings.fill_at,
            last_buys=frozenset({self.sleeve}) if self.sleeve else frozenset(),
            delay_sessions=settings.delay_sessions,
        )
        self.records: list[dict[str, Any]] = []
        self.nav: list[NavPoint] = []
        self.fills: list[tuple[date, FillDetail]] = []
        self.unfilled: list[Unfilled] = []
        self.alerts: list[str] = []
        self._planned: dict[str, PlannedOrder] = {}
        self._booked_actions: dict[tuple[str, date, str], CorporateAction] = {}
        self._overdue_alerted: set[tuple[str, date]] = set()
        self._last_tick = datetime(1970, 1, 1, tzinfo=UTC)
        self._decision_state = DrawdownState.NORMAL  # the state at the last weekly decision
        self.weekly: set[date] = set()
        self.grid: list[date] = []

    # ---- the loop ----------------------------------------------------------------------
    def run(self) -> BacktestResult:
        s = self.settings
        codes = {self.master, *self.calendars.values()}
        days = sorted({d for code in codes for d in sessions(code, s.start, s.end)})
        self.weekly = set(weekly_decision_sessions(self.master, s.start, s.end))
        final = self.data.epoch(release_utc(s.end, self.eod))
        firsts = [idx.dates[0] for idx in final.indices.values() if idx.dates]
        self.grid = sessions(self.master, min(firsts), s.end) if firsts else []
        for day in days:
            self._tick(day)
        years = range(s.start.year, s.end.year + 1)
        missing = missing_coverage(self.events, self.cfg.events.map.keys(), years)
        run_days = [p.day for p in self.nav]
        bench = cash_index(
            run_days,
            final.corra,
            self.data.boc_days,
            self.cfg.data_quality.rate_max_age_business_days,
            cutoffs=[release_utc(d, self.eod) for d in run_days],
        )
        return BacktestResult(
            settings=s,
            universe=self.universe,
            nav=self.nav,
            cash_level=[float(v) for v in bench.level],
            ledger=self.ledger,
            records=self.records,
            fills=self.fills,
            unfilled=self.unfilled,
            missing_events=missing,
            alerts=self.alerts,
            sleeve=self.sleeve,
        )

    def _tick(self, day: date) -> None:
        now = release_utc(day, self.eod)
        self.clock.advance_to(day, now)
        rep = self.broker.run_session(day)
        for det in rep.fills:
            planned = self._planned.get(det.fill.order_id)
            reason = planned.reason if planned else ""
            gates = planned.gates_text() if planned else ""
            self.ledger.book_fill(day, det.fill, det.currency, det.decision_id, reason, gates)
            self.fills.append((day, det))
        for fx in rep.conversions:
            self.ledger.book_conversion(
                ConversionRecord(
                    day,
                    fx.purpose,
                    fx.sell_currency,
                    fx.sell_amount,
                    fx.buy_currency,
                    fx.buy_amount,
                    fx.execution_rate,
                    fx.valuation_rate,
                    fx.cost_cad,
                )
            )
        for posting in rep.interest:
            self.ledger.book_interest(posting)
        self.ledger.set_accrued_interest(rep.accrued)
        self.unfilled.extend(rep.unfilled)
        booked = self._book_actions(day, now)
        paid = self.ledger.collect(day)
        val = self._value(day, now)
        self.broker.mark_nav(val.nav, val.usd_to_cad)
        session: dict[str, Any] = {}
        if rep.activity or booked or paid or rep.unfilled or rep.waiting:
            session = {
                "kind": "session",
                "date": day,
                "fills": [self._fill_record(d) for d in rep.fills],
                "conversions": rep.conversions,
                "interest": rep.interest,
                "unfilled": rep.unfilled,
                "waiting": rep.waiting,
                "splits": rep.splits,
                "broker_paid": rep.paid,
                "ledger_booked": booked,
                "ledger_paid": [(r.symbol, r.net, r.currency) for r in paid],
            }
        if rep.activity or booked or paid:
            rec = reconcile(
                {s: p.shares for s, p in self.broker.positions().items()},
                self.broker.cash(),
                self.ledger.positions,
                self.ledger.cash,
                val.usd_to_cad,
            )
            session["reconciliation"] = rec.describe()
            if not rec.ok:
                self._alert(day, f"reconciliation {rec.describe()}")
                if not self.safety.venue_fault_active():
                    self.safety.set_venue_fault(rec.describe(), "system", now)
        if session:
            session["nav"] = val.nav
            self.records.append(session)
        for r in self.ledger.overdue(day, RECEIVABLE_ALERT_DAYS):
            if (r.symbol, r.ex_date) not in self._overdue_alerted:
                self._overdue_alerted.add((r.symbol, r.ex_date))
                self._alert(
                    day,
                    f"{r.symbol} distribution (ex-date {r.ex_date}, net {r.net} {r.currency}) "
                    f"unpaid {RECEIVABLE_ALERT_DAYS} days after its ex-date",
                )
        status = self.tracker.update(val.nav, now, weekly_decision=day in self.weekly)
        self.nav.append(
            NavPoint(
                day,
                val.nav,
                val.cash,
                val.receivables_cad,
                val.usd_to_cad,
                status.drawdown,
                status.hwm or val.nav,
                status.state.value,
                self._risk_weights(val),
                val.accrued_interest_cad,
                val.weights().get(self.sleeve, 0.0) if self.sleeve else 0.0,
            )
        )
        risk = self._risk(day) if (day in self.weekly or self.ledger.positions) else None
        safety_cuts: set[str] = set()
        if risk is not None and self.ledger.positions and not self.unscaled:
            safety_cuts = self._daily_safety(day, now, val, risk)
        if day in self.weekly and risk is not None:
            self._weekly(day, now, val, risk, status, safety_cuts)
        self.ledger.close_day(day)
        self._last_tick = now

    # ---- valuation, risk and events ----------------------------------------------------
    def _value(self, day: date, now: datetime) -> Valuation:
        rate = self.data.usd_to_cad(day, now)
        if rate is None:
            raise RuntimeError(f"{day}: no Bank of Canada USD/CAD rate is known; cannot value")
        prices = {}
        for sym in self.ledger.positions:
            close = self.data.last_close(sym, day, now)
            if close is None:
                raise RuntimeError(f"{day}: {sym} is held but has no known close")
            prices[sym] = close[1]
        return self.ledger.value(prices, self.currencies, rate[1])

    def _prices(self, day: date, now: datetime) -> dict[str, float]:
        out = {}
        for sym in self.universe:
            close = self.data.last_close(sym, day, now)
            if close is not None:
                out[sym] = close[1]
        return out

    def _risk(self, day: date) -> Stage1Decision:
        ep = self.data.epoch(release_utc(day, self.eod))
        grid = self.grid[: bisect.bisect_right(self.grid, day)]
        return stage1_decision(self.cfg, day, ep.indices, ep.cash, grid)

    def _targets(self, risk: Stage1Decision) -> dict[str, float]:
        """Pre-gate targets: the strategy's (§7.3), or a §12.3 benchmark's."""
        if self.settings.benchmark == ALL_SIGNALS_ON:  # the all-on weights a, before signals
            sz = risk.sizing
            return {s: float(a) for s, a in zip(sz.symbols, sz.after_cash, strict=True)}
        if self.unscaled:  # equal weights 1/N for each eligible fund whose signal is on
            n = len(self.universe)
            return {
                s: 1.0 / n if s in risk.eligible and risk.funds[s].signal.on else 0.0
                for s in self.universe
            }
        return risk.weights()

    def _cov(self, risk: Stage1Decision) -> FloatArray:
        sigmas = [risk.funds[s].sigma_floored or 0.0 for s in self.universe]
        return covariance(sigmas, risk.corr)

    def stressed(self, symbol: str, day: date) -> bool:
        """Execution inside an event window for the fund: spreads x the stress multiplier."""
        times = self._event_times.get(symbol)
        if not times:
            return False
        code = self.calendars[symbol]
        start = session_close_utc(code, previous_session(code, day))
        end = session_close_utc(code, day)
        i = bisect.bisect_right(times, start)
        return i < len(times) and times[i] <= end

    # ---- corporate actions in the ledger -----------------------------------------------
    def _book_actions(self, day: date, now: datetime) -> list[dict[str, Any]]:
        booked: list[dict[str, Any]] = []
        for a in self.data.new_actions(self._last_tick, now):
            if a.ex_date < self.settings.start:
                continue
            key = (a.symbol, a.ex_date, a.action_type)
            previous = self._booked_actions.get(key)
            self._booked_actions[key] = a
            currency = self.currencies.get(a.symbol, "CAD")
            if a.action_type == "split":
                if previous is not None:
                    self._alert(day, f"{a.symbol} split {a.ex_date} was corrected; not re-booked")
                    continue
                close = self.data.last_close(a.symbol, a.ex_date, now)
                if self.ledger.held_before(a.symbol, a.ex_date) == 0 or close is None:
                    continue
                rec = self.ledger.book_split(a, day, close[1], currency)
                booked.append({"split": rec})
            elif a.action_type == "reinvested":
                if previous is None and (r := self.ledger.book_reinvested(a, day)) is not None:
                    booked.append({"reinvested": r})
            else:
                change = (
                    a
                    if previous is None
                    else a.model_copy(update={"amount": a.amount - previous.amount})
                )
                if change.amount == 0:
                    continue
                dist = self.ledger.book_distribution(change, day, currency)
                if dist is not None:
                    booked.append({"distribution": dist, "correction": previous is not None})
        return booked

    # ---- the daily safety check (§8.3) -------------------------------------------------
    def _daily_safety(
        self, day: date, now: datetime, val: Valuation, risk: Stage1Decision
    ) -> set[str]:
        held = self._risk_weights(val)
        ceiling = self.cfg.risk.portfolio_vol_ceiling
        check = daily_safety_check(
            held, self.universe, self._cov(risk), ceiling, self.cfg.risk.daily_safety_multiple
        )
        if not check.triggered:
            return set()
        cuts = {s: w for s, w in check.targets.items() if w < held.get(s, 0.0)}
        record: dict[str, Any] = {"kind": "safety", "date": day, "vol": check.vol, "targets": cuts}
        if self.safety.kill_active():
            record["blocked"] = "KILL: no orders"
            self.records.append(record)
            return set()
        did = safety_id(day)
        for o in self.broker.open_orders():
            if o.symbol in cuts:
                self.broker.cancel(o.order_id)
        scale = ceiling / check.vol
        orders, skipped = build_orders(
            did,
            cuts,
            self.ledger.positions,
            self._prices(day, now),
            {"CAD": 1.0, "USD": val.usd_to_cad},
            self.currencies,
            val.nav,
            self.cfg.rebalance.min_order_cad,
            reasons=dict.fromkeys(cuts, "safety_cut"),
            gates={s: [("daily_safety", scale)] for s in cuts},
        )
        record["orders"] = self._submit(orders)
        record["skipped"] = skipped
        self.records.append(record)
        return set(cuts)

    # ---- the weekly decision (§9.1) ----------------------------------------------------
    def _weekly(
        self,
        day: date,
        now: datetime,
        val: Valuation,
        risk: Stage1Decision,
        status: DrawdownStatus,
        safety_cuts: set[str],
    ) -> None:
        did = decision_id(day)
        snap = self.data.snapshot(now)
        report = run_quality_gate(
            QualityInputs(
                instruments=self.sleeve_instruments,
                bars=snap.bars,
                actions=snap.actions,
                crosscheck=snap.crosscheck,
                fx=snap.fx,
                corra=snap.corra,
                last_sessions={
                    s: last_completed_session(code, now) for s, code in self.calendars.items()
                },
                master_session=last_completed_session(self.master, now),
                as_of_date=day,
                boc_days=self.data.boc_days,
                fx_series=self.cfg.sources.fx_series,
                confirmed=self.data.confirmed,
                confirmed_actions=self.data.confirmed_actions,
            ),
            self.cfg.data_quality,
        )
        targets = self._targets(risk)
        held = self._risk_weights(val)
        cov = self._cov(risk)
        start, ends = event_window(day, self.master, self.calendars)
        pre_event = events_in_window(self.events, self.cfg.events.map, start, ends)
        ctx = GateContext(
            quality=report,
            symbols=self.universe,
            covariance=cov,
            vol_ceiling=self.cfg.risk.portfolio_vol_ceiling,
            pre_event=pre_event,
            stress=self.cfg.stress,
            drawdown_state=status.state,
            halve_multiplier=self.cfg.drawdown.halve_multiplier,
            venue_fault=self.safety.venue_fault_active(),
            kill=self.safety.kill_active(),
        )
        gates = run_gates(targets, held, ctx, UNSCALED_SKIPS if self.unscaled else frozenset())
        entering_halved = (
            status.state == DrawdownState.HALVED and self._decision_state != DrawdownState.HALVED
        )
        self._decision_state = status.state
        superseded = [o.order_id for o in self.broker.open_orders()]
        for oid in superseded:
            self.broker.cancel(oid)
        record: dict[str, Any] = {
            "kind": "decision",
            "decision_id": did,
            "date": day,
            "as_of_utc": now,
            "nav": val.nav,
            "drawdown": {
                "state": status.state.value,
                "drawdown": status.drawdown,
                "hwm": status.hwm,
                "entering_halved": entering_halved,
            },
            "funds": {
                s: {
                    "momentum": f.signal.momentum,
                    "on": f.signal.on,
                    "eligible": s in risk.eligible,
                    "sigma": f.sigma,
                    "sigma_floored": f.sigma_floored,
                    "quality": report.instruments[s].status,
                    "held": held.get(s, 0.0),
                    "target": targets[s],
                    "after_gates": gates.weights[s],
                }
                for s, f in risk.funds.items()
            },
            "sizing": {"scale": risk.sizing.scale, "bound": risk.sizing.bound},
            "gates": [
                {
                    "gate": g.gate_name,
                    "bound": g.bound,
                    "scale": g.scale_applied,
                    "details": g.details,
                    "weights_in": g.weights_in,  # M5: gate statistics need entry and exit
                    "weights_out": g.weights_out,
                }
                for g in gates.logs
            ],
            "pre_event": {
                s: [f"{e.type} {e.release_utc.isoformat()}" for e in es]
                for s, es in pre_event.items()
            },
            "superseded": superseded,
        }
        if gates.orders_blocked:
            record["blocked"] = "KILL: no orders, cuts included"
            self.records.append(record)
            return
        forced = dict.fromkeys(safety_cuts, "safety_cut")
        if entering_halved:
            for s in forced_cuts(gates.logs, gates.weights, held, only=("drawdown",)):
                forced.setdefault(s, "halved")
        band = apply_band(
            gates.weights, held, self.cfg.rebalance.band_abs, self.cfg.rebalance.band_rel, forced
        )
        plan = plan_trades(
            held, gates.weights, band, self.universe, cov, self.cfg.stress, self.limits
        )
        scales = gate_scales(gates.logs)
        if plan.buy_scale < 1.0:
            for s in plan.reasons:
                if plan.weights[s] > held.get(s, 0.0):
                    scales.setdefault(s, []).append(("post_trade_buys", plan.buy_scale))
        for s, b in band.items():
            fund = record["funds"].setdefault(s, {})
            fund["band"] = b.reason
            fund["post_trade"] = plan.weights.get(s, held.get(s, 0.0))
        record["post_trade"] = {
            "banded": plan.banded,
            "final": plan.final,
            "buy_scale": plan.buy_scale,
            "fallback": plan.fallback,
        }
        fx_to_cad = {"CAD": 1.0, "USD": val.usd_to_cad}
        orders, skipped = build_orders(
            did,
            {s: plan.weights[s] for s in plan.reasons},
            self.ledger.positions,
            self._prices(day, now),
            fx_to_cad,
            self.currencies,
            val.nav,
            self.cfg.rebalance.min_order_cad,
            reasons=plan.reasons,
            gates=scales,
        )
        record["orders"] = self._submit(orders)
        record["skipped"] = skipped
        reserved = sum(
            o.shares * o.reference_price * (1.0 + self.cfg.rebalance.fx_funding_buffer)
            for o in orders
            if o.side == "buy" and o.currency == "USD"
        )
        surplus = usd_surplus(
            val.cash.get("USD", 0.0),
            reserved,
            val.usd_to_cad,
            val.nav,
            self.cfg.rebalance.usd_surplus_convert_above_nav,
        )
        if surplus > 0:
            self.broker.queue_conversion(USD_SURPLUS, "USD", "CAD", Decimal(repr(surplus)))
            record["fx"] = {"purpose": USD_SURPLUS, "usd": surplus}
        if self.sleeve is not None:
            record["sleeve"] = self._sleeve_order(did, day, now, val, orders, surplus, report)
        self.records.append(record)

    def _sleeve_order(
        self,
        did: str,
        day: date,
        now: datetime,
        val: Valuation,
        orders: list[PlannedOrder],
        surplus: float,
        report: QualityReport,
    ) -> dict[str, Any]:
        """The weekly sleeve rule (§7.4, v1.7), after the risk order plan."""
        assert self.sleeve is not None
        sym = self.sleeve
        close = self.data.last_close(sym, day, now)
        out: dict[str, Any] = {"symbol": sym}
        if close is None:
            out["note"] = "before the fund's inception: cash stays at the broker"
            return out
        if self.safety.kill_active() or self.safety.venue_fault_active():
            out["note"] = "KILL or VENUE_FAULT: no sleeve orders"
            return out
        price = close[1]
        expected = expected_cad_after(
            orders,
            val.cash.get("CAD", 0.0),
            val.cash.get("USD", 0.0),
            val.usd_to_cad,
            self.costs,
            self.cfg.rebalance.fx_funding_buffer,
            surplus,
        )
        quality = report.instruments.get(sym)
        plan = plan_sleeve(
            expected,
            val.nav,
            self.sleeve_cfg,
            price,
            self.costs.side_bps(sym),
            lambda n, p: self.costs.commission("CAD", n, p),
            self.ledger.positions.get(sym, 0),
            buys_allowed=quality is not None and quality.status == "OK",
        )
        out.update(
            {
                "expected_cad": plan.expected_cad,
                "buffer": plan.buffer,
                "band": plan.band,
                "note": plan.note,
                "weight": val.weights().get(sym, 0.0),
                "stress_loss_50bp": self._sleeve_stress(val),
            }
        )
        if plan.side is not None and plan.shares > 0:
            order = PlannedOrder(
                decision_id=did,
                symbol=sym,
                side=plan.side,
                shares=plan.shares,
                currency="CAD",
                reference_price=price,
                reference_price_cad=price,
                reason=plan.reason,
            )
            out["orders"] = self._submit([order])
        return out

    def _sleeve_stress(self, val: Valuation) -> float | None:
        """Diagnostic only: the sleeve's loss, as a share of NAV, under the +50 bp shock."""
        d = self.sleeve_cfg.duration_years
        if d is None or self.sleeve is None:
            return None
        shock = (self.cfg.stress.factors["CA_YIELD"].shock_bp or 0.0) / 10_000
        return val.weights().get(self.sleeve, 0.0) * d * shock

    def _risk_weights(self, val: Valuation) -> dict[str, float]:
        """Weights of the risk positions; the cash sleeve counts as cash (rule 5)."""
        return {s: w for s, w in val.weights().items() if s in self.universe}

    # ---- helpers -----------------------------------------------------------------------
    def _submit(self, orders: list[PlannedOrder]) -> list[dict[str, Any]]:
        acks = self.broker.submit(
            [
                Order(
                    decision_id=o.decision_id,
                    symbol=o.symbol,
                    side=o.side,
                    shares=o.shares,
                    order_type="close",
                    idempotency_key=o.idempotency_key,
                )
                for o in orders
            ]
        )
        out = []
        for o, ack in zip(orders, acks, strict=True):
            self._planned.setdefault(o.idempotency_key, o)
            out.append({"order": o, "status": ack.status})
            if ack.status != "submitted":
                self._alert(self.clock.current_date, f"order {o.idempotency_key} {ack.status}")
        return out

    def _fill_record(self, d: FillDetail) -> dict[str, Any]:
        f = d.fill
        planned = self._planned.get(f.order_id)
        return {
            "order_id": f.order_id,
            "symbol": f.symbol,
            "side": f.side,
            "shares": f.shares,
            "price": f.price,
            "market_price": d.market_price,
            "side_bps": d.side_bps,
            "commission": f.commission,
            "currency": d.currency,
            "stressed": d.stressed,
            "capped": d.capped,
            "cash_limited": d.cash_limited,
            "reason": planned.reason if planned else "",
        }

    def _alert(self, day: date, message: str) -> None:
        self.alerts.append(f"{day.isoformat()} {message}")
        self.records.append({"kind": "alert", "date": day, "message": message})
