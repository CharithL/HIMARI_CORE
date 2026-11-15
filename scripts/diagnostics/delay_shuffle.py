"""Why does the one-session delay cost so much on the fixture? (M5 review, item C)

Runs the baseline and the delay variant (every fill one session later) at 2x costs on the
committed fixture and on copies whose daily returns are shuffled within each fund
(`himari.research.shuffle`). With no look-ahead in the base case and no day-to-day dependence
in the returns, the gap (baseline minus delay) should average about zero over shuffles, apart
from what the cost model charges for the later fills.

    python scripts/diagnostics/delay_shuffle.py [shuffles]
"""

from __future__ import annotations

import csv
import statistics
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from himari.core.config import load_config  # noqa: E402
from himari.data.ingest import SeriesNames  # noqa: E402
from himari.execution.costs import load_cost_model  # noqa: E402
from himari.gates.events import load_events  # noqa: E402
from himari.research.metrics import backtest_metrics  # noqa: E402
from himari.research.shuffle import shuffled_market  # noqa: E402
from himari.risk.state_machine import SafetyStore  # noqa: E402
from himari.sim.engine import Backtest, BacktestResult, BacktestSettings  # noqa: E402
from himari.sim.market import MarketTruth, PointInTimeData  # noqa: E402
from himari.sim.report import summarize  # noqa: E402
from himari.sim.run import (  # noqa: E402
    DEMO_END,
    DEMO_SLEEVE,
    DEMO_SLEEVE_HALF_SPREAD_BPS,
    DEMO_START,
    demo_config,
    demo_confirmations,
    fixture_root,
    ingest_demo_fixtures,
)

CFG = demo_config(load_config(ROOT / "config" / "frozen_v1.yaml"))
COMMISSIONS = ROOT / "tests" / "fixtures" / "commissions_demo.yaml"
FUNDS = [i.symbol for i in CFG.stage1.instruments()]


def run(store_root: Path, market: Path, work: Path) -> tuple[BacktestResult, BacktestResult]:
    """The baseline and the delay variant at 2x on one market."""
    store = ingest_demo_fixtures(CFG, ROOT, store_root, market)
    conf = demo_confirmations(ROOT)
    data = PointInTimeData(
        store, CFG, "csv", "xcheck", None, conf.confirmed(), conf.confirmed("Q10")
    )
    names = SeriesNames(CFG.sources.fx_series, CFG.sources.cash_rate_series)
    truth = MarketTruth(store, "csv", names)
    events = load_events(ROOT / "config" / "events", CFG.events.map.keys())
    out = []
    for delay in (0, 1):
        costs = load_cost_model(ROOT / "config" / "costs_v1.yaml", 2.0, COMMISSIONS)
        costs = costs.with_symbol(DEMO_SLEEVE, DEMO_SLEEVE_HALF_SPREAD_BPS)
        settings = BacktestSettings(
            DEMO_START, DEMO_END, Decimal(100000), 2.0, delay_sessions=delay,
            label="delay" if delay else "",
        )  # fmt: skip
        safety = SafetyStore(work / f"safety-{delay}.sqlite")
        out.append(Backtest(CFG, data, truth, costs, events, settings, safety).run())
    return out[0], out[1]


def describe(base: BacktestResult, late: BacktestResult) -> dict[str, float]:
    b, d = backtest_metrics(base, CFG.research), backtest_metrics(late, CFG.research)
    sb, sd = summarize(base), summarize(late)
    m0 = sb.metric_start

    def stressed(res: BacktestResult) -> int:
        return sum(
            1
            for day, det in res.fills
            if day >= m0 and det.stressed and det.fill.symbol != res.sleeve
        )

    return {
        "gap": b.annual_excess_return - d.annual_excess_return,
        "cost_gap": (sd.costs_cad - sb.costs_cad) / sb.base_nav,  # extra strategy costs / NAV
        "stressed_base": stressed(base),
        "stressed_delay": stressed(late),
    }


def path_drift(base: BacktestResult, market: Path) -> float:
    """What delaying the baseline's own measured trades by one session would cost from price
    moves alone: side x shares x (next close - fill-day close), in CAD, over the base NAV."""
    closes: dict[str, dict[date, float]] = {}
    for sym in FUNDS:
        with open(market / "primary" / "bars" / f"{sym}.csv", newline="", encoding="utf-8") as f:
            closes[sym] = {
                date.fromisoformat(r["session_date"]): float(r["close"]) for r in csv.DictReader(f)
            }
    s = summarize(base)
    rate = {p.day: p.usd_to_cad for p in base.nav}
    total = 0.0
    for day, det in base.fills:
        sym = det.fill.symbol
        if day < s.metric_start or sym not in closes:
            continue
        seq = sorted(closes[sym])
        i = seq.index(day)
        if i + 1 < len(seq):
            move = closes[sym][seq[i + 1]] - closes[sym][day]
            side = 1.0 if det.fill.side == "buy" else -1.0
            fx = 1.0 if det.currency == "CAD" else rate.get(day, 1.0)
            total += side * det.fill.shares * move * fx
    return total / s.base_nav


def main() -> None:
    shuffles = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        base, late = run(t / "orig-store", fixture_root(ROOT), t)
        orig = describe(base, late)
        print(
            f"original fixture: gap {orig['gap']:+.2%} a year at 2x; extra strategy costs "
            f"{orig['cost_gap']:+.2%} of NAV; stressed fills {orig['stressed_base']:.0f} -> "
            f"{orig['stressed_delay']:.0f}; one-day price drift on the baseline's trades "
            f"{path_drift(base, fixture_root(ROOT)):+.2%} of NAV",
            flush=True,
        )
        gaps = []
        for seed in range(1, shuffles + 1):
            d = t / f"shuffle-{seed}"
            d.mkdir()
            market = shuffled_market(fixture_root(ROOT), d / "market", FUNDS, seed)
            b, late = run(d / "store", market, d)
            res = describe(b, late)
            gaps.append(res["gap"])
            print(
                f"shuffle {seed:>2}: gap {res['gap']:+.2%}; extra costs {res['cost_gap']:+.2%}; "
                f"stressed fills {res['stressed_base']:.0f} -> {res['stressed_delay']:.0f}; "
                f"price drift {path_drift(b, market):+.2%}",
                flush=True,
            )
        sd = statistics.stdev(gaps) if len(gaps) > 1 else 0.0
        print(
            f"shuffled: mean gap {statistics.mean(gaps):+.2%} a year, sd {sd:.2%}, range "
            f"{min(gaps):+.2%} to {max(gaps):+.2%} over {len(gaps)} shuffles"
        )


if __name__ == "__main__":
    main()
