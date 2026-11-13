"""The account ledger (§9.4, amendment v1.6)."""

from datetime import date
from decimal import Decimal

import pytest

from himari.accounting.ledger import ConversionRecord, InterestRecord, Ledger
from himari.core.types import CorporateAction, Fill

D = Decimal
CUR = {"VCN": "CAD", "PDBC": "USD"}


def ledger(cad: str = "100000") -> Ledger:
    return Ledger(cash={"CAD": D(cad), "USD": D(0)}, withholding_rate=D("0.15"),
                  us_listed=frozenset({"PDBC"}))  # fmt: skip


def fill(symbol: str, side: str, shares: int, price: str, commission: str = "5") -> Fill:
    return Fill(order_id=f"D-1:{symbol}", symbol=symbol, side=side, shares=shares,
                price=D(price), commission=D(commission))  # fmt: skip


def cash(symbol: str, ex: date, amount: str, pay: date | None = None) -> CorporateAction:
    return CorporateAction(symbol=symbol, ex_date=ex, pay_date=pay, action_type="cash",
                           amount=D(amount))  # fmt: skip


class TestTrades:
    def test_buy_and_sell_move_cash_and_positions(self) -> None:
        lg = ledger()
        lg.book_fill(date(2025, 1, 6), fill("VCN", "buy", 100, "40.040000"), "CAD", "D-1", "band")
        assert lg.cash["CAD"] == D("100000") - D("4004.00") - D("5.00")
        lg.book_fill(date(2025, 1, 7), fill("VCN", "sell", 40, "41.000000"), "CAD", "D-2", "band")
        assert lg.positions == {"VCN": 60}
        assert lg.trades[0].reason == "band" and lg.trades[1].gross == D("1640.00")

    def test_no_short_positions(self) -> None:
        with pytest.raises(ValueError, match="short"):
            ledger().book_fill(date(2025, 1, 6), fill("VCN", "sell", 1, "40"), "CAD", "D-1", "")

    def test_conversions_are_not_trades(self) -> None:
        lg = ledger()
        rec = ConversionRecord(date(2025, 1, 6), "fx_funding", "CAD", D("1350.00"), "USD",
                               D("999.50"), 1.3507, 1.35, D("0.68"))  # fmt: skip
        lg.book_conversion(rec)
        assert lg.cash == {"CAD": D("98650.00"), "USD": D("999.50")}
        assert not lg.trades


