"""The IBKR daily-bars adapter (the Q6 cross-check, v1.10) against a fake TWS layer."""

from datetime import date
from decimal import Decimal

import pytest

from himari.data.ibkr import IbkrBar, IbkrSource

SPLIT = date(2025, 6, 16)


class FakeTws:
    """What IBKR returns: TRADES bars with prices before a split divided by its ratio."""

    def __init__(self) -> None:
        self.resolved: list[tuple[str, str, str]] = []

    def resolve(self, symbol: str, primary_exchange: str, currency: str) -> int:
        self.resolved.append((symbol, primary_exchange, currency))
        return {"VIU": 101, "PDBC": 102, "CGL.C": 103}[symbol]

    def head_date(self, con_id: int) -> date | None:
        return {101: date(2015, 12, 8), 102: None}.get(con_id)

    def daily_trades(self, con_id: int, start: date, end: date) -> list[IbkrBar]:
        # as traded: 44.61 before the 2-for-1 split, 22.47 after; IBKR shows 22.305 before
        return [
            IbkrBar(date(2025, 6, 12), 22.38, 22.48, 21.8, 22.38, 200_000),
            IbkrBar(date(2025, 6, 13), 22.365, 22.425, 22.23, 22.305, 80_000),
            IbkrBar(date(2025, 6, 16), 22.31, 22.6, 22.25, 22.47, 111_132),
        ]


def source(tws: FakeTws) -> IbkrSource:
    splits = {"VIU": [(SPLIT, Decimal(2))]}
    exchanges = {"VIU": "TSX", "PDBC": "NASDAQ", "CGL.C": "TSX"}
    return IbkrSource(tws, exchanges, lambda s: splits.get(s, []))


def test_the_split_adjustment_is_reversed_before_q6() -> None:
    bars = source(FakeTws()).fetch_bars("VIU", date(2025, 6, 12), date(2025, 6, 16))
    assert [(b.session_date, b.close, b.volume) for b in bars] == [
        (date(2025, 6, 12), 44.76, 100_000),
        (date(2025, 6, 13), 44.61, 40_000),
        (date(2025, 6, 16), 22.47, 111_132),  # the ex-date trades at post-split prices
    ]
    assert all(b.source == "ibkr" for b in bars)


def test_a_fund_without_splits_is_passed_through() -> None:
    bars = source(FakeTws()).fetch_bars("PDBC", date(2025, 6, 12), date(2025, 6, 13))
    assert [b.close for b in bars] == [22.38, 22.305]


def test_listings_and_contract_lookups() -> None:
    tws = FakeTws()
    src = source(tws)
    src.fetch_bars("VIU", date(2025, 6, 12), date(2025, 6, 16))
    src.fetch_bars("VIU", date(2025, 6, 12), date(2025, 6, 16))
    src.earliest("PDBC")
    src.con_id("CGL.C")
    assert tws.resolved == [("VIU", "TSE", "CAD"), ("PDBC", "NASDAQ", "USD"),
                            ("CGL.C", "TSE", "CAD")]  # fmt: skip
    assert src.earliest("VIU") == date(2015, 12, 8) and src.earliest("PDBC") is None


def test_no_distributions_come_from_ibkr() -> None:
    assert source(FakeTws()).fetch_actions("VIU", date(2015, 1, 1), date(2025, 12, 31)) == []


def test_bars_outside_the_window_are_dropped() -> None:
    bars = source(FakeTws()).fetch_bars("VIU", date(2025, 6, 13), date(2025, 6, 13))
    assert [b.session_date for b in bars] == [date(2025, 6, 13)]


@pytest.mark.parametrize("bad", ["XYZ"])
def test_an_unknown_symbol_is_refused(bad: str) -> None:
    with pytest.raises(KeyError):
        source(FakeTws()).con_id(bad)
