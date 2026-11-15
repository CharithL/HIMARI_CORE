"""The cash sleeve's weekly rule (§7.4, amendment v1.7)."""

from pathlib import Path

import pytest

from himari.core.config import CashSleeveConfig
from himari.execution.costs import load_cost_model
from himari.execution.orders import PlannedOrder
from himari.execution.sleeve import (
    SLEEVE_FUNDING,
    SLEEVE_SWEEP,
    expected_cad_after,
    plan_sleeve,
)

ROOT = Path(__file__).parents[2]
COSTS = load_cost_model(ROOT / "config" / "costs_v1.yaml", 1.0,
                        ROOT / "tests" / "fixtures" / "commissions_demo.yaml")  # fmt: skip
CFG = CashSleeveConfig(enabled=True, symbol="TBILL.SYN")
NAV = 100_000.0  # buffer 2,000, band 3,000


def flat5(shares: int, price: float) -> float:
    return 5.0


def plan(cash: float, held: int = 1000, buys: bool = True):  # type: ignore[no-untyped-def]
    return plan_sleeve(cash, NAV, CFG, 50.0, 10.0, flat5, held, buys_allowed=buys)


def test_buffer_and_band() -> None:
    assert (CFG.buffer(NAV), CFG.band(NAV)) == (2_000.0, 3_000.0)
    assert (CFG.buffer(5_000.0), CFG.band(5_000.0)) == (200.0, 1_000.0)  # the minimums


def test_inside_the_band_no_order() -> None:
    for cash in (1_000.0, 2_000.0, 5_000.0):  # buffer/2 <= C <= buffer + band
        p = plan(cash)
        assert p.side is None and p.shares == 0


def test_low_cash_sells_back_to_the_buffer_rounded_up() -> None:
    p = plan(-10_000.0)
    assert (p.side, p.reason) == ("sell", SLEEVE_FUNDING)
    net = 50.0 * (1 - 0.001)
    assert p.shares * net - 5.0 >= 12_000.0 > (p.shares - 1) * net - 5.0  # just enough
    assert plan(999.99).side == "sell"  # below buffer / 2


def test_a_sale_never_exceeds_the_holding() -> None:
    assert plan(-1_000_000.0, held=30).shares == 30
    empty = plan(-1_000.0, held=0)
    assert empty.side is None and "empty" in empty.note


def test_excess_cash_is_swept_rounded_down() -> None:
    p = plan(10_000.0)
    assert (p.side, p.reason) == ("buy", SLEEVE_SWEEP)
    gross = 50.0 * 1.001
    assert p.shares * gross + 5.0 <= 8_000.0 < (p.shares + 1) * gross + 5.0


def test_a_data_fault_stops_sweeps_only() -> None:
    assert plan(10_000.0, buys=False).side is None
    assert plan(-10_000.0, buys=False).side == "sell"


def order(symbol: str, side: str, shares: int, price: float, ccy: str) -> PlannedOrder:
    return PlannedOrder("D-1", symbol, side, shares, ccy, price, price, "band")


def test_expected_cash_after_the_plan() -> None:
    orders = [
        order("VCN", "buy", 100, 40.0, "CAD"),  # 4,004 + 5
        order("VIU", "sell", 50, 40.0, "CAD"),  # 1,998 - 5
        order("PDBC", "buy", 100, 14.0, "USD"),  # funded: (1,400 + 5) x 1.01 x 1.35 x 1.0005
    ]
    got = expected_cad_after(orders, 10_000.0, 0.0, 1.35, COSTS, 0.01, usd_surplus=100.0)
    funding = (1_400.0 + 5.0) * 1.01 * 1.35 * 1.0005
    want = 10_000.0 - 4_009.0 + 1_993.0 - funding + 100.0 * 1.35 * 0.9995
    assert got == pytest.approx(want)
