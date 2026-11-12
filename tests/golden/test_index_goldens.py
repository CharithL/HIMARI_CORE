"""Golden tests for the CAD total-return index (§5.5, v1.2) and the cash index (§5.6).

Expected values are computed by hand from the formulas in the build guide.
"""

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from himari.accounting.indices import cash_index, total_return_index
from himari.core.types import Bar, CorporateAction, Currency, QualityFlag, SeriesObservation
from himari.data.calendars import BocBusinessDays
from himari.data.sources import CsvSource

UTC = timezone.utc
D0 = date(2025, 1, 6)


def days(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def bars(closes: Sequence[float], dates: Sequence[date] | None = None) -> list[Bar]:
    ds = dates or days(len(closes))
    return [
        Bar(
            symbol="X",
            session_date=d,
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1,
            source="t",
            released_at_utc=datetime(d.year, d.month, d.day, 23, 30, tzinfo=UTC),
        )
        for d, c in zip(ds, closes, strict=True)
    ]


def cash(day: date, amount: str) -> CorporateAction:
    return CorporateAction(symbol="X", ex_date=day, action_type="cash", amount=Decimal(amount))


def split(day: date, ratio: str) -> CorporateAction:
    return CorporateAction(symbol="X", ex_date=day, action_type="split", amount=Decimal(ratio))


def fx(day: date, value: float) -> SeriesObservation:
    release = datetime(day.year, day.month, day.day, 21, 30, tzinfo=UTC)
    return SeriesObservation(
        series="FXUSDCAD", obs_date=day, value=value, source="t", released_at_utc=release
    )


class TestCadTotalReturn:
    def test_price_only(self) -> None:
        idx = total_return_index(bars([100.0, 102.0, 99.96]), [], Currency.CAD)
        assert math.isnan(idx.returns[0])
        assert idx.returns[1] == pytest.approx(0.02, abs=1e-15)
        assert idx.returns[2] == pytest.approx(-0.02, abs=1e-15)
        assert list(idx.wealth) == pytest.approx([1.0, 1.02, 0.9996], abs=1e-15)

    def test_cash_distribution_on_ex_date(self) -> None:
        d = days(3)
        idx = total_return_index(bars([100.0, 101.0, 100.5], d), [cash(d[2], "1.0")], Currency.CAD)
        assert idx.returns[2] == pytest.approx(101.5 / 101.0 - 1, abs=1e-15)

    def test_two_for_one_split(self) -> None:
        d = days(2)
        idx = total_return_index(bars([100.0, 50.5], d), [split(d[1], "2")], Currency.CAD)
        assert idx.returns[1] == pytest.approx(0.01, abs=1e-15)

    def test_split_and_distribution_same_day(self) -> None:
        """§5.5 edge case: r = S * (P_t + D_t) / P_{t-1} - 1 = 2 * (49.75 + 0.5) / 100 - 1."""
        d = days(2)
        acts = [split(d[1], "2"), cash(d[1], "0.5")]
        idx = total_return_index(bars([100.0, 49.75], d), acts, Currency.CAD)
        assert idx.returns[1] == pytest.approx(0.005, abs=1e-15)
        assert idx.wealth[1] == pytest.approx(1.005, abs=1e-15)

    def test_action_on_missing_bar_applies_to_next_session(self) -> None:
        d = [date(2025, 1, 6), date(2025, 1, 8)]  # no bar on the 7th, the ex-date
        idx = total_return_index(
            bars([100.0, 99.0], d), [cash(date(2025, 1, 7), "1.0")], Currency.CAD
        )
        assert idx.returns[1] == pytest.approx(0.0, abs=1e-15)

    def test_cad_fund_ignores_fx(self) -> None:
        """CAD-listed funds already include currency effects: never convert twice."""
        d = days(2)
        idx = total_return_index(
            bars([10.0, 10.1], d), [], Currency.CAD, [fx(d[0], 1.3), fx(d[1], 1.4)]
        )
        assert idx.returns[1] == pytest.approx(0.01, abs=1e-15)

    def test_released_at_is_latest_input(self) -> None:
        d = days(2)
        late = cash(d[1], "1.0").model_copy(
            update={"released_at_utc": datetime(2025, 2, 1, tzinfo=UTC)}
        )
        idx = total_return_index(bars([100.0, 100.0], d), [late], Currency.CAD)
        assert idx.released_at[1] == datetime(2025, 2, 1, tzinfo=UTC)

    def test_rejects_unsorted_or_non_positive(self) -> None:
        d = days(2)
        with pytest.raises(ValueError, match="increasing"):
            total_return_index(bars([1.0, 1.0], [d[1], d[0]]), [], Currency.CAD)
        with pytest.raises(ValueError, match="positive"):
            total_return_index(bars([1.0, 0.0], d), [], Currency.CAD)


class TestUsdTotalReturn:
    def test_fx_conversion(self) -> None:
        """(1 + r_loc) * X_t / X_{t-1} - 1 = 1.01 * 1.01 - 1."""
        d = days(2)
        idx = total_return_index(
            bars([20.0, 20.2], d), [], Currency.USD, [fx(d[0], 1.35), fx(d[1], 1.3635)]
        )
        assert idx.returns[1] == pytest.approx(0.0201, abs=1e-15)

    def test_fill_forward_within_three_days(self) -> None:
        d = [date(2025, 6, 30), date(2025, 7, 1)]  # no Bank of Canada rate on Canada Day
        idx = total_return_index(bars([20.0, 20.2], d), [], Currency.USD, [fx(d[0], 1.36)])
        assert idx.returns[1] == pytest.approx(0.01, abs=1e-15)
        assert idx.flags[d[1]] == [QualityFlag.FX_FILLED]

    def test_easter_bridge_books_compounded_return(self) -> None:
        """Thu rate, no rate Fri-Mon, Tue rate: Monday has no return; Tuesday books both days."""
        d = [date(2025, 4, 17), date(2025, 4, 21), date(2025, 4, 22)]
        rates = [fx(d[0], 1.40), fx(d[2], 1.414)]
        idx = total_return_index(bars([20.0, 20.2, 20.402], d), [], Currency.USD, rates)
        assert math.isnan(idx.returns[1]) and math.isnan(idx.wealth[1])
        assert idx.returns[2] == pytest.approx(1.0201 * 1.01 - 1, abs=1e-12)
        assert idx.flags[d[1]] == [QualityFlag.FX_BRIDGED]
        assert idx.flags[d[2]] == [QualityFlag.FX_BRIDGED]
        rate_published = rates[1].released_at_utc
        assert rate_published is not None
        booked = idx.released_at[2]
        assert booked is not None and booked >= rate_published
        assert idx.released_at[1] is None

    def test_outage_longer_than_cap_stops_index(self) -> None:
        d = [date(2025, 4, 10), date(2025, 4, 21), date(2025, 4, 22)]
        rates = [fx(d[0], 1.40), fx(d[2], 1.40)]
        idx = total_return_index(bars([20.0, 20.2, 20.2], d), [], Currency.USD, rates)
        assert idx.flags[d[1]] == [QualityFlag.FX_OUTAGE]
        assert idx.flags[d[2]] == [QualityFlag.FX_OUTAGE]
        assert math.isnan(idx.wealth[2])

    def test_confirmed_outage_is_bridged(self) -> None:
        d = [date(2025, 4, 10), date(2025, 4, 21), date(2025, 4, 22)]
        rates = [fx(d[0], 1.40), fx(d[2], 1.40)]
        idx = total_return_index(
            bars([20.0, 20.2, 20.2], d),
            [],
            Currency.USD,
            rates,
            confirmed_outages={date(2025, 4, 22)},
        )
        assert idx.wealth[2] == pytest.approx(1.01, abs=1e-12)

    def test_index_starts_at_first_usable_rate(self) -> None:
        d = days(2)
        idx = total_return_index(bars([20.0, 21.0], d), [], Currency.USD, [fx(d[1], 1.4)])
        assert math.isnan(idx.wealth[0]) and idx.wealth[1] == 1.0
        assert idx.flags[d[0]] == [QualityFlag.FX_STALE]


def corra(day: date, value: float, released: datetime | None = None) -> SeriesObservation:
    return SeriesObservation(
        series="AVG.INTWO", obs_date=day, value=value, source="t", released_at_utc=released
    )


class TestCashIndex:
    def test_weekend_accrues_calendar_days(self) -> None:
        idx = cash_index([date(2025, 1, 3), date(2025, 1, 6)], [corra(date(2025, 1, 3), 3.65)])
        assert idx.level[1] == pytest.approx(1 + 0.0365 * 3 / 365, abs=1e-15)

    def test_uses_rate_of_previous_session(self) -> None:
        s = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]
        rates = [corra(s[0], 3.65), corra(s[1], 7.30)]
        idx = cash_index(s, rates)
        expected = (1 + 0.0365 / 365) * (1 + 0.0730 / 365)
        assert idx.level[2] == pytest.approx(expected, abs=1e-15)

    def test_point_in_time_cutoffs(self) -> None:
        """CORRA for Fri is released Tue 11:00 ET when Mon is a Bank holiday: Mon uses Thu's."""
        thu, fri, mon = date(2024, 11, 7), date(2024, 11, 8), date(2024, 11, 11)
        rates = [
            corra(thu, 3.65, datetime(2024, 11, 8, 16, 0, tzinfo=UTC)),
            corra(fri, 7.30, datetime(2024, 11, 12, 16, 0, tzinfo=UTC)),
        ]
        cutoffs = [
            datetime(2024, 11, 8, 23, 30, tzinfo=UTC),
            datetime(2024, 11, 11, 23, 30, tzinfo=UTC),
        ]
        idx = cash_index([fri, mon], rates, cutoffs=cutoffs)
        assert idx.rate[1] == 3.65
        assert idx.level[1] == pytest.approx(1 + 0.0365 * 3 / 365, abs=1e-15)

    def test_stale_rate_is_flagged_but_used(self) -> None:
        cal = BocBusinessDays([], [2025])
        s = [date(2025, 1, 6), date(2025, 1, 14), date(2025, 1, 15)]
        idx = cash_index(s, [corra(date(2025, 1, 3), 3.65)], boc_days=cal)
        assert idx.flags[s[2]] == [QualityFlag.RATE_STALE]
        assert not math.isnan(idx.level[2])

    def test_no_rate_leaves_no_value(self) -> None:
        idx = cash_index([date(2025, 1, 6), date(2025, 1, 7)], [])
        assert idx.level[0] == 1.0 and math.isnan(idx.level[1])


class TestFixtureIndexMatchesVendorAdjusted:
    """On the fixture set, W_last / W_first equals the vendor-adjusted ratio (split included)."""

    @pytest.mark.parametrize("symbol", ["VIU", "VGV", "VCN"])
    def test_cad_funds(self, symbol: str) -> None:
        src = CsvSource(Path(__file__).parents[1] / "fixtures" / "market" / "primary")
        span = (date(2024, 1, 1), date(2025, 12, 31))
        b = src.fetch_bars(symbol, *span)
        idx = total_return_index(b, src.fetch_actions(symbol, *span), Currency.CAD)
        first, last = b[0].adj_close, b[-1].adj_close
        assert first is not None and last is not None
        assert idx.wealth[-1] == pytest.approx(last / first, rel=1e-6)