class TestDistributions:
    def setup_method(self) -> None:
        self.lg = ledger()
        self.lg.book_fill(date(2025, 3, 20), fill("VCN", "buy", 100, "40"), "CAD", "D-1", "band")
        self.lg.close_day(date(2025, 3, 20))
        self.lg.close_day(date(2025, 3, 21))

    def test_entitlement_from_the_previous_session(self) -> None:
        """A buy on the ex-date is not entitled; the holding before it is."""
        self.lg.book_fill(date(2025, 3, 24), fill("VCN", "buy", 50, "40"), "CAD", "D-2", "band")
        rec = self.lg.book_distribution(
            cash("VCN", date(2025, 3, 24), "0.25"), date(2025, 3, 24), "CAD"
        )
        assert rec is not None and rec.shares == 100 and rec.net == D("25.00")

    def test_paid_on_the_pay_date(self) -> None:
        act = cash("VCN", date(2025, 3, 24), "0.25", pay=date(2025, 3, 31))
        self.lg.book_distribution(act, date(2025, 3, 24), "CAD")
        before = self.lg.cash["CAD"]
        assert not self.lg.collect(date(2025, 3, 28))
        assert [r.net for r in self.lg.collect(date(2025, 3, 31))] == [D("25.00")]
        assert self.lg.cash["CAD"] == before + D("25.00") and not self.lg.receivables()

    def test_no_pay_date_stays_receivable_and_counts_in_nav(self) -> None:
        self.lg.book_distribution(cash("VCN", date(2025, 3, 24), "0.25"), date(2025, 3, 24), "CAD")
        assert len(self.lg.receivables()) == 1
        val = self.lg.value({"VCN": 40.0}, CUR, 1.35)
        assert val.receivables_cad == pytest.approx(25.0)
        assert val.nav == pytest.approx(float(self.lg.cash["CAD"]) + 4000.0 + 25.0)

    def test_overdue_30_days_after_the_ex_date(self) -> None:
        self.lg.book_distribution(cash("VCN", date(2025, 3, 24), "0.25"), date(2025, 3, 24), "CAD")
        assert not self.lg.overdue(date(2025, 4, 22), 30)
        assert len(self.lg.overdue(date(2025, 4, 23), 30)) == 1

    def test_us_listed_withholding(self) -> None:
        lg = ledger()
        lg.book_fill(date(2024, 12, 19), fill("PDBC", "buy", 1000, "14"), "USD", "D-1", "band")
        lg.close_day(date(2024, 12, 19))
        rec = lg.book_distribution(
            cash("PDBC", date(2024, 12, 20), "0.6016"), date(2024, 12, 20), "USD"
        )
        assert rec is not None
        assert (rec.gross, rec.withholding, rec.net) == (D("601.60"), D("90.24"), D("511.36"))

    def test_late_action_uses_the_position_history(self) -> None:
        """Data seen a week late: the entitlement still uses the holding at the ex-date."""
        self.lg.close_day(date(2025, 3, 24))
        self.lg.book_fill(date(2025, 3, 25), fill("VCN", "sell", 100, "41"), "CAD", "D-2", "band")
        self.lg.close_day(date(2025, 3, 25))
        rec = self.lg.book_distribution(
            cash("VCN", date(2025, 3, 24), "0.25"), date(2025, 3, 31), "CAD"
        )
        assert rec is not None and rec.shares == 100

    def test_reinvested_moves_no_cash(self) -> None:
        before = dict(self.lg.cash)
        act = CorporateAction(symbol="VCN", ex_date=date(2025, 12, 31), action_type="reinvested",
                              amount=D("0.0450"))  # fmt: skip
        rec = self.lg.book_reinvested(act, date(2025, 12, 31))
        assert rec is not None and rec.amount == D("4.50") and rec.shares == 100
        assert self.lg.cash == before and not self.lg.receivables()


class TestSplits:
    def test_on_time_and_late_give_the_same_holding(self) -> None:
        on_time, late = ledger(), ledger()
        split = CorporateAction(symbol="VCN", ex_date=date(2025, 6, 16), action_type="split",
                                amount=D("2"))  # fmt: skip
        for lg in (on_time, late):
            lg.book_fill(date(2025, 6, 12), fill("VCN", "buy", 101, "40"), "CAD", "D-1", "band")
            lg.close_day(date(2025, 6, 13))
        on_time.book_split(split, date(2025, 6, 16), 20.0, "CAD")
        for lg in (on_time, late):  # a post-split buy on the ex-date
            lg.book_fill(date(2025, 6, 16), fill("VCN", "buy", 10, "20"), "CAD", "D-2", "band")
        late.book_split(split, date(2025, 6, 18), 20.0, "CAD")
        assert on_time.positions == late.positions == {"VCN": 212}

    def test_reverse_split_pays_cash_in_lieu(self) -> None:
        lg = ledger()
        lg.book_fill(date(2025, 6, 12), fill("VCN", "buy", 101, "10", "0"), "CAD", "D-1", "band")
        lg.close_day(date(2025, 6, 13))
        before = lg.cash["CAD"]
        rev = CorporateAction(symbol="VCN", ex_date=date(2025, 6, 16), action_type="split",
                              amount=D("0.5"))  # fmt: skip
        rec = lg.book_split(rev, date(2025, 6, 16), 20.0, "CAD")
        assert lg.positions == {"VCN": 50} and rec.cash_in_lieu == D("10.00")
        assert lg.cash["CAD"] == before + D("10.00")


class TestInterest:
    def test_postings_are_cash_and_accruals_count_in_nav(self) -> None:
        lg = ledger("50000")
        lg.book_interest(InterestRecord(date(2025, 2, 3), "CAD", D("81.37"), "2025-01"))
        lg.set_accrued_interest({"CAD": D("12.10"), "USD": D("0")})
        assert lg.cash["CAD"] == D("50081.37")
        val = lg.value({}, CUR, 1.35)
        assert val.accrued_interest_cad == pytest.approx(12.10)
        assert val.nav == pytest.approx(50081.37 + 12.10)
