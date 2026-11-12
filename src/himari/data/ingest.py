"""Load source data into versioned storage, stamping release times (§5.3, §5.4 v1.1, v1.3)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from himari.data.calendars import (
    FX_RELEASE_ET,
    BocBusinessDays,
    HolidayFile,
    corra_release_utc,
    release_utc,
)
from himari.data.fx import LEGACY_FX_FINAL, LEGACY_FX_SERIES
from himari.data.storage import (
    ACTIONS,
    BARS,
    SERIES,
    AppendResult,
    RawStore,
    VersionedStore,
    action_row,
    bar_row,
    published_dates,
    series_row,
)

if TYPE_CHECKING:
    from himari.core.types import SeriesObservation
    from himari.data.sources import BocValetSource, DataSource, SeriesSource


@dataclass(frozen=True)
class SeriesNames:
    fx: str  # FXUSDCAD, CAD per USD
    cash_rate: str  # AVG.INTWO, CORRA in % per year
    fx_legacy: str = LEGACY_FX_SERIES  # legacy U.S. dollar noon rate, CAD per USD (v1.3)

    @property
    def all(self) -> tuple[str, ...]:
        return (self.fx_legacy, self.fx, self.cash_rate)


def ingest_bars(
    source: DataSource,
    store: VersionedStore,
    symbols: Sequence[str],
    start: date,
    end: date,
    observed_at_utc: datetime,
    eod_release_et: time,
) -> dict[str, AppendResult]:
    out: dict[str, AppendResult] = {}
    for sym in symbols:
        rows = [
            bar_row(b, release_utc(b.session_date, eod_release_et))
            for b in source.fetch_bars(sym, start, end)
        ]
        out[sym] = store.append(BARS, rows, observed_at_utc)
    return out


def ingest_actions(
    source: DataSource,
    store: VersionedStore,
    symbols: Sequence[str],
    start: date,
    end: date,
    observed_at_utc: datetime,
    eod_release_et: time,
) -> dict[str, AppendResult]:
    out: dict[str, AppendResult] = {}
    for sym in symbols:
        rows = [
            action_row(a, release_utc(a.ex_date, eod_release_et))
            for a in source.fetch_actions(sym, start, end)
        ]
        out[sym] = store.append(ACTIONS, rows, observed_at_utc)
    return out


def boc_calendar(
    store: VersionedStore,
    names: SeriesNames,
    forward: HolidayFile | None,
    extra_dates: Iterable[date] = (),
) -> BocBusinessDays:
    """Publication record (stored and incoming FX, legacy FX and CORRA dates) + forward file."""
    return BocBusinessDays({*published_dates(store, names.all), *extra_dates}, forward)


def series_release_utc(
    obs: SeriesObservation,
    names: SeriesNames,
    boc_days: BocBusinessDays,
    observed_at_utc: datetime,
    eod_release_et: time,
) -> datetime:
    if obs.series == names.fx:
        return release_utc(obs.obs_date, FX_RELEASE_ET)
    if obs.series == names.fx_legacy:
        return release_utc(obs.obs_date, eod_release_et)
    if obs.series == names.cash_rate:
        return corra_release_utc(obs.obs_date, boc_days, observed_at_utc)
    raise ValueError(f"no release rule for series {obs.series}")


def ingest_series(
    observations: Sequence[SeriesObservation],
    store: VersionedStore,
    names: SeriesNames,
    forward: HolidayFile | None,
    observed_at_utc: datetime,
    eod_release_et: time,
) -> AppendResult:
    """Store one batch of series rows. The batch's own dates join the publication record first,
    so a CORRA value's next business day is found even when it arrives with the same pull."""
    boc_days = boc_calendar(store, names, forward, {o.obs_date for o in observations})
    rows = [
        series_row(o, series_release_utc(o, names, boc_days, observed_at_utc, eod_release_et))
        for o in observations
    ]
    return store.append(SERIES, rows, observed_at_utc)


def _series_to_fetch(names: SeriesNames, start: date) -> list[str]:
    return [s for s in names.all if s != names.fx_legacy or start <= LEGACY_FX_FINAL]


def ingest_series_source(
    source: SeriesSource,
    store: VersionedStore,
    names: SeriesNames,
    forward: HolidayFile | None,
    start: date,
    end: date,
    observed_at_utc: datetime,
    eod_release_et: time,
) -> AppendResult:
    batch = [o for s in _series_to_fetch(names, start) for o in source.fetch_series(s, start, end)]
    return ingest_series(batch, store, names, forward, observed_at_utc, eod_release_et)


def ingest_boc(
    source: BocValetSource,
    raw: RawStore,
    store: VersionedStore,
    names: SeriesNames,
    forward: HolidayFile | None,
    start: date,
    end: date,
    observed_at_utc: datetime,
    eod_release_et: time,
) -> AppendResult:
    """Fetch legacy FX, FX and CORRA from Valet, keep raw responses unchanged, then store."""
    batch: list[SeriesObservation] = []
    for series in _series_to_fetch(names, start):
        payload = source.fetch_raw(series, start, end)
        raw.save(source.name, f"{series}_{start}_{end}.json", payload, observed_at_utc)
        batch += [o for o in source.parse(series, payload) if start <= o.obs_date <= end]
    return ingest_series(batch, store, names, forward, observed_at_utc, eod_release_et)
