"""M4 acceptance on the fixture set (§16): 1x and 2x runs, determinism, no look-ahead, the
amendment v1.6 band and post-trade check, interest on cash, the safety paths, and the v1.7 cash
sleeve (the synthetic TBILL.SYN) with its cash-at-the-broker sensitivity."""

import csv
import hashlib
import shutil
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from himari.core.config import FrozenConfig, load_config
from himari.data.calendars import sessions
from himari.data.ingest import SeriesNames
from himari.data.storage import VersionedStore
from himari.execution.costs import load_cost_model
from himari.gates.events import load_events
from himari.research.attribution import attribute, leave_one_instrument_out
from himari.research.metrics import (
    backtest_metrics,
    excess_returns,
    gate_statistics,
    measured_series,
)
from himari.research.stats import paired_bootstrap, sharpe_rows
from himari.risk.state_machine import SafetyStore
from himari.sim.engine import (
    ALL_SIGNALS_ON,
    EQUAL_WEIGHT_TIMING,
    Backtest,
    BacktestResult,
    BacktestSettings,
)
from himari.sim.market import MarketTruth, PointInTimeData
from himari.sim.report import nav_csv, summarize, trades_csv
from himari.sim.run import (
    BROKER_CASH,
    DEMO_END,
    DEMO_SLEEVE,
    DEMO_SLEEVE_HALF_SPREAD_BPS,
    DEMO_START,
    RunPlan,
    broker_cash_config,
    demo_config,
    demo_confirmations,
    fixture_root,
    ingest_demo_fixtures,
    run_backtests,
)

ROOT = Path(__file__).parents[2]
CFG = load_config(ROOT / "config" / "frozen_v1.yaml")  # the cash sleeve is CBIL (v1.8)
SLEEVE_CFG = demo_config(CFG)  # the baseline of record: idle cash in the synthetic sleeve
BROKER_CFG = broker_cash_config(CFG)  # the sleeve off: idle cash at the broker
COMMISSIONS = ROOT / "tests" / "fixtures" / "commissions_demo.yaml"
NAMES = SeriesNames(CFG.sources.fx_series, CFG.sources.cash_rate_series)
CASH = Decimal("100000")
REASONS = {
    "band", "full_exit", "post_trade_limit", "halved", "safety_cut", "sleeve_funding",
    "sleeve_sweep",
}  # fmt: skip

# sha256 of the 1x run log (decisions.jsonl) on the fixture set. Change only on purpose, with a
# CHANGELOG entry that says why.
GOLDEN_LOG_HASH = "68154cacfd21e0f00300213a5005a6cb477aead670a0559ff8992b4d22c49f65"  # M5


def plan(multipliers: tuple[float, ...], start: date = DEMO_START, end: date = DEMO_END) -> RunPlan:
    return RunPlan(start, end, CASH, multipliers, COMMISSIONS, "csv", "xcheck", "close",
                   DEMO_SLEEVE_HALF_SPREAD_BPS)  # fmt: skip


def log_hash(res: BacktestResult) -> str:
    return hashlib.sha256("".join(line + "\n" for line in res.log_lines()).encode()).hexdigest()


def backtest(
    store: VersionedStore,
    safety: SafetyStore,
    start: date,
    end: date,
    cfg: FrozenConfig = BROKER_CFG,
    benchmark: str = "",
    delay_sessions: int = 0,
) -> Backtest:
    """A single 1x run with a caller-supplied safety store (for the KILL and fault paths)."""
    conf = demo_confirmations(ROOT)
    data = PointInTimeData(store, cfg, "csv", "xcheck", None, conf.confirmed(),
                           conf.confirmed("Q10"))  # fmt: skip
    costs = load_cost_model(ROOT / "config" / "costs_v1.yaml", 1.0, COMMISSIONS)
    if cfg.cash_sleeve.symbol == DEMO_SLEEVE:
        costs = costs.with_symbol(DEMO_SLEEVE, DEMO_SLEEVE_HALF_SPREAD_BPS)
    return Backtest(
        cfg,
        data,
        MarketTruth(store, "csv", NAMES),
        costs,
        load_events(ROOT / "config" / "events", cfg.events.map.keys()),
        BacktestSettings(start, end, CASH, 1.0, benchmark=benchmark, delay_sessions=delay_sessions),
        safety,
    )


