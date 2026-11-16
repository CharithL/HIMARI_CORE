"""Coverage check for a price source before the baseline (M6 preparation).

For each fund: the first and last bar against the fund's inception date and the end of the
window, exchange sessions with no bar, zero-volume sessions, cash distributions per year and
splits. Nothing is filled in or assumed; the result lists what is there and what is missing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from himari.data.calendars import sessions

if TYPE_CHECKING:
    from himari.core.types import Bar
    from himari.data.sources import DataSource

MISSING_SHOWN = 5  # missing sessions listed by date; the rest are counted
INCEPTION_SLACK_DAYS = 7  # a first bar within a week of inception counts as from inception


@dataclass(frozen=True)
class FundCoverage:
    symbol: str
    inception: date | None
    first_bar: date | None
    last_bar: date | None
    bars: int
    missing_sessions: list[date]
    zero_volume: int
    distributions_by_year: dict[int, int]
    splits: list[tuple[date, Decimal]]
    gaps: list[str] = field(default_factory=list)


def check_fund(
    source: DataSource,
    symbol: str,
    calendar: str,
    inception: date | None,
    start: date,
    end: date,
) -> FundCoverage:
    bars = source.fetch_bars(symbol, start, end)
    actions = source.fetch_actions(symbol, start, end)
    days = sorted({b.session_date for b in bars})
    gaps: list[str] = []
    if not days:
        return FundCoverage(symbol, inception, None, None, 0, [], 0, {}, [], ["no bars at all"])
    first, last = days[0], days[-1]
    expected = sessions(calendar, first, last)
    missing = sorted(set(expected) - set(days))
    zero = sum(1 for b in bars if b.volume == 0)
    dists = Counter(a.ex_date.year for a in actions if a.action_type == "cash")
    splits = sorted((a.ex_date, a.amount) for a in actions if a.action_type == "split")
    if inception is None:
        gaps.append("start date unknown: cannot tell whether the history is complete")
    elif first > inception + timedelta(days=INCEPTION_SLACK_DAYS):
        days_late = (first - inception).days
        gaps.append(f"history starts {first}, {days_late} days after the fund's start {inception}")
    if missing:
        shown = ", ".join(str(d) for d in missing[:MISSING_SHOWN])
        more = f" and {len(missing) - MISSING_SHOWN} more" if len(missing) > MISSING_SHOWN else ""
        gaps.append(f"{len(missing)} {calendar} sessions without a bar: {shown}{more}")
    stale = sessions(calendar, last + timedelta(days=1), end)
    if stale:
        gaps.append(f"last bar {last}: {len(stale)} later {calendar} sessions without a bar")
    if zero:
        gaps.append(f"{zero} sessions with zero volume")
    if bars and not dists:
        gaps.append("no cash distributions in the window (check the issuer's history)")
    return FundCoverage(symbol, inception, first, last, len(bars), missing, zero, dict(dists),
                        splits, gaps)  # fmt: skip


@dataclass(frozen=True)
class CrossAudit:
    """Q6 over a whole window for one fund (v1.10: the baseline waits until every fund passes
    against the IBKR bars). v1.11: a session without a cross-check bar passes only when the
    primary shows zero volume that day (no trades, so no bar is expected), and every such
    session is listed; any other missing bar fails."""

    symbol: str
    shared: int  # primary sessions in the window that the cross-check also has
    mismatches: list[tuple[date, float, float]]  # session, primary close, cross-check close
    missing: list[date]  # primary sessions with volume but no cross-check bar: these fail
    no_trade: list[date]  # primary sessions with zero volume and no cross-check bar: allowed

    @property
    def passed(self) -> bool:
        return self.shared > 0 and not self.mismatches and not self.missing


def cross_check_audit(
    primary: Mapping[str, Sequence[Bar]],
    crosscheck: Mapping[str, Sequence[Bar]],
    symbols: Sequence[str],
    start: date,
    end: date,
    tol: float,
) -> list[CrossAudit]:
    """Every primary close in [start, end] against the cross-check's close on the same session,
    with Q6's tolerance. A session the cross-check lacks is allowed only when the primary bar
    has zero volume (v1.11); otherwise it is missing."""
    out = []
    for sym in symbols:
        cross = {b.session_date: b.close for b in crosscheck.get(sym, [])}
        mismatches: list[tuple[date, float, float]] = []
        missing: list[date] = []
        no_trade: list[date] = []
        shared = 0
        for b in sorted(primary.get(sym, []), key=lambda x: x.session_date):
            if not start <= b.session_date <= end:
                continue
            c = cross.get(b.session_date)
            if c is None:
                (no_trade if b.volume == 0 else missing).append(b.session_date)
                continue
            shared += 1
            if not c > 0 or abs(b.close / c - 1.0) > tol:
                mismatches.append((b.session_date, b.close, c))
        out.append(CrossAudit(sym, shared, mismatches, missing, no_trade))
    return out


def check_coverage(
    source: DataSource,
    calendars: Mapping[str, str],
    inceptions: Mapping[str, date],
    start: date,
    end: date,
    symbols: Sequence[str] | None = None,
) -> list[FundCoverage]:
    """One FundCoverage per symbol (all of `calendars` unless `symbols` is given)."""
    return [
        check_fund(source, s, calendars[s], inceptions.get(s), start, end)
        for s in (symbols if symbols is not None else list(calendars))
    ]
