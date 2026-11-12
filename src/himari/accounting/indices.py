"""CAD total-return index (§5.5, FX bridging per v1.2) and cash index (§5.6)."""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence, Set
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import pairwise
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

from himari.core.types import Bar, CorporateAction, Currency, QualityFlag, SeriesObservation

if TYPE_CHECKING:
    from himari.data.calendars import BocBusinessDays

FloatArray = npt.NDArray[np.float64]


def _action_index(dates: Sequence[date], action: CorporateAction) -> int | None:
    """Session an action applies to: the first on or after its ex-date, if a return spans it."""
    i = bisect.bisect_left(dates, action.ex_date)
    return i if 0 < i < len(dates) else None


def session_adjustments(
    dates: Sequence[date], actions: Sequence[CorporateAction]
) -> tuple[FloatArray, FloatArray]:
    """Split ratio S_t and cash D_t per session.

    An action applies to the first session on or after its ex-date, so a missing ex-date bar
    still adjusts the return that spans it. Actions at or before the first session, or after
    the last, fall outside the return series.
    """
    split = np.ones(len(dates))
    cash = np.zeros(len(dates))
    for a in actions:
        i = _action_index(dates, a)
        if i is None:
            continue
        if a.action_type == "split":
            split[i] *= float(a.amount)
        else:
            cash[i] += float(a.amount)
    return split, cash


def action_sessions(dates: Sequence[date], actions: Sequence[CorporateAction]) -> set[date]:
    return {dates[i] for a in actions if (i := _action_index(dates, a)) is not None}


@dataclass(frozen=True)
class FxBridge:
    """Sessions of a USD fund whose latest FXUSDCAD rate is older than the fill limit."""

    gap: tuple[date, ...]
    from_rate: date  # rate used on the session before the gap
    to_rate: date | None  # first usable rate after the gap; None while the gap is open
    booked_on: date | None  # session that books the compounded return

    @property
    def calendar_days(self) -> int | None:
        return None if self.to_rate is None else (self.to_rate - self.from_rate).days


def _usable_rates(
    dates: Sequence[date], fx: Sequence[SeriesObservation], max_fill_days: int
) -> list[SeriesObservation | None]:
    ordered = sorted(fx, key=lambda o: o.obs_date)
    obs_dates = [o.obs_date for o in ordered]
    usable: list[SeriesObservation | None] = []
    for day in dates:
        i = bisect.bisect_right(obs_dates, day) - 1
        fresh = i >= 0 and (day - ordered[i].obs_date).days <= max_fill_days
        usable.append(ordered[i] if fresh else None)
    return usable


def _bridges(dates: Sequence[date], usable: Sequence[SeriesObservation | None]) -> list[FxBridge]:
    bridges: list[FxBridge] = []
    last_rate: date | None = None
    gap: list[date] = []
    for day, obs in zip(dates, usable, strict=True):
        if obs is None:
            if last_rate is not None:
                gap.append(day)
            continue
        if gap and last_rate is not None:
            bridges.append(FxBridge(tuple(gap), last_rate, obs.obs_date, day))
            gap = []
        last_rate = obs.obs_date
    if gap and last_rate is not None:
        bridges.append(FxBridge(tuple(gap), last_rate, None, None))
    return bridges


def fx_bridges(
    dates: Sequence[date], fx: Sequence[SeriesObservation], max_fill_days: int
) -> list[FxBridge]:
    return _bridges(dates, _usable_rates(dates, fx, max_fill_days))


def is_outage(bridge: FxBridge, max_bridge_days: int, confirmed: Set[date]) -> bool:
    """A bridge longer than the cap is a data outage unless a person confirmed it."""
    days = bridge.calendar_days
    return days is not None and days > max_bridge_days and bridge.to_rate not in confirmed


