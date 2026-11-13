"""Reinvested year-end distributions and Q10 (amendment v1.6)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from himari.accounting.indices import action_sessions, total_return_index
from himari.core.types import Bar, CorporateAction, Currency, QualityFlag
from himari.data.sources import CsvSource
from himari.quality.checks import Severity, check_december_split, check_jumps

ROOT = Path(__file__).parents[2]
DAYS = [date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 2)]


def bars(closes: list[float]) -> list[Bar]:
    return [Bar(symbol="VCN", session_date=d, open=c, high=c, low=c, close=c, volume=1,
                source="t") for d, c in zip(DAYS, closes, strict=True)]  # fmt: skip


def action(kind: str, day: date, amount: str) -> CorporateAction:
    return CorporateAction(symbol="VCN", ex_date=day, action_type=kind, amount=Decimal(amount))  # type: ignore[arg-type]


REINVESTED = action("reinvested", date(2025, 12, 31), "0.50")


def test_reinvested_adds_nothing_to_the_total_return() -> None:
    plain = total_return_index(bars([40.0, 40.4, 40.4, 40.8]), [], Currency.CAD)
    with_r = total_return_index(bars([40.0, 40.4, 40.4, 40.8]), [REINVESTED], Currency.CAD)
    assert list(with_r.wealth) == list(plain.wealth)


def test_reinvested_never_explains_a_price_move() -> None:
    """Q4: a 20% drop on a reinvested ex-date is still a fault (no cash left the fund)."""
    b = bars([40.0, 40.0, 32.0, 32.0])
    assert date(2025, 12, 31) not in action_sessions(DAYS, [REINVESTED])
    faults = [r for r in check_jumps("VCN", b, [REINVESTED], set(), 0.15)
              if r.severity == Severity.FAULT]  # fmt: skip
    assert [r.session_date for r in faults] == [date(2025, 12, 31)]
    cash = action("cash", date(2025, 12, 31), "8.00")
    assert date(2025, 12, 31) in action_sessions(DAYS, [cash])


def test_q10_needs_each_december_split_confirmed() -> None:
    acts = [
        action("cash", date(2025, 12, 29), "0.25"),
        REINVESTED,
        action("cash", date(2025, 9, 25), "0.25"),  # not December: no check
        action("split", date(2025, 12, 30), "2"),  # not a distribution
    ]
    results = check_december_split("VCN", acts, set())
    assert [(r.session_date, r.flag) for r in results] == [
        (date(2025, 12, 29), QualityFlag.ACTION_UNCONFIRMED),
        (date(2025, 12, 31), QualityFlag.ACTION_UNCONFIRMED),
    ]
    assert all(r.severity == Severity.FAULT for r in results)
    ok = check_december_split("VCN", acts, {date(2025, 12, 29), date(2025, 12, 31)})
    assert [r.severity for r in ok] == [Severity.PASS]


def test_csv_source_reads_reinvested_rows() -> None:
    src = CsvSource(ROOT / "tests" / "fixtures" / "market" / "primary")
    acts = src.fetch_actions("VCN", date(2024, 12, 1), date(2024, 12, 31))
    assert [(a.ex_date, a.action_type, a.pay_date) for a in acts] == [
        (date(2024, 12, 27), "cash", date(2025, 1, 6)),
        (date(2024, 12, 31), "reinvested", None),
    ]
