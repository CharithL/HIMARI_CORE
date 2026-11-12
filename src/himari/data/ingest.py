"""Load source data into versioned storage, stamping release times (§5.3, §5.4 v1.1)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from himari.data.calendars import (
    FX_RELEASE_ET,
    BocBusinessDays,
    corra_release_utc,
    release_utc,
)
from himari.data.storage import (
    ACTIONS,
    BARS,
    SERIES,
    AppendResult,
    RawStore,
    VersionedStore,
    action_row,
    bar_row,
    series_row,
)

if TYPE_CHECKING:
    from himari.core.types import SeriesObservation
    from himari.data.sources import BocValetSource, DataSource, SeriesSource


@dataclass(frozen=True)
class SeriesNames:
    fx: str  # FXUSDCAD, CAD per USD
    cash_rate: str  # AVG.INTWO, CORRA in % per year


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


def series_release_utc(
    obs: SeriesObservation,
    names: SeriesNames,
    boc_days: BocBusinessDays,
    observed_at_utc: datetime,
) -> datetime:
    if obs.series == names.fx:
        return release_utc(obs.obs_date, FX_RELEASE_ET)
    if obs.series == names.cash_rate:
        return corra_release_utc(obs.obs_date, boc_days, observed_at_utc)
    raise ValueError(f"no release rule for series {obs.series}")


def ingest_series(
    observations: Sequence[SeriesObservation],
    store: VersionedStore,
    names: SeriesNames,
    boc_days: BocBusinessDays,
    observed_at_utc: datetime,
) -> AppendResult:
    rows = [
        series_row(o, series_release_utc(o, names, boc_days, observed_at_utc)) for o in observations
    ]
    return store.append(SERIES, rows, observed_at_utc)


def ingest_series_source(
    source: SeriesSource,
    store: VersionedStore,
    names: SeriesNames,
    boc_days: BocBusinessDays,
    start: date,
    end: date,
    observed_at_utc: datetime,
) -> dict[str, AppendResult]:
    return {
        series: ingest_series(
            source.fetch_series(series, start, end), store, names, boc_days, observed_at_utc
        )
        for series in (names.fx, names.cash_rate)
    }


def ingest_boc(
    source: BocValetSource,
    raw: RawStore,
    store: VersionedStore,
    names: SeriesNames,
    boc_days: BocBusinessDays,
    start: date,
    end: date,
    observed_at_utc: datetime,
) -> dict[str, AppendResult]:
    """Fetch FX and CORRA from Valet, keep each response unchanged in raw/, then store."""
    out: dict[str, AppendResult] = {}
    for series in (names.fx, names.cash_rate):
        payload = source.fetch_raw(series, start, end)
        raw.save(source.name, f"{series}_{start}_{end}.json", payload, observed_at_utc)
        observations = [o for o in source.parse(series, payload) if start <= o.obs_date <= end]
        out[series] = ingest_series(observations, store, names, boc_days, observed_at_utc)
    return out