@dataclass(frozen=True)
class TotalReturnIndex:
    symbol: str
    dates: list[date]
    returns: FloatArray  # r_cad_t; NaN on the first date and on dates without a value
    wealth: FloatArray  # W_t = 1 on the first date with a value
    released_at: list[datetime | None]  # when each value became knowable; None without a value
    flags: dict[date, list[QualityFlag]] = field(default_factory=dict)


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def total_return_index(
    bars: Sequence[Bar],
    actions: Sequence[CorporateAction],
    currency: Currency,
    fx: Sequence[SeriesObservation] = (),
    fx_max_age_days: int = 3,
    fx_bridge_max_days: int = 7,
    confirmed_outages: Set[date] = frozenset(),
) -> TotalReturnIndex:
    """W_t from unadjusted closes, gross distributions, splits and (USD funds) FXUSDCAD.

    USD funds (v1.2): a session whose latest rate is older than fx_max_age_days is inside a gap
    and gets no return of its own (FX_BRIDGED). The first session with a usable rate books the
    local returns across the gap times the FX change (also FX_BRIDGED). A gap whose rates are
    more than fx_bridge_max_days apart is an outage (FX_OUTAGE): unless confirmed, the index
    has no value from there on. released_at is the latest release among the inputs used.
    """
    if not bars:
        return TotalReturnIndex("", [], np.array([]), np.array([]), [])
    symbol = bars[0].symbol
    dates = [b.session_date for b in bars]
    if any(later <= earlier for earlier, later in pairwise(dates)):
        raise ValueError(f"{symbol}: bars must be strictly increasing by date")
    close = np.array([b.close for b in bars], dtype=float)
    if not np.all(np.isfinite(close) & (close > 0)):
        raise ValueError(f"{symbol}: closes must be finite and positive")

    n = len(dates)
    split, cash = session_adjustments(dates, actions)
    r_loc = np.full(n, math.nan)
    r_loc[1:] = split[1:] * (close[1:] + cash[1:]) / close[:-1] - 1.0

    input_release: list[datetime | None] = [b.released_at_utc for b in bars]
    for a in actions:
        i = _action_index(dates, a)
        if i is not None:
            input_release[i] = _later(input_release[i], a.released_at_utc)

    if currency == Currency.CAD:
        wealth = np.ones(n)
        wealth[1:] = np.cumprod(1.0 + r_loc[1:])
        released: list[datetime | None] = []
        running: datetime | None = None
        for rel in input_release:
            running = _later(running, rel)
            released.append(running)
        return TotalReturnIndex(symbol, dates, r_loc, wealth, released)

    usable = _usable_rates(dates, fx, fx_max_age_days)
    outages = [
        b for b in _bridges(dates, usable) if is_outage(b, fx_bridge_max_days, confirmed_outages)
    ]
    first_outage = outages[0] if outages else None

    returns = np.full(n, math.nan)
    wealth = np.full(n, math.nan)
    released = [None] * n
    flags: dict[date, list[QualityFlag]] = {}
    anchor_w: float | None = None
    anchor_x = math.nan
    gross = 1.0
    in_gap = False
    running = None
    for i, (day, obs) in enumerate(zip(dates, usable, strict=True)):
        running = _later(running, input_release[i])
        if anchor_w is not None:
            gross *= 1.0 + r_loc[i]
        if obs is None:
            in_gap = anchor_w is not None
            flags[day] = [QualityFlag.FX_BRIDGED if in_gap else QualityFlag.FX_STALE]
            continue
        if first_outage is not None and day == first_outage.booked_on:
            for gap_day in (*first_outage.gap, day):
                flags[gap_day] = [QualityFlag.FX_OUTAGE]
            break
        running = _later(running, obs.released_at_utc)
        day_flags: list[QualityFlag] = []
        if day > obs.obs_date:
            day_flags.append(QualityFlag.FX_FILLED)
        if anchor_w is None:
            wealth[i] = 1.0
        else:
            if in_gap:
                day_flags.append(QualityFlag.FX_BRIDGED)
            returns[i] = gross * obs.value / anchor_x - 1.0
            wealth[i] = anchor_w * (1.0 + returns[i])
        if day_flags:
            flags[day] = day_flags
        released[i] = running
        anchor_w, anchor_x, gross, in_gap = float(wealth[i]), obs.value, 1.0, False
    return TotalReturnIndex(symbol, dates, returns, wealth, released, flags)


@dataclass(frozen=True)
class CashIndex:
    dates: list[date]
    level: FloatArray  # B_t = 1 on the first date
    rate: FloatArray  # CORRA (% per year) accrued over the period ending at t
    flags: dict[date, list[QualityFlag]] = field(default_factory=dict)


def cash_index(
    sessions: Sequence[date],
    corra: Sequence[SeriesObservation],
    boc_days: BocBusinessDays | None = None,
    max_age_bdays: int = 5,
    cutoffs: Sequence[datetime] | None = None,
) -> CashIndex:
    """B_t = B_{t-1} * (1 + y_{t-1}/100 * Δdays/365); y_{t-1} = latest CORRA on or before t-1.

    With cutoffs (the decision time of each session), only values released by that time are
    used, so B_t never depends on data published after t. A period with no CORRA value at all
    leaves the index without a value from there on (nothing is imputed).
    """
    n = len(sessions)
    if cutoffs is not None and len(cutoffs) != n:
        raise ValueError("cutoffs must align with sessions")
    ordered = sorted(corra, key=lambda o: o.obs_date)
    obs_dates = [o.obs_date for o in ordered]
    level = np.full(n, math.nan)
    rate = np.full(n, math.nan)
    flags: dict[date, list[QualityFlag]] = {}
    if n == 0:
        return CashIndex([], level, rate)
    level[0] = 1.0
    for k in range(1, n):
        prev = sessions[k - 1]
        i = bisect.bisect_right(obs_dates, prev) - 1
        if cutoffs is not None:
            while i >= 0:
                rel = ordered[i].released_at_utc
                if rel is not None and rel <= cutoffs[k]:
                    break
                i -= 1
        if i < 0:
            continue
        obs = ordered[i]
        if boc_days is not None and boc_days.count_between(obs.obs_date, prev) > max_age_bdays:
            flags[sessions[k]] = [QualityFlag.RATE_STALE]
        days = (sessions[k] - prev).days
        rate[k] = obs.value
        level[k] = level[k - 1] * (1.0 + obs.value / 100.0 * days / 365.0)
    return CashIndex(list(sessions), level, rate, flags)
