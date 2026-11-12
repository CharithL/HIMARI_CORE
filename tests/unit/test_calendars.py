"""Exchange sessions, decision timing, release times and Bank of Canada business days."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from himari.data.calendars import (
    CORRA_RELEASE_ET,
    FX_RELEASE_ET,
    BocBusinessDays,
    corra_release_utc,
    last_completed_session,
    load_boc_holidays,
    release_utc,
    weekly_decision_sessions,
)

ROOT = Path(__file__).parents[2]
UTC = timezone.utc


class TestExchangeSessions:
    def test_early_close_counts_once_closed(self) -> None:
        # XTSE closes at 13:00 ET (18:00 UTC) on 2025-12-24
        assert last_completed_session("XTSE", datetime(2025, 12, 24, 17, 59, tzinfo=UTC)) == date(
            2025, 12, 23
        )
        assert last_completed_session("XTSE", datetime(2025, 12, 24, 18, 0, tzinfo=UTC)) == date(
            2025, 12, 24
        )

    def test_nasdaq_trades_on_boxing_day(self) -> None:
        as_of = datetime(2025, 12, 27, 12, 0, tzinfo=UTC)
        assert last_completed_session("XNAS", as_of) == date(2025, 12, 26)
        assert last_completed_session("XTSE", as_of) == date(2025, 12, 24)

    def test_naive_time_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            last_completed_session("XTSE", datetime(2025, 1, 2, 12, 0))

    def test_good_friday_moves_decision_to_thursday(self) -> None:
        got = weekly_decision_sessions("XTSE", date(2025, 4, 7), date(2025, 4, 30))
        assert got == [date(2025, 4, 11), date(2025, 4, 17), date(2025, 4, 25)]

    def test_week_cut_by_end_date_has_no_decision(self) -> None:
        got = weekly_decision_sessions("XTSE", date(2025, 12, 15), date(2025, 12, 31))
        assert got == [date(2025, 12, 19), date(2025, 12, 24)]


class TestReleaseTimes:
    def test_fx_release_in_winter_and_summer(self) -> None:
        assert release_utc(date(2025, 1, 15), FX_RELEASE_ET) == datetime(
            2025, 1, 15, 21, 30, tzinfo=UTC
        )
        assert release_utc(date(2025, 7, 15), FX_RELEASE_ET) == datetime(
            2025, 7, 15, 20, 30, tzinfo=UTC
        )


HOLIDAYS = BocBusinessDays(
    [date(2025, 7, 1), date(2025, 11, 11), date(2025, 12, 25), date(2025, 12, 26)], [2025]
)


class TestBocBusinessDays:
    def test_remembrance_day_is_not_a_business_day(self) -> None:
        assert not HOLIDAYS.is_business_day(date(2025, 11, 11))  # the TSX is open that day
        assert HOLIDAYS.next_business_day(date(2025, 11, 10)) == date(2025, 11, 12)

    def test_next_business_day_skips_weekend_and_holidays(self) -> None:
        assert HOLIDAYS.next_business_day(date(2025, 12, 24)) == date(2025, 12, 29)

    def test_unknown_outside_covered_years(self) -> None:
        assert HOLIDAYS.next_business_day(date(2025, 12, 31)) is None

    def test_count_skips_holidays(self) -> None:
        assert HOLIDAYS.count_between(date(2025, 6, 27), date(2025, 7, 3)) == 3

    def test_count_outside_coverage_counts_weekdays(self) -> None:
        assert HOLIDAYS.count_between(date(2025, 12, 31), date(2026, 1, 9)) == 7

    def test_corra_release_next_business_day_1100_et(self) -> None:
        observed = datetime(2026, 1, 1, tzinfo=UTC)
        got = corra_release_utc(date(2025, 6, 30), HOLIDAYS, observed)
        assert got == release_utc(date(2025, 7, 2), CORRA_RELEASE_ET)

    def test_corra_release_unknown_uses_observation_time(self) -> None:
        observed = datetime(2026, 1, 5, 23, 30, tzinfo=UTC)
        assert corra_release_utc(date(2025, 12, 31), HOLIDAYS, observed) == observed


class TestHolidayFile:
    def test_repo_file_loads(self) -> None:
        cal = load_boc_holidays(ROOT / "config" / "calendars" / "boc_holidays.yaml")
        assert not cal.covers(date(2026, 11, 11))  # no year is complete yet

    def test_fixture_calendar_covers_fixture_years(self) -> None:
        cal = load_boc_holidays(ROOT / "tests" / "fixtures" / "market" / "boc_holidays.yaml")
        assert cal.covers(date(2024, 6, 1)) and cal.covers(date(2025, 6, 1))
        assert not cal.is_business_day(date(2025, 4, 21))

    def test_covered_year_without_holidays_is_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "h.yaml"
        f.write_text("covered_years: [2030]\nholidays: []\n", encoding="utf-8")
        with pytest.raises(ValueError, match="2030"):
            load_boc_holidays(f)
