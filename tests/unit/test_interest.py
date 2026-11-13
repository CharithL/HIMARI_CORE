"""Interest on cash and settlement cycles (amendment v1.6)."""

from datetime import date
from pathlib import Path

import pytest
import yaml

from himari.accounting.interest import InterestModel, SettlementCycles, add_sessions

ROOT = Path(__file__).parents[2]
RAW = yaml.safe_load((ROOT / "config" / "costs_v1.yaml").read_text(encoding="utf-8"))
MODEL = InterestModel.from_config(RAW["cash_interest"])
CYCLES = SettlementCycles.from_config(RAW["settlement_days"])


class TestInterestModel:
    def test_cad_rate_is_benchmark_less_spread(self) -> None:
        assert MODEL.annual_rate("CAD", 4.0, 150_000.0) == pytest.approx(0.035)

    def test_rate_scales_below_the_full_rate_nav(self) -> None:
        assert MODEL.annual_rate("CAD", 4.0, 72_000.0) == pytest.approx(0.035 * 0.72)

    def test_usd_earns_nothing_in_stage1(self) -> None:
        assert MODEL.annual_rate("USD", 4.0, 150_000.0) == 0.0

    def test_rate_never_negative(self) -> None:
        assert MODEL.annual_rate("CAD", 0.25, 150_000.0) == 0.0  # 0.25% - 0.50%

    def test_first_13000_cad_earns_nothing(self) -> None:
        assert MODEL.interest("CAD", 12_000.0, 4.0, 150_000.0, 30) == 0.0
        # 100,000 above the tier at 3.5% for 30 days of 365
        assert MODEL.interest("CAD", 113_000.0, 4.0, 150_000.0, 30) == pytest.approx(
            100_000 * 0.035 * 30 / 365
        )

    def test_only_daily_accrual_and_monthly_posting(self) -> None:
        with pytest.raises(ValueError, match="daily accrual"):
            InterestModel.from_config({**RAW["cash_interest"], "posting": "quarterly"})


class TestSettlement:
    @pytest.mark.parametrize(
        ("exchange", "trade", "days"),
        [
            ("TSX", date(2017, 9, 1), 3),
            ("TSX", date(2017, 9, 5), 2),
            ("TSX", date(2024, 5, 24), 2),
            ("TSX", date(2024, 5, 27), 1),
            ("NASDAQ", date(2024, 5, 27), 2),  # the US moved a day later
            ("NASDAQ", date(2024, 5, 28), 1),
        ],
    )
    def test_cycle_by_trade_date(self, exchange: str, trade: date, days: int) -> None:
        assert CYCLES.days(exchange, trade) == days

    def test_the_2017_transition_settled_both_cycles_together(self) -> None:
        """T+3 trades of Fri 1 Sep and T+2 trades of Tue 5 Sep 2017 (after Labour Day) both
        settled on Thu 7 Sep."""
        assert CYCLES.settle_date("TSX", "XTSE", date(2017, 9, 1)) == date(2017, 9, 7)
        assert CYCLES.settle_date("TSX", "XTSE", date(2017, 9, 5)) == date(2017, 9, 7)

    def test_settlement_skips_holidays_on_the_exchange_calendar(self) -> None:
        assert CYCLES.settle_date("NASDAQ", "XNAS", date(2024, 5, 24)) == date(2024, 5, 29)
        assert add_sessions("XTSE", date(2025, 6, 30), 1) == date(2025, 7, 2)  # Canada Day

    def test_no_cycle_before_the_first_entry(self) -> None:
        with pytest.raises(ValueError, match="no settlement cycle"):
            CYCLES.days("TSX", date(1999, 12, 31))
