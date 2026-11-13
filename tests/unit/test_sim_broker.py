"""SimBroker and the backtest fill model (§10.4), with interest on settled cash (v1.6).

Failure modes from §14 covered here: capacity cap -> partial fill; a duplicate order is
refused through idempotency; market and local holidays diverge.
"""

import statistics
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from himari.core.types import Bar, CorporateAction, Order
from himari.data.calendars import sessions
from himari.execution.costs import load_cost_model
from himari.sim.broker import SimBroker

ROOT = Path(__file__).parents[2]
COSTS = load_cost_model(ROOT / "config" / "costs_v1.yaml", 1.0,
                        ROOT / "tests" / "fixtures" / "commissions_demo.yaml")  # fmt: skip
D = Decimal
CAL = {"VCN": "XTSE", "VIU": "XTSE", "PDBC": "XNAS"}
CUR = {"VCN": "CAD", "VIU": "CAD", "PDBC": "USD"}
EXCH = {"VCN": "TSX", "VIU": "TSX", "PDBC": "NASDAQ"}


class FakeTruth:
    """Constant closes and volumes on each fund's real sessions, with overrides."""

    def __init__(self, volume: int = 100_000, actions: list[CorporateAction] | None = None):
        start, end = date(2024, 11, 1), date(2025, 8, 29)
        close = {"VCN": 40.0, "VIU": 40.0, "PDBC": 14.0}
        self.bars = {
            s: {
                d: Bar(
                    symbol=s,
                    session_date=d,
                    open=c,
                    high=c,
                    low=c,
                    close=c,
                    volume=volume,
                    source="t",
                )
                for d in sessions(CAL[s], start, end)
            }  # fmt: skip
            for s, c in close.items()
        }
        self.dates = {s: sorted(m) for s, m in self.bars.items()}
        self.actions = actions or []

    def set(self, symbol: str, day: date, **fields: object) -> None:
        self.bars[symbol][day] = self.bars[symbol][day].model_copy(update=fields)

    def bar(self, symbol: str, day: date) -> Bar | None:
        return self.bars.get(symbol, {}).get(day)

    def median_traded_value(self, symbol: str, day: date, n: int) -> float | None:
        prior = [d for d in self.dates[symbol] if d < day][-n:]
        values = [self.bars[symbol][d].close * self.bars[symbol][d].volume for d in prior]
        return statistics.median(values) if values else None

    def actions_between(self, after: date, through: date) -> list[CorporateAction]:
        return [a for a in self.actions if after < a.ex_date <= through]

    def usd_to_cad(self, day: date, max_age_days: int) -> float | None:
        return 1.35

    def corra_on_or_before(self, day: date) -> float | None:
        return 4.0


def broker(truth: FakeTruth, cad: str = "100000", carry: int = 3) -> SimBroker:
    return SimBroker(truth, COSTS, CAL, CUR, EXCH, frozenset({"PDBC"}), D("0.15"), 0.005, carry,
                     0.01, 3, {"CAD": D(cad)})  # type: ignore[arg-type]  # fmt: skip


def order(symbol: str, side: str, shares: int, decision: str = "D-1") -> Order:
    return Order(decision_id=decision, symbol=symbol, side=side, shares=shares,
                 order_type="close", idempotency_key=f"{decision}:{symbol}")  # fmt: skip


def test_fills_at_the_next_close_with_costs() -> None:
    b = broker(FakeTruth())
    b.start(date(2025, 1, 3))
    assert [a.status for a in b.submit([order("VCN", "buy", 100)])] == ["submitted"]
    rep = b.run_session(date(2025, 1, 6))
    (f,) = rep.fills
    assert f.fill.price == D("40.040000") and f.side_bps == 10.0
    assert b.cash()["CAD"] == D("100000") - D("4004.00") - D("5.00")


def test_participation_cap_partial_fills_carry_then_expire() -> None:
    """Cap: 0.5% of the 4,000,000 CAD median traded value = 500 shares a session."""
    b = broker(FakeTruth())
    b.start(date(2025, 1, 3))
    b.submit([order("VCN", "buy", 2300)])
    days = sessions("XTSE", date(2025, 1, 6), date(2025, 1, 10))
    filled = [sum(f.fill.shares for f in b.run_session(d).fills) for d in days[:4]]
    assert filled == [500, 500, 500, 500]
    assert b.order("D-1:VCN").status == "expired"  # 1 session + 3 carried
    rep = b.run_session(days[4])
    assert not rep.fills


def test_expired_remainder_is_logged() -> None:
    b = broker(FakeTruth())
    b.start(date(2025, 1, 3))
    b.submit([order("VCN", "buy", 2300)])
    reps = [b.run_session(d) for d in sessions("XTSE", date(2025, 1, 6), date(2025, 1, 9))]
    (u,) = reps[-1].unfilled
    assert (u.symbol, u.shares) == ("VCN", 300)


def test_a_halt_counts_toward_the_carry_limit() -> None:
    truth = FakeTruth()
    truth.set("VCN", date(2025, 1, 6), volume=0)
    b = broker(truth)
    b.start(date(2025, 1, 3))
    b.submit([order("VCN", "buy", 100)])
    rep = b.run_session(date(2025, 1, 6))
    assert not rep.fills and rep.waiting == ["VCN: halted (zero volume)"]
    assert b.order("D-1:VCN").attempts == 1
    assert b.run_session(date(2025, 1, 7)).fills


