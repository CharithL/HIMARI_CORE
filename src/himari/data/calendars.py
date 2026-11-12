"""Exchange sessions (§5.4), Bank of Canada business days and release times (§5.4, v1.1)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
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


def session_close_utc(code: str, day: date) -> datetime:
    close: datetime = get_calendar(code).session_close(pd.Timestamp(day)).to_pydatetime()
    return close


def next_session(code: str, day: date) -> date:
    """First session strictly after day."""
    cal = get_calendar(code)
    nxt: date = cal.date_to_session(pd.Timestamp(day + timedelta(days=1)), direction="next").date()
    return nxt


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


@dataclass(frozen=True)
class HolidayFile:
    """Forward Bank of Canada holidays for years not yet observed (v1.3)."""

    holidays: frozenset[date] = frozenset()
    years: frozenset[int] = frozenset()


class BocBusinessDays:
    """Bank of Canada business days (v1.3).

    Within the publication record (first to last date the Bank published FX or CORRA for),
    a day is a business day exactly when the Bank published. After the record, the forward
    holiday file decides for the years it covers. Beyond both, the day is unknown: it counts
    as a business day when measuring staleness (never under-counted), and next_business_day
    returns None (a value is then treated as released when observed, never earlier).
    """

    def __init__(self, published: Iterable[date] = (), forward: HolidayFile | None = None) -> None:
        self._published = frozenset(published)
        self._first = min(self._published) if self._published else None
        self._last = max(self._published) if self._published else None
        self._forward = forward or HolidayFile()

    @property
    def last_published(self) -> date | None:
        return self._last

    def known(self, day: date) -> bool | None:
        """Whether day is a business day, or None when neither the record nor the file covers it."""
        if self._first is not None and self._last is not None and self._first <= day <= self._last:
            return day in self._published
        if day.year in self._forward.years and (self._last is None or day > self._last):
            return day.weekday() < 5 and day not in self._forward.holidays
        return None

    def is_business_day(self, day: date) -> bool:
        known = self.known(day)
        return day.weekday() < 5 if known is None else known

    def next_business_day(self, day: date) -> date | None:
        d = day + timedelta(days=1)
        while (known := self.known(d)) is not None:
            if known:
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


def load_boc_holidays(path: Path) -> HolidayFile:
    """Forward holiday file: covered_years plus holidays (date, name, basis, source url)."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    years = frozenset(int(y) for y in raw.get("covered_years") or [])
    days = frozenset(date.fromisoformat(str(h["date"])) for h in raw.get("holidays") or [])
    for year in years:
        if not any(d.year == year for d in days):
            raise ValueError(f"{path}: year {year} is marked covered but lists no holidays")
    return HolidayFile(days, years)


@dataclass(frozen=True)
class RecordReview:
    """Disagreements to review by hand (v1.3)."""

    fx_only: list[date]  # the Bank published FX but no CORRA for that day
    corra_only: list[date]  # the Bank published CORRA but no FX for that day
    file_closed_but_published: list[date]  # forward file said closed; the record says open
    file_open_but_unpublished: list[date]  # forward file said open; nothing was published


def review_record(
    fx_dates: Iterable[date],
    corra_dates: Iterable[date],
    forward: HolidayFile,
    start: date,
    end: date,
) -> RecordReview:
    """Cross-check the FX and CORRA records against each other and against the forward file.

    Weekends never count. A trailing day with FX but no CORRA yet is expected (CORRA for a day
    is published the next business day) and is left out.
    """
    fx = {d for d in fx_dates if start <= d <= end}
    corra = {d for d in corra_dates if start <= d <= end}
    record = fx | corra
    fx_only = sorted(fx - corra)
    if fx_only and corra and fx_only[-1] > max(corra):
        fx_only = [d for d in fx_only if d <= max(corra)]
    closed, open_ = [], []
    d = start
    while d <= min(end, max(record, default=start)):
        if d.year in forward.years and d.weekday() < 5:
            file_open = d not in forward.holidays
            if not file_open and d in record:
                closed.append(d)
            elif file_open and d not in record:
                open_.append(d)
        d += timedelta(days=1)
    return RecordReview(fx_only, sorted(corra - fx), closed, open_)


def corra_release_utc(
    obs_date: date, boc_days: BocBusinessDays, observed_at_utc: datetime
) -> datetime:
    """11:00 ET on the next Bank of Canada business day; observation time if that is unknown."""
    nxt = boc_days.next_business_day(obs_date)
    if nxt is None:
        return observed_at_utc
    return release_utc(nxt, CORRA_RELEASE_ET)
