"""Exchange sessions, decision timing, release times and Bank of Canada business days."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from himari.data.calendars import (
    CORRA_RELEASE_ET,
    FX_RELEASE_ET,
    BocBusinessDays,
    HolidayFile,
    corra_release_utc,
    last_completed_session,
    load_boc_holidays,
    release_utc,
    review_record,
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


def weekdays(
    start: date, end: date, closed: frozenset[date] | set[date] = frozenset()
) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in closed:
            out.append(d)
        d += timedelta(days=1)
    return out


# The record for 2025: the Bank published every weekday except these
CLOSED_2025 = {date(2025, 7, 1), date(2025, 11, 11), date(2025, 12, 25), date(2025, 12, 26)}
RECORD = weekdays(date(2025, 1, 2), date(2025, 12, 31), CLOSED_2025)
FORWARD = HolidayFile(frozenset({date(2026, 1, 1), date(2026, 2, 16)}), frozenset({2026}))
CAL = BocBusinessDays(RECORD, FORWARD)


class TestBocBusinessDays:
    def test_remembrance_day_is_not_a_business_day(self) -> None:
        assert not CAL.is_business_day(date(2025, 11, 11))  # the TSX is open that day
        assert CAL.next_business_day(date(2025, 11, 10)) == date(2025, 11, 12)

    def test_next_business_day_skips_weekend_and_closures(self) -> None:
        assert CAL.next_business_day(date(2025, 12, 24)) == date(2025, 12, 29)

    def test_forward_file_decides_after_the_record(self) -> None:
        assert CAL.next_business_day(date(2025, 12, 31)) == date(2026, 1, 2)
        assert not CAL.is_business_day(date(2026, 2, 16))

    def test_record_wins_over_the_forward_file(self) -> None:
        record = [*RECORD, date(2026, 1, 1)]  # suppose the Bank did publish on Jan 1
        cal = BocBusinessDays(record, FORWARD)
        assert cal.is_business_day(date(2026, 1, 1))

    def test_unknown_beyond_record_and_file(self) -> None:
        assert BocBusinessDays(RECORD).next_business_day(date(2025, 12, 31)) is None
        assert CAL.known(date(2027, 3, 1)) is None

    def test_count_skips_closures(self) -> None:
        assert CAL.count_between(date(2025, 6, 27), date(2025, 7, 3)) == 3

    def test_count_beyond_coverage_counts_weekdays(self) -> None:
        cal = BocBusinessDays(RECORD)
        assert cal.count_between(date(2025, 12, 31), date(2026, 1, 9)) == 7

    def test_corra_release_next_business_day_1100_et(self) -> None:
        observed = datetime(2026, 1, 5, tzinfo=UTC)
        got = corra_release_utc(date(2025, 6, 30), CAL, observed)
        assert got == release_utc(date(2025, 7, 2), CORRA_RELEASE_ET)

    def test_corra_release_unknown_uses_observation_time(self) -> None:
        observed = datetime(2026, 1, 5, 23, 30, tzinfo=UTC)
        assert corra_release_utc(date(2025, 12, 31), BocBusinessDays(RECORD), observed) == observed


class TestReviewRecord:
    def test_lists_disagreements(self) -> None:
        fx = weekdays(date(2026, 1, 1), date(2026, 2, 27), {date(2026, 1, 1)})
        corra = [d for d in fx if d != date(2026, 1, 2)]  # CORRA missing one FX day
        corra.append(date(2026, 1, 1))  # and CORRA present on a day without FX
        review = review_record(fx, corra, FORWARD, date(2026, 1, 1), date(2026, 2, 27))
        assert review.fx_only == [date(2026, 1, 2)]
        assert review.corra_only == [date(2026, 1, 1)]
        assert review.file_closed_but_published == [date(2026, 1, 1), date(2026, 2, 16)]
        assert review.file_open_but_unpublished == []

    def test_trailing_fx_day_awaiting_corra_is_not_listed(self) -> None:
        fx = weekdays(date(2026, 3, 2), date(2026, 3, 6))
        review = review_record(fx, fx[:-1], HolidayFile(), date(2026, 3, 2), date(2026, 3, 6))
        assert review.fx_only == []

    def test_file_open_but_nothing_published(self) -> None:
        fx = weekdays(date(2026, 3, 2), date(2026, 3, 6), {date(2026, 3, 4)})
        forward = HolidayFile(frozenset(), frozenset({2026}))
        review = review_record(fx, fx, forward, date(2026, 3, 2), date(2026, 3, 6))
        assert review.file_open_but_unpublished == [date(2026, 3, 4)]


class TestHolidayFile:
    def test_repo_forward_file(self) -> None:
        forward = load_boc_holidays(ROOT / "config" / "calendars" / "boc_holidays.yaml")
        assert forward.years == {2026, 2027}
        assert date(2026, 11, 11) in forward.holidays  # Remembrance Day: closed
        assert date(2026, 2, 16) in forward.holidays  # Ontario Family Day: closed
        assert date(2026, 4, 6) not in forward.holidays  # Easter Monday: open
        assert date(2026, 6, 24) not in forward.holidays  # Quebec-only: open
        assert all(d.weekday() < 5 for d in forward.holidays)

    def test_covered_year_without_holidays_is_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "h.yaml"
        f.write_text("covered_years: [2030]\nholidays: []\n", encoding="utf-8")
        with pytest.raises(ValueError, match="2030"):
            load_boc_holidays(f)
