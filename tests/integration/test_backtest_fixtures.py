"""M4 acceptance on the fixture set (§16): 1x and 2x runs, determinism, no look-ahead, the
amendment v1.6 band and post-trade check, interest on cash, and the safety paths."""

import csv
import hashlib
import shutil
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from himari.core.config import load_config
from himari.data.ingest import SeriesNames
from himari.data.storage import VersionedStore
from himari.execution.costs import load_cost_model
from himari.gates.events import load_events
from himari.risk.state_machine import SafetyStore
from himari.sim.engine import Backtest, BacktestResult, BacktestSettings
from himari.sim.market import MarketTruth, PointInTimeData
from himari.sim.report import nav_csv, summarize, trades_csv
from himari.sim.run import (
    DEMO_END,
    DEMO_START,
    RunPlan,
    demo_confirmations,
    fixture_root,
    ingest_demo_fixtures,
    run_backtests,
)

ROOT = Path(__file__).parents[2]
CFG = load_config(ROOT / "config" / "frozen_v1.yaml")
COMMISSIONS = ROOT / "tests" / "fixtures" / "commissions_demo.yaml"
NAMES = SeriesNames(CFG.sources.fx_series, CFG.sources.cash_rate_series)
CASH = Decimal("100000")
REASONS = {"band", "full_exit", "post_trade_limit", "halved", "safety_cut"}

# sha256 of the 1x run log (decisions.jsonl) on the fixture set. Change only on purpose, with a
# CHANGELOG entry that says why.
GOLDEN_LOG_HASH = "2a8b67c6830b8196a99f013da1cc2f9f312ff6bde0a74bc7f97f2fd679510dad"


def plan(multipliers: tuple[float, ...], start: date = DEMO_START, end: date = DEMO_END) -> RunPlan:
    return RunPlan(start, end, CASH, multipliers, COMMISSIONS, "csv", "xcheck")


def log_hash(res: BacktestResult) -> str:
    return hashlib.sha256("".join(line + "\n" for line in res.log_lines()).encode()).hexdigest()


def backtest(
    store: VersionedStore, safety: SafetyStore, start: date, end: date, multiplier: float = 1.0
) -> Backtest:
    """A single run with a caller-supplied safety store (for the KILL and fault paths)."""
    conf = demo_confirmations(ROOT)
    data = PointInTimeData(store, CFG, "csv", "xcheck", None, conf.confirmed(),
                           conf.confirmed("Q10"))  # fmt: skip
    return Backtest(
        CFG,
        data,
        MarketTruth(store, "csv", NAMES),
        load_cost_model(ROOT / "config" / "costs_v1.yaml", multiplier, COMMISSIONS),
        load_events(ROOT / "config" / "events", CFG.events.map.keys()),
        BacktestSettings(start, end, CASH, multiplier),
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
    work = tmp_path_factory.mktemp("runs")
    return run_backtests(CFG, ROOT, store, plan((1.0, 2.0)), work, None, demo_confirmations(ROOT))


class TestFixtureRuns:
    def test_ledger_and_broker_reconcile_every_session(self, runs: list[BacktestResult]) -> None:
        for res in runs:
            checks = [r["reconciliation"] for r in res.records if "reconciliation" in r]
            assert checks and set(checks) == {"reconciled"}
            assert res.alerts == []

    def test_costs_at_2x(self, runs: list[BacktestResult]) -> None:
        one, two = (summarize(r) for r in runs)
        assert two.costs_cad > 1.8 * one.costs_cad
        assert two.final_nav < one.final_nav

    def test_idle_cash_earns_interest(self, runs: list[BacktestResult]) -> None:
        """v1.6: during the all-cash warm-up NAV rises with the accrual; monthly postings."""
        res = runs[0]
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
            assert all(t.gates for t in res.ledger.trades if t.decision_id == "D-2025-01-24-000")

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
        (again,) = run_backtests(CFG, ROOT, store, plan((1.0,)), tmp_path, None,
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
        (shocked,) = run_backtests(CFG, ROOT, shocked_store, plan((1.0,)), tmp_path / "run",
                                   None, demo_confirmations(ROOT))  # fmt: skip

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
