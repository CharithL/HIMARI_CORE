"""Clock abstraction: historical (sim) and live modes.

The same strategy code runs against both clocks.
SimClock replays historical sessions; LiveClock uses wall time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol

import exchange_calendars as xcals

from himari.core.types import Session


class Clock(Protocol):
    def now_utc(self) -> datetime: ...
    def sessions(self, exchange: str, start: date, end: date) -> list[Session]: ...


class SimClock:
    """Replays historical sessions for backtesting."""

    def __init__(self, current_date: date) -> None:
        self._current_date = current_date

    @property
    def current_date(self) -> date:
        return self._current_date

    def advance_to(self, d: date) -> None:
        self._current_date = d

    def now_utc(self) -> datetime:
        return datetime(
            self._current_date.year,
            self._current_date.month,
            self._current_date.day,
            23,
            59,
            59,
            tzinfo=UTC,
        )

    def sessions(self, exchange: str, start: date, end: date) -> list[Session]:
        cal = xcals.get_calendar(exchange)
        sched = cal.sessions_in_range(start.isoformat(), end.isoformat())
        return [Session(date=s.date(), exchange=exchange) for s in sched]


class LiveClock:
    """Uses wall time for paper and live trading."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def sessions(self, exchange: str, start: date, end: date) -> list[Session]:
        cal = xcals.get_calendar(exchange)
        sched = cal.sessions_in_range(start.isoformat(), end.isoformat())
        return [Session(date=s.date(), exchange=exchange) for s in sched]