def test_each_fund_trades_on_its_own_exchange() -> None:
    """Canada Day 2025: NASDAQ open, TSX closed. PDBC fills on 1 July, VCN waits for 2 July."""
    b = broker(FakeTruth())
    b.start(date(2025, 6, 30))
    b.submit([order("PDBC", "buy", 100), order("VCN", "buy", 100)])
    rep = b.run_session(date(2025, 7, 1))
    assert [f.fill.symbol for f in rep.fills] == ["PDBC"]
    assert b.order("D-1:VCN").attempts == 0  # no session: not an attempt
    assert [f.fill.symbol for f in b.run_session(date(2025, 7, 2)).fills] == ["VCN"]


def test_a_duplicate_order_is_refused() -> None:
    b = broker(FakeTruth())
    b.start(date(2025, 1, 3))
    b.submit([order("VCN", "buy", 100)])
    assert [a.status for a in b.submit([order("VCN", "buy", 100)])] == ["duplicate"]
    assert sum(f.fill.shares for f in b.run_session(date(2025, 1, 6)).fills) == 100


def test_buys_are_cut_to_the_cash_on_hand() -> None:
    b = broker(FakeTruth(), cad="1000")
    b.start(date(2025, 1, 3))
    b.submit([order("VCN", "buy", 100)])
    (f,) = b.run_session(date(2025, 1, 6)).fills
    assert f.fill.shares == 24 and f.cash_limited  # 25 x 40.04 + 5 > 1,000
    assert b.cash()["CAD"] >= 0


def test_usd_buys_are_funded_first() -> None:
    b = broker(FakeTruth())
    b.start(date(2025, 1, 3))
    b.submit([order("PDBC", "buy", 100)])
    rep = b.run_session(date(2025, 1, 6))
    (fx,) = rep.conversions
    assert fx.purpose == "fx_funding" and (fx.sell_currency, fx.buy_currency) == ("CAD", "USD")
    assert fx.execution_rate == pytest.approx(1.35 * 1.0005)
    assert rep.fills[0].fill.shares == 100 and b.cash()["USD"] >= 0


def test_funding_covers_the_commission() -> None:
    """One PDBC share (14 USD) with no USD on hand: the conversion must also cover the
    5 USD commission, or the buy can never fill."""
    b = broker(FakeTruth())
    b.start(date(2025, 1, 3))
    b.submit([order("PDBC", "buy", 1)])
    rep = b.run_session(date(2025, 1, 6))
    assert [f.fill.shares for f in rep.fills] == [1] and not rep.fills[0].cash_limited


def test_a_split_changes_the_holding_and_open_orders() -> None:
    split = CorporateAction(symbol="VIU", ex_date=date(2025, 1, 8), action_type="split",
                            amount=D("2"))  # fmt: skip
    truth = FakeTruth(actions=[split])
    b = broker(truth, carry=5)
    b.start(date(2025, 1, 3))
    b.submit([order("VIU", "buy", 600)])  # 500 fill, 100 carry
    b.run_session(date(2025, 1, 6))
    b.run_session(date(2025, 1, 7))  # the last 100 fill
    b.submit([order("VIU", "buy", 50, "D-2")])
    b.run_session(date(2025, 1, 8))
    assert b.positions()["VIU"].shares == 1200 + 100  # 600 x 2, plus the 50-share order x 2


def test_distributions_paid_net_on_the_pay_date_and_reinvested_pays_nothing() -> None:
    acts = [
        CorporateAction(symbol="PDBC", ex_date=date(2025, 1, 8), pay_date=date(2025, 1, 10),
                        action_type="cash", amount=D("0.60")),
        CorporateAction(symbol="PDBC", ex_date=date(2025, 1, 9), action_type="reinvested",
                        amount=D("0.10")),
    ]  # fmt: skip
    b = broker(FakeTruth(actions=acts))
    b.start(date(2025, 1, 3))
    b.submit([order("PDBC", "buy", 100)])
    b.run_session(date(2025, 1, 6))
    usd = b.cash()["USD"]
    for d in (date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9)):
        assert not b.run_session(d).paid
    rep = b.run_session(date(2025, 1, 10))
    assert rep.paid == [("PDBC", D("51.00"), "USD")]  # 60.00 less 15% withholding
    assert b.cash()["USD"] == usd + D("51.00")


def test_interest_on_settled_cash_posted_monthly() -> None:
    """113,000 CAD, 13,000 earn nothing, CORRA 4% - 0.5% at full rate (NAV over US$100,000)."""
    b = broker(FakeTruth(), cad="113000")
    b.start(date(2025, 1, 28))
    b.mark_nav(202_500.0, 1.35)  # US$150,000
    for d in (date(2025, 1, 29), date(2025, 1, 30), date(2025, 1, 31)):
        assert not b.run_session(d).interest
    rep = b.run_session(date(2025, 2, 3))
    (posting,) = rep.interest
    assert posting.period == "2025-01" and posting.currency == "CAD"
    assert posting.amount == D("28.77")  # 100,000 x 3.5% x 3 days / 365
    assert rep.accrued == {"CAD": D("28.77")}  # 1 to 3 February; USD earns nothing


def test_sale_proceeds_earn_once_settled() -> None:
    b = broker(FakeTruth(), cad="13000")  # nothing above the tier
    b.start(date(2025, 1, 3))
    b.mark_nav(202_500.0, 1.35)
    b.submit([order("VCN", "buy", 300)])
    b.run_session(date(2025, 1, 6))
    b.submit([order("VCN", "sell", 300, "D-2")])
    b.run_session(date(2025, 1, 7))  # sold: T+1, settles 8 January
    assert b.settled_cash("CAD", date(2025, 1, 7)) < D("13000")
    assert b.settled_cash("CAD", date(2025, 1, 8)) == b.cash()["CAD"]
