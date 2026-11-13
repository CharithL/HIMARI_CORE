"""Market data for the backtest: the strategy's point-in-time view and the simulator's market.

`PointInTimeData` is what the strategy sees (rule 2): as of a time T, only rows released by T,
each key at its latest version released by T. Most rows become visible at the daily job time
of their own date, so the view at day d is a prefix of the full history, and the index
functions are prefix-stable (a value at t never depends on rows after t). Rows that break the
prefix are "late": corrections (version > 1), and bars, actions or FX rates released after
their date's daily job. Each late release time starts a new epoch; indices are computed once
per epoch and cut at the decision date, which is exact. CORRA rows are never late: the cash
index applies each session's cutoff itself.

`MarketTruth` is the simulator's market: the final data (latest versions), used for fills,
splits, distribution payments and conversion rates, never for decisions.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import TYPE_CHECKING

import pandas as pd

from himari.accounting.indices import CashIndex, TotalReturnIndex, cash_index, total_return_index
from himari.core.types import Currency
from himari.data.calendars import release_utc
from himari.data.fx import splice_fx
from himari.data.ingest import SeriesNames, boc_calendar
from himari.data.storage import (
    ACTIONS,
    BARS,
    SERIES,
    frame_actions,
    frame_bars,
    frame_series,
    latest_versions,
)

if TYPE_CHECKING:
    from himari.core.config import FrozenConfig
    from himari.core.types import Bar, CorporateAction, SeriesObservation
    from himari.data.calendars import BocBusinessDays, HolidayFile
    from himari.data.storage import TableSpec, VersionedStore


@dataclass(frozen=True)
class Epoch:
    """Every row visible from one late release time to the next, as domain objects."""

    bars: dict[str, list[Bar]]
    crosscheck: dict[str, list[Bar]]
    actions: dict[str, list[CorporateAction]]
    fx: list[SeriesObservation]  # spliced legacy noon rate + FXUSDCAD
    corra: list[SeriesObservation]
    indices: dict[str, TotalReturnIndex]
    cash: dict[str, CashIndex]
    bar_dates: dict[str, list[date]] = field(default_factory=dict)
    fx_dates: list[date] = field(default_factory=list)


@dataclass(frozen=True)
class Snapshot:
    """Rows known at as_of_utc (rule 2), plus the epoch's indices, which callers cut at the
    decision date."""

    as_of_utc: datetime
    bars: dict[str, list[Bar]]
    crosscheck: dict[str, list[Bar]]
    actions: dict[str, list[CorporateAction]]
    fx: list[SeriesObservation]
    corra: list[SeriesObservation]
    indices: dict[str, TotalReturnIndex]
    cash: dict[str, CashIndex]


def _known(rows: Sequence[Bar], as_of: datetime) -> list[Bar]:
    return [r for r in rows if r.released_at_utc is not None and r.released_at_utc <= as_of]


class PointInTimeData:
    def __init__(
        self,
        store: VersionedStore,
        cfg: FrozenConfig,
        primary: str,
        crosscheck: str | None = None,
        forward: HolidayFile | None = None,
        confirmed: Mapping[str, set[date]] | None = None,
        confirmed_actions: Mapping[str, set[date]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.primary = primary
        self.crosscheck_source = crosscheck
        self.eod = time.fromisoformat(cfg.schedule.daily_job_after_et)
        self.names = SeriesNames(cfg.sources.fx_series, cfg.sources.cash_rate_series)
        self.instruments = cfg.stage1.instruments()
        self.confirmed = {k: set(v) for k, v in (confirmed or {}).items()}  # Q4, Q7
        self.confirmed_actions = {k: set(v) for k, v in (confirmed_actions or {}).items()}  # Q10
        self.boc_days: BocBusinessDays = boc_calendar(store, self.names, forward)
        self._frames = {spec.name: store.load(spec) for spec in (BARS, ACTIONS, SERIES)}
        self._late = {spec.name: self._late_mask(spec) for spec in (BARS, ACTIONS, SERIES)}
        times: set[datetime] = set()
        for spec in (BARS, ACTIONS, SERIES):
            late_rows = self._frames[spec.name][self._late[spec.name]]
            times.update(ts.to_pydatetime() for ts in late_rows["released_at_utc"])
        self.late_times: list[datetime] = sorted(times)
        self._epochs: dict[int, Epoch] = {}

    # ---- epochs ------------------------------------------------------------------------
    def _late_mask(self, spec: TableSpec) -> pd.Series:
        df = self._frames[spec.name]
        if df.empty:
            return pd.Series([], dtype=bool)
        late = df["version"].astype(int) > 1
        date_col = {"bars": "session_date", "actions": "ex_date", "series": "obs_date"}[spec.name]
        due = pd.Series(
            pd.to_datetime([release_utc(d, self.eod) for d in df[date_col]], utc=True),
            index=df.index,
        )
        after_due = pd.to_datetime(df["released_at_utc"], utc=True) > due
        if spec.name == "series":
            fx = df["series"].isin([self.names.fx, self.names.fx_legacy])
            return late | (after_due & fx)
        return late | after_due

    def epoch_number(self, as_of: datetime) -> int:
        return bisect.bisect_right(self.late_times, as_of)

    def epoch(self, as_of: datetime) -> Epoch:
        n = self.epoch_number(as_of)
        if n not in self._epochs:
            self._epochs[n] = self._build_epoch(None if n == 0 else self.late_times[n - 1])
        return self._epochs[n]

    def _view(self, spec: TableSpec, until: datetime | None) -> pd.DataFrame:
        df, late = self._frames[spec.name], self._late[spec.name]
        if df.empty:
            return df
        keep = ~late if until is None else (~late) | (df["released_at_utc"] <= pd.Timestamp(until))
        return latest_versions(df[keep.to_numpy()], spec)

    def _build_epoch(self, until: datetime | None) -> Epoch:
        bars_df, actions_df, series_df = (self._view(s, until) for s in (BARS, ACTIONS, SERIES))
        bars = frame_bars(bars_df, self.primary)
        cross = frame_bars(bars_df, self.crosscheck_source) if self.crosscheck_source else {}
        actions = frame_actions(actions_df, self.primary)
        fx = splice_fx(
            frame_series(series_df, self.names.fx_legacy), frame_series(series_df, self.names.fx)
        )
        corra = frame_series(series_df, self.names.cash_rate)
        dq = self.cfg.data_quality
        outages = self.confirmed.get(self.cfg.sources.fx_series, set())
        indices: dict[str, TotalReturnIndex] = {}
        cash: dict[str, CashIndex] = {}
        for inst in self.instruments:
            sym = inst.symbol
            idx = total_return_index(
                bars.get(sym, []),
                actions.get(sym, []),
                inst.currency,
                fx if inst.currency == Currency.USD else (),
                dq.fx_max_age_calendar_days,
                dq.fx_bridge_max_calendar_days,
                outages,
            )
            indices[sym] = idx
            cash[sym] = cash_index(
                idx.dates,
                corra,
                self.boc_days,
                dq.rate_max_age_business_days,
                cutoffs=[release_utc(d, self.eod) for d in idx.dates],
            )
        return Epoch(
            bars=bars,
            crosscheck=cross,
            actions=actions,
            fx=fx,
            corra=corra,
            indices=indices,
            cash=cash,
            bar_dates={s: [b.session_date for b in lst] for s, lst in bars.items()},
            fx_dates=[o.obs_date for o in fx],
        )

    # ---- point-in-time access ----------------------------------------------------------
    def snapshot(self, as_of: datetime) -> Snapshot:
        ep = self.epoch(as_of)

        def known_series(rows: Sequence[SeriesObservation]) -> list[SeriesObservation]:
            return [o for o in rows if o.released_at_utc is not None and o.released_at_utc <= as_of]

        return Snapshot(
            as_of_utc=as_of,
            bars={s: _known(b, as_of) for s, b in ep.bars.items()},
            crosscheck={s: _known(b, as_of) for s, b in ep.crosscheck.items()},
            actions={
                s: [a for a in acts if a.released_at_utc is not None and a.released_at_utc <= as_of]
                for s, acts in ep.actions.items()
            },
            fx=known_series(ep.fx),
            corra=known_series(ep.corra),
            indices=ep.indices,
            cash=ep.cash,
        )

    def last_close(self, symbol: str, day: date, as_of: datetime) -> tuple[date, float] | None:
        """The latest close on or before `day` known at as_of."""
        ep = self.epoch(as_of)
        bars, dates = ep.bars.get(symbol, []), ep.bar_dates.get(symbol, [])
        i = bisect.bisect_right(dates, day) - 1
        while i >= 0:
            b = bars[i]
            if b.released_at_utc is not None and b.released_at_utc <= as_of:
                return b.session_date, b.close
            i -= 1
        return None

    def usd_to_cad(self, day: date, as_of: datetime) -> tuple[date, float] | None:
        """The Bank of Canada valuation rate: the latest rate on or before `day` known at as_of."""
        ep = self.epoch(as_of)
        i = bisect.bisect_right(ep.fx_dates, day) - 1
        while i >= 0:
            o = ep.fx[i]
            if o.released_at_utc is not None and o.released_at_utc <= as_of:
                return o.obs_date, o.value
            i -= 1
        return None

    def new_actions(self, after: datetime, as_of: datetime) -> list[CorporateAction]:
        """Actions (latest versions) that became known in (after, as_of], oldest first."""
        ep = self.epoch(as_of)
        found = [
            a
            for acts in ep.actions.values()
            for a in acts
            if a.released_at_utc is not None and after < a.released_at_utc <= as_of
        ]
        return sorted(found, key=lambda a: (a.ex_date, a.symbol, a.action_type))


class MarketTruth:
    """The simulator's market: final data, latest versions, release times ignored."""

    def __init__(self, store: VersionedStore, source: str, names: SeriesNames) -> None:
        bars = frame_bars(store.view(BARS), source)
        self.bars: dict[str, dict[date, Bar]] = {
            s: {b.session_date: b for b in lst} for s, lst in bars.items()
        }
        self.dates: dict[str, list[date]] = {
            s: [b.session_date for b in lst] for s, lst in bars.items()
        }
        self.traded_value: dict[str, list[float]] = {
            s: [b.close * b.volume for b in lst] for s, lst in bars.items()
        }
        self.actions: dict[date, list[CorporateAction]] = {}
        for acts in frame_actions(store.view(ACTIONS), source).values():
            for a in acts:
                self.actions.setdefault(a.ex_date, []).append(a)
        series = store.view(SERIES)
        fx = splice_fx(frame_series(series, names.fx_legacy), frame_series(series, names.fx))
        self.fx_dates = [o.obs_date for o in fx]
        self.fx_values = [o.value for o in fx]
        corra = frame_series(series, names.cash_rate)
        self.corra_dates = [o.obs_date for o in corra]
        self.corra_values = [o.value for o in corra]

    def corra_on_or_before(self, day: date) -> float | None:
        """CORRA (% per year) for `day`, or the latest earlier value: the interest benchmark."""
        i = bisect.bisect_right(self.corra_dates, day) - 1
        return self.corra_values[i] if i >= 0 else None

    def bar(self, symbol: str, day: date) -> Bar | None:
        return self.bars.get(symbol, {}).get(day)

    def actions_between(self, after: date, through: date) -> list[CorporateAction]:
        """Actions with after < ex_date <= through, by ex-date, symbol and type."""
        found = [a for d, acts in self.actions.items() if after < d <= through for a in acts]
        return sorted(found, key=lambda a: (a.ex_date, a.symbol, a.action_type))

    def median_traded_value(self, symbol: str, day: date, sessions: int) -> float | None:
        """Median close x volume over the `sessions` sessions before `day`."""
        dates = self.dates.get(symbol, [])
        i = bisect.bisect_left(dates, day)
        window = self.traded_value[symbol][max(0, i - sessions) : i] if i else []
        return statistics.median(window) if window else None

    def usd_to_cad(self, day: date, max_age_days: int) -> float | None:
        """The Bank of Canada rate for `day`, or the latest earlier one within max_age_days."""
        i = bisect.bisect_right(self.fx_dates, day) - 1
        if i < 0 or (day - self.fx_dates[i]).days > max_age_days:
            return None
        return self.fx_values[i]