def edit_fixtures(tmp: Path, change: object) -> Path:
    """A copy of the fixture set with `change(path, rows)` applied to every bar/action CSV."""
    market = tmp / "market"
    shutil.copytree(fixture_root(ROOT), market)
    for path in sorted(market.rglob("*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if rows and change(path, rows):  # type: ignore[operator]
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
                w.writeheader()
                w.writerows(rows)
    return market


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> VersionedStore:
    return ingest_demo_fixtures(CFG, ROOT, tmp_path_factory.mktemp("store"))


@pytest.fixture(scope="module")
def runs(store: VersionedStore, tmp_path_factory: pytest.TempPathFactory) -> list[BacktestResult]:
    """1x and 2x with the sleeve, then the same with cash at the broker (the sensitivity)."""
    work = tmp_path_factory.mktemp("runs")
    out = run_backtests(SLEEVE_CFG, ROOT, store, plan((1.0, 2.0)), work, None,
                        demo_confirmations(ROOT))  # fmt: skip
    assert [r.settings.run_name for r in out] == [
        "1x", "2x", "1x-cash-at-the-broker", "2x-cash-at-the-broker"
    ]  # fmt: skip
    return out


class TestFixtureRuns:
    def test_ledger_and_broker_reconcile_every_session(self, runs: list[BacktestResult]) -> None:
        for res in runs:
            checks = [r["reconciliation"] for r in res.records if "reconciliation" in r]
            assert checks and set(checks) == {"reconciled"}
            assert res.alerts == []

    def test_costs_at_2x(self, runs: list[BacktestResult]) -> None:
        one, two = summarize(runs[0]), summarize(runs[1])
        assert two.costs_cad > 1.8 * one.costs_cad
        assert two.final_nav < one.final_nav

    def test_idle_cash_earns_interest(self, runs: list[BacktestResult]) -> None:
        """v1.6, with cash at the broker: during the all-cash warm-up NAV rises with the
        accrual; monthly postings."""
        res = runs[2]
        assert res.settings.label == BROKER_CASH and res.sleeve is None
        warm = [p for p in res.nav if p.day < date(2024, 12, 1)]
        assert all(b.nav > a.nav for a, b in zip(warm, warm[1:], strict=False))
        postings = [r for r in res.ledger.interest if r.day.year == 2024]
        assert [r.period for r in postings] == [f"2024-{m:02d}" for m in range(1, 12)]
        assert all(r.day.day <= 7 for r in postings)  # the first session of the next month
        nav_2024 = next(p.nav for p in reversed(res.nav) if p.day.year == 2024)
        assert 101_000 < nav_2024 < 103_500  # about 87,000 x (CORRA - 0.5%) x NAV-scale

    def test_warm_up_is_excluded_from_the_metrics(self, runs: list[BacktestResult]) -> None:
        s = summarize(runs[0])
        assert s.metric_start == date(2025, 1, 3)  # the first decision with a valid signal
        assert s.warmup_sessions > 250 and s.base_nav > float(CASH)
        assert 0.0 < s.avg_gross_exposure < 1.0 and s.time_in_market > 0.9

    def test_gate_trims_follow_the_band(self, runs: list[BacktestResult]) -> None:
        """v1.6, 2025-01-24: VUN switches on and the stress gate scales every target by 0.949;
        the trims of the other holdings are inside the band, so only VUN trades."""
        rec = next(r for r in runs[0].records if r.get("decision_id") == "D-2025-01-24-000")
        (o,) = [x["order"] for x in rec["orders"]]
        assert (o.side, o.symbol, o.reason) == ("buy", "VUN", "band")
        assert [g for g, _ in o.gates] == ["stress"] and o.gates[0][1] == pytest.approx(
            0.949, abs=1e-3
        )
        stress = next(g for g in rec["gates"] if g["gate"] == "stress")
        assert stress["bound"]

    def test_every_order_has_a_reason(self, runs: list[BacktestResult]) -> None:
        for res in runs:
            assert {t.reason for t in res.ledger.trades} <= REASONS
            risk = [t for t in res.ledger.trades if t.symbol != DEMO_SLEEVE]
            assert all(t.gates for t in risk if t.decision_id == "D-2025-01-24-000")

    def test_post_trade_portfolios_fit(self, runs: list[BacktestResult]) -> None:
        for res in runs:
            for r in res.records:
                if r["kind"] == "decision" and "post_trade" in r:
                    assert r["post_trade"]["final"].fits

    def test_no_distribution_left_unpaid(self, runs: list[BacktestResult]) -> None:
        """v1.6: the fixture distributions have pay dates; only late-December ones (paid in
        January) may still be receivable at the end."""
        for res in runs:
            assert all(r.ex_date >= date(2025, 12, 20) for r in res.ledger.receivables())


class TestDeterminism:
    def test_same_run_same_bytes(
        self, store: VersionedStore, runs: list[BacktestResult], tmp_path: Path
    ) -> None:
        again, _ = run_backtests(SLEEVE_CFG, ROOT, store, plan((1.0,)), tmp_path, None,
                                 demo_confirmations(ROOT))  # fmt: skip
        assert again.log_lines() == runs[0].log_lines()
        assert nav_csv(again) == nav_csv(runs[0])
        assert trades_csv(again) == trades_csv(runs[0])

    def test_golden_run_log(self, runs: list[BacktestResult]) -> None:
        assert log_hash(runs[0]) == GOLDEN_LOG_HASH


class TestNoLookAhead:
    CUT = date(2025, 6, 27)

    def test_changing_the_future_changes_nothing_before(
        self, runs: list[BacktestResult], tmp_path: Path
    ) -> None:
        """Every bar after the cut moves 50% and every FX rate 10%; the run up to the cut,
        including its decisions, fills and NAV, is unchanged."""

        def shock(path: Path, rows: list[dict[str, str]]) -> bool:
            key = "session_date" if "bars" in path.parts else "date"
            if "actions" in path.parts or key not in rows[0]:
                return False
            for row in rows:
                if date.fromisoformat(row[key]) > self.CUT:
                    if "bars" in path.parts:
                        for col in ("open", "high", "low", "close", "adj_close"):
                            if row.get(col):
                                row[col] = f"{float(row[col]) * 1.5:.6f}"
                    elif path.stem == "FXUSDCAD":
                        row["value"] = f"{float(row['value']) * 1.1:.4f}"
            return True

        market = edit_fixtures(tmp_path, shock)
        shocked_store = ingest_demo_fixtures(CFG, ROOT, tmp_path / "store", market)
        shocked, _ = run_backtests(SLEEVE_CFG, ROOT, shocked_store, plan((1.0,)),
                                   tmp_path / "run", None, demo_confirmations(ROOT))  # fmt: skip

        def upto(res: BacktestResult) -> list[str]:
            return [line for r, line in zip(res.records, res.log_lines(), strict=True)
                    if r["date"] <= self.CUT]  # fmt: skip

        assert upto(shocked) == upto(runs[0])
        assert [p for p in shocked.nav if p.day <= self.CUT] == [
            p for p in runs[0].nav if p.day <= self.CUT
        ]
        assert any(p.nav != q.nav for p, q in zip(shocked.nav, runs[0].nav, strict=True))


class TestSafetyPaths:
    START, END = date(2024, 12, 2), date(2025, 2, 28)

    def test_kill_blocks_every_order(self, store: VersionedStore, tmp_path: Path) -> None:
        safety = SafetyStore(tmp_path / "safety.sqlite")
        safety.kill("drill", "tester", datetime(2024, 12, 1, tzinfo=timezone.utc))
        res = backtest(store, safety, self.START, self.END).run()
        decisions = [r for r in res.records if r["kind"] == "decision"]
        assert decisions and all(r.get("blocked") for r in decisions)
        assert not res.ledger.trades

    def test_a_reconciliation_mismatch_sets_venue_fault(
        self, store: VersionedStore, tmp_path: Path
    ) -> None:
        """Drop the ledger's conversions: the first USD funding leaves the cash apart."""
        safety = SafetyStore(tmp_path / "safety.sqlite")
        bt = backtest(store, safety, self.START, self.END)
        bt.ledger.book_conversion = lambda rec: None  # type: ignore[method-assign]
        res = bt.run()
        assert any("reconciliation mismatch" in a for a in res.alerts)
        assert safety.venue_fault_active()
        later = [r for r in res.records if r["kind"] == "decision" and r["date"] > date(2025, 1, 6)]
        faults = [next(g for g in r["gates"] if g["gate"] == "faults") for r in later]
        assert faults and all(g["details"]["venue_fault"] for g in faults)

    def test_unpaid_distribution_alert(self, tmp_path: Path) -> None:
        """v1.6: a distribution still receivable 30 days after its ex-date raises an alert."""

        def no_pay_date(path: Path, rows: list[dict[str, str]]) -> bool:
            if path.parent.name != "actions" or path.stem != "VCN":
                return False
            for row in rows:
                if row["ex_date"] == "2025-03-25":
                    row["pay_date"] = ""
            return True

        market = edit_fixtures(tmp_path, no_pay_date)
        st = ingest_demo_fixtures(CFG, ROOT, tmp_path / "store", market)
        res = backtest(
            st, SafetyStore(tmp_path / "s.sqlite"), date(2025, 1, 2), date(2025, 5, 30)
        ).run()
        alerts = [a for a in res.alerts if "unpaid 30 days" in a]
        assert len(alerts) == 1 and alerts[0].startswith(
            "2025-04-24 VCN distribution (ex-date 2025-03-25"
        )


class TestResearchMetrics:
    """M5: §12.2 metrics and the §12.4 paired bootstrap on the engine's own fixture runs."""

    def test_metrics_start_after_the_warm_up(self, runs: list[BacktestResult]) -> None:
        days, nav, cash = measured_series(runs[0])
        assert days[0] == summarize(runs[0]).metric_start == date(2025, 1, 3)
        assert len(days) == len(nav) == len(cash)

    def test_2x_costs_lower_the_return_metrics(self, runs: list[BacktestResult]) -> None:
        one, two = (backtest_metrics(r, CFG.research) for r in runs[:2])
        assert two.annual_excess_return < one.annual_excess_return
        assert two.cer < one.cer and two.sharpe < one.sharpe
        assert one.es > 0 and two.es > 0
        assert one.max_drawdown == pytest.approx(summarize(runs[0]).max_drawdown)

    def test_the_paired_bootstrap_runs_on_engine_returns(self, runs: list[BacktestResult]) -> None:
        """2x against 1x on the same sessions: 2x is not an improvement."""
        (_, n1, c1), (_, n2, c2) = measured_series(runs[0]), measured_series(runs[1])
        res = paired_bootstrap(
            excess_returns(n1, c1), excess_returns(n2, c2), sharpe_rows, resamples=500,
            seed=CFG.research.seed,
        )  # fmt: skip
        assert res.observed < 0 and res.p_value > 0.5


class TestCashSleeve:
    """§7.4 (v1.7) on the synthetic sleeve fund, listed from 2 July 2024."""

    def test_used_only_from_the_funds_inception(self, runs: list[BacktestResult]) -> None:
        trades = [t for t in runs[0].ledger.trades if t.symbol == DEMO_SLEEVE]
        first = trades[0]
        assert (first.day, first.side, first.reason) == (date(2024, 7, 8), "buy", "sleeve_sweep")
        before = [
            r for r in runs[0].records if r["kind"] == "decision" and r["date"] < date(2024, 7, 5)
        ]
        assert before and all("inception" in r["sleeve"]["note"] for r in before)

    def test_the_sleeve_is_cash_not_a_risk_position(self, runs: list[BacktestResult]) -> None:
        res = runs[0]
        assert all(set(p.weights) <= set(res.universe) for p in res.nav)
        august = [p.sleeve_weight for p in res.nav if (p.day.year, p.day.month) == (2024, 8)]
        assert min(august) > 0.95  # all but the buffer is swept
        for r in res.records:
            if r["kind"] == "decision":
                assert DEMO_SLEEVE not in r["funds"]
                assert all(DEMO_SLEEVE not in g["details"] for g in r["gates"])

    def test_a_funding_sale_settles_the_same_sessions_buys(
        self, runs: list[BacktestResult]
    ) -> None:
        """2025-01-06: the first risk buys need about 59,000 CAD; the sleeve sale fills first."""
        fills = [det.fill for day, det in runs[0].fills if day == date(2025, 1, 6)]
        assert fills[0].symbol == DEMO_SLEEVE and fills[0].side == "sell"
        assert {f.side for f in fills[1:]} == {"buy"}

    def test_the_synthetic_sleeve_pays_corra_less_its_fee(self) -> None:
        """Fixture check: TBILL.SYN's 2025 payouts per unit, over its 50.00 price, match the
        year's mean fixture CORRA less the 0.10% fee (SLEEVE_PRICE and SLEEVE_FEE in
        make_market_fixtures.py) to within 0.05 percentage points."""
        primary = ROOT / "tests" / "fixtures" / "market" / "primary"
        with open(primary / "actions" / f"{DEMO_SLEEVE}.csv", encoding="utf-8") as f:
            paid = sum(float(r["amount"]) for r in csv.DictReader(f) if r["ex_date"][:4] == "2025")
        with open(primary / "series" / "AVG.INTWO.csv", encoding="utf-8") as f:
            corra = [float(r["value"]) for r in csv.DictReader(f) if r["date"][:4] == "2025"]
        assert paid / 50.0 * 100 == pytest.approx(sum(corra) / len(corra) - 0.10, abs=0.05)

    def test_sleeve_lines_are_reported_apart(self, runs: list[BacktestResult]) -> None:
        s, broker = summarize(runs[0]), summarize(runs[2])
        assert s.sleeve == DEMO_SLEEVE and s.sleeve_trades > 0 and s.sleeve_income_cad > 0
        assert broker.sleeve is None and broker.sleeve_trades == 0
        assert s.trades == broker.trades  # the strategy's trades exclude the sleeve's
        assert s.avg_gross_exposure == pytest.approx(broker.avg_gross_exposure, abs=0.01)

    def test_kill_and_venue_fault_stop_sleeve_orders(
        self, store: VersionedStore, tmp_path: Path
    ) -> None:
        safety = SafetyStore(tmp_path / "kill.sqlite")
        safety.kill("drill", "tester", datetime(2024, 6, 1, tzinfo=timezone.utc))
        res = backtest(store, safety, date(2024, 6, 3), date(2024, 8, 30), SLEEVE_CFG).run()
        assert not res.ledger.trades
        fault = SafetyStore(tmp_path / "fault.sqlite")
        fault.set_venue_fault("drill", "tester", datetime(2024, 6, 1, tzinfo=timezone.utc))
        res = backtest(store, fault, date(2024, 6, 3), date(2024, 8, 30), SLEEVE_CFG).run()
        notes = [r["sleeve"]["note"] for r in res.records if r["kind"] == "decision"]
        assert not res.ledger.trades and any("VENUE_FAULT" in n for n in notes)

    def test_a_sleeve_data_fault_stops_sweeps_only(self, tmp_path: Path) -> None:
        """No cross-check for the sleeve from February 2025: Q6 faults it, so no sweeps; funding
        sales still go through."""

        def drop_crosscheck(path: Path, rows: list[dict[str, str]]) -> bool:
            if path.parent.parent.name != "crosscheck" or path.stem != DEMO_SLEEVE:
                return False
            rows[:] = [r for r in rows if r["session_date"] < "2025-02-01"]
            return True

        market = edit_fixtures(tmp_path, drop_crosscheck)
        st = ingest_demo_fixtures(CFG, ROOT, tmp_path / "store", market)
        res = backtest(st, SafetyStore(tmp_path / "s.sqlite"), date(2024, 6, 3), date(2025, 6, 30),
                       SLEEVE_CFG).run()  # fmt: skip
        late = [  # trades decided after the cross-check stopped
            t
            for t in res.ledger.trades
            if t.symbol == DEMO_SLEEVE and t.decision_id > "D-2025-02-01"
        ]
        assert late and {t.side for t in late} == {"sell"}
        notes = [r["sleeve"].get("note", "") for r in res.records
                 if r["kind"] == "decision" and r["date"] >= date(2025, 2, 1)]  # fmt: skip
        assert any("data fault" in n for n in notes)

    def test_cbil_without_data_keeps_cash_at_the_broker(
        self, store: VersionedStore, tmp_path: Path
    ) -> None:
        """v1.8: the frozen config names CBIL; the fixture set has no CBIL bars, so the sleeve
        waits for the fund's inception and idle cash stays at the broker."""
        res = backtest(store, SafetyStore(tmp_path / "c.sqlite"), date(2024, 6, 3),
                       date(2024, 9, 30), CFG).run()  # fmt: skip
        assert res.sleeve == "CBIL" and not any(t.symbol == "CBIL" for t in res.ledger.trades)
        notes = [r["sleeve"]["note"] for r in res.records if r["kind"] == "decision"]
        assert notes and all("inception" in n for n in notes)


class TestFixtureChecks:
    def test_the_viu_split_doubled_the_ledgers_shares(self, runs: list[BacktestResult]) -> None:
        """The fixture's deliberate 2-for-1 VIU split on 2025-06-16 (SPLITS in
        make_market_fixtures.py): the ledger doubles the holding and its value does not jump."""
        res = runs[0]
        split = next(s for s in res.ledger.splits if s.symbol == "VIU")
        assert (split.ex_date, split.ratio, split.shares_before, split.shares_added) == (
            date(2025, 6, 16), Decimal(2), 161, 161,
        )  # fmt: skip
        assert res.ledger.held_before("VIU", date(2025, 6, 17)) == 322
        w = {p.day: p.weights.get("VIU", 0.0) for p in res.nav}
        assert w[date(2025, 6, 16)] == pytest.approx(w[date(2025, 6, 13)], abs=0.002)


@pytest.fixture(scope="module")
def variants(
    store: VersionedStore, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, BacktestResult]:
    """The §12.3 benchmarks 2 and 3 and the one-session delay, at 1x, with the sleeve."""
    work = tmp_path_factory.mktemp("variants")
    kinds: dict[str, dict[str, Any]] = {
        "all_on": {"benchmark": ALL_SIGNALS_ON},
        "equal": {"benchmark": EQUAL_WEIGHT_TIMING},
        "delay": {"delay_sessions": 1},
    }
    return {
        name: backtest(store, SafetyStore(work / f"{name}.sqlite"), DEMO_START, DEMO_END,
                       SLEEVE_CFG, **kw).run()
        for name, kw in kinds.items()
    }  # fmt: skip


def _measured_decisions(res: BacktestResult) -> list[dict[str, Any]]:
    return [r for r in res.records if r["kind"] == "decision" and r["date"] >= date(2025, 1, 3)]


class TestAttribution:
    def test_fund_pnl_adds_up_to_the_nav_change(self, runs: list[BacktestResult]) -> None:
        """Every fund's P&L plus interest, USD cash revaluation and conversion costs is the NAV
        change of each session, to the cent (with the sleeve and with cash at the broker)."""
        for res in (runs[0], runs[2]):
            att = attribute(res)
            assert float(np.max(np.abs(att.residual))) < 0.05
            total = sum(float(v.sum()) for v in att.pnl.values()) + float(att.cash.sum())
            assert total == pytest.approx(res.nav[-1].nav - res.nav[0].nav, abs=1.0)

    def test_leave_one_instrument_out(self, runs: list[BacktestResult]) -> None:
        res = runs[1]  # 2x: the research gate's cost level
        rows = leave_one_instrument_out(res)
        full = backtest_metrics(res, CFG.research).annual_excess_return
        assert set(rows) == set(res.universe)
        assert rows["DLR"] == pytest.approx(full)  # never held: nothing to remove
        assert rows["CGL.C"] != pytest.approx(full, abs=1e-4)  # the largest holding


class TestGateStatistics:
    def test_counts_and_average_scales(self, runs: list[BacktestResult]) -> None:
        res = runs[0]
        stats = gate_statistics(res)
        assert {g: (s.binds, s.decisions) for g, s in stats.items()} == summarize(res).gate_binds
        assert all(0 < (s.avg_scale or 0) < 1 for s in stats.values() if s.binds)
        assert stats["event"].binds and stats["stress"].binds

    def test_the_gate_log_carries_weights_in_and_out(self, runs: list[BacktestResult]) -> None:
        d = _measured_decisions(runs[0])[0]
        for g in d["gates"]:
            assert set(g["weights_in"]) == set(g["weights_out"]) == set(runs[0].universe)


class TestBenchmarks:
    def test_all_signals_on_targets_every_eligible_fund(
        self, variants: dict[str, BacktestResult]
    ) -> None:
        decisions = _measured_decisions(variants["all_on"])
        for r in decisions:
            assert all(f["target"] > 0 for f in r["funds"].values() if f["eligible"])
        assert any(not f["on"] and f["target"] > 0 for r in decisions for f in r["funds"].values())

    def test_equal_weight_timing_is_one_eighth_times_the_signal(
        self, variants: dict[str, BacktestResult]
    ) -> None:
        res = variants["equal"]
        for r in _measured_decisions(res):
            for f in r["funds"].values():
                assert f["target"] == pytest.approx(0.125 if f["eligible"] and f["on"] else 0.0)
            assert [g["gate"] for g in r["gates"]] == ["data", "faults"]
        assert not any(r["kind"] == "safety" for r in res.records)  # no daily safety check

    def test_the_benchmarks_use_the_same_cash_handling(
        self, variants: dict[str, BacktestResult]
    ) -> None:
        for name in ("all_on", "equal"):
            res = variants[name]
            assert res.sleeve == DEMO_SLEEVE
            assert any(t.symbol == DEMO_SLEEVE for t in res.ledger.trades)


class TestDelayVariant:
    def test_every_fill_comes_one_session_later(
        self, runs: list[BacktestResult], variants: dict[str, BacktestResult]
    ) -> None:
        def lags(res: BacktestResult) -> list[int]:
            decided = {r["decision_id"]: r["date"] for r in res.records if r["kind"] == "decision"}
            out = []
            for day, det in res.fills:
                if det.decision_id in decided:
                    code = "XNAS" if det.fill.symbol == "PDBC" else "XTSE"
                    after = decided[det.decision_id] + timedelta(days=1)
                    out.append(len(sessions(code, after, day)))
            return out

        base, late = lags(runs[0]), lags(variants["delay"])
        assert min(base) == 1 and min(late) == 2 and 1 not in late
