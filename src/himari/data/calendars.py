"""Exchange sessions (§5.4), Bank of Canada business days and release times (§5.4, v1.1)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import yaml

ET = ZoneInfo("America/Toronto")
CALENDAR_START = "2000-01-01"

FX_RELEASE_ET = time(16, 30)  # FXUSDCAD for day t, published the same day
CORRA_RELEASE_ET = time(11, 0)  # CORRA for day t, on the next Bank of Canada business day


@cache
def get_calendar(code: str) -> xcals.ExchangeCalendar:
    return xcals.get_calendar(code, start=CALENDAR_START)


def sessions(code: str, start: date, end: date) -> list[date]:
    idx = get_calendar(code).sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [ts.date() for ts in idx]


def last_completed_session(code: str, as_of_utc: datetime) -> date:
    """Most recent session whose close is at or before as_of_utc."""
    if as_of_utc.tzinfo is None:
        raise ValueError("as_of_utc must be timezone-aware")
    cal = get_calendar(code)
    now = pd.Timestamp(as_of_utc)
    local_day = now.tz_convert(cal.tz).normalize().tz_localize(None)
    session = cal.date_to_session(local_day, direction="previous")
    if cal.session_close(session) > now:
        session = cal.previous_session(session)
    return session.date()  # type: ignore[no-any-return]


def weekly_decision_sessions(code: str, start: date, end: date) -> list[date]:
    """Last session of each ISO week: normally Friday, Thursday if Friday is a holiday."""
    week_end = end + timedelta(days=6 - end.weekday())
    last_in_week: dict[tuple[int, int], date] = {}
    for d in sessions(code, start, week_end):
        iso = d.isocalendar()
        last_in_week[(iso.year, iso.week)] = d
    return sorted(d for d in last_in_week.values() if d <= end)


def release_utc(day: date, at_et: time) -> datetime:
    return datetime.combine(day, at_et, tzinfo=ET).astimezone(timezone.utc)


class BocBusinessDays:
    """Bank of Canada business days: weekdays not on the Bank's published holiday list.

    Only covered years are known. Outside them, every weekday counts when measuring staleness
    (so it is never under-counted) and next_business_day returns None (so a value is treated
    as released when it was observed, never earlier).
    """

    def __init__(self, holidays: Iterable[date], covered_years: Iterable[int]) -> None:
        self._holidays = frozenset(holidays)
        self._years = frozenset(covered_years)

    def covers(self, day: date) -> bool:
        return day.year in self._years

    def is_business_day(self, day: date) -> bool:
        return day.weekday() < 5 and not (self.covers(day) and day in self._holidays)

    def next_business_day(self, day: date) -> date | None:
        """First business day after day, or None if that falls outside the covered years."""
        d = day + timedelta(days=1)
        while self.covers(d):
            if self.is_business_day(d):
                return d
            d += timedelta(days=1)
        return None

    def count_between(self, start: date, end: date) -> int:
        """Business days in (start, end]."""
        count = 0
        d = start + timedelta(days=1)
        while d <= end:
            count += self.is_business_day(d)
            d += timedelta(days=1)
        return count


def load_boc_holidays(path: Path) -> BocBusinessDays:
    """Holiday file: covered_years plus holidays, each with date, name, scope and source url."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    covered = [int(y) for y in raw.get("covered_years") or []]
    days = [date.fromisoformat(str(h["date"])) for h in raw.get("holidays") or []]
    for year in covered:
        if not any(d.year == year for d in days):
            raise ValueError(f"{path}: year {year} is marked covered but lists no holidays")
    return BocBusinessDays(days, covered)


def corra_release_utc(
    obs_date: date, boc_days: BocBusinessDays, observed_at_utc: datetime
) -> datetime:
    """11:00 ET on the next Bank of Canada business day; observation time if that is unknown."""
    nxt = boc_days.next_business_day(obs_date)
    if nxt is None:
        return observed_at_utc
    return release_utc(nxt, CORRA_RELEASE_ET)
