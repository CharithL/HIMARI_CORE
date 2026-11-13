"""Versioned, append-only storage (§5.3).

Raw pulls are kept byte-for-byte under data/raw/. Clean tables live under data/clean/.
A changed value is appended as a new version, never written over the old one, and the
as-of view rebuilds what was known at a time by filtering released_at_utc.
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from himari.core.types import Bar, CorporateAction, SeriesObservation


class RawStore:
    """Every pull, stored unchanged under {root}/{source}/{UTC stamp}_{name}."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, source: str, name: str, payload: bytes, observed_at_utc: datetime) -> Path:
        stamp = observed_at_utc.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.root / source / f"{stamp}_{name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as f:
            f.write(payload)
        return path


@dataclass(frozen=True)
class TableSpec:
    name: str
    key: tuple[str, ...]
    values: tuple[str, ...]

    @property
    def columns(self) -> list[str]:
        meta = ["version", "observed_at_utc", "released_at_utc"]
        extra = ["quality_flags"] if self.name == "bars" else []
        return [*self.key, *self.values, *meta, *extra]


BARS = TableSpec(
    "bars",
    key=("symbol", "session_date", "source"),
    values=("open", "high", "low", "close", "volume", "adj_close"),
)
ACTIONS = TableSpec(
    "actions",
    key=("symbol", "ex_date", "action_type", "source"),
    values=("pay_date", "amount", "currency"),
)
SERIES = TableSpec("series", key=("series", "obs_date", "source"), values=("value",))


@dataclass(frozen=True)
class AppendResult:
    added: int = 0
    corrected: int = 0
    unchanged: int = 0


def _same(a: Any, b: Any) -> bool:
    a_missing = a is None or (isinstance(a, float) and math.isnan(a))
    b_missing = b is None or (isinstance(b, float) and math.isnan(b))
    if a_missing or b_missing:
        return a_missing and b_missing
    return bool(a == b)


class VersionedStore:
    """Append-only Parquet tables: bars, actions and series (FX and cash rate)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, spec: TableSpec) -> Path:
        return self.root / f"{spec.name}.parquet"

    def load(self, spec: TableSpec) -> pd.DataFrame:
        path = self._path(spec)
        if not path.exists():
            return pd.DataFrame(columns=spec.columns)
        return pd.read_parquet(path)

    def append(
        self, spec: TableSpec, rows: Sequence[dict[str, Any]], observed_at_utc: datetime
    ) -> AppendResult:
        """Append rows. Rows carry their version-1 released_at_utc; corrections use observed_at."""
        existing = self.load(spec)
        latest: dict[tuple[Any, ...], dict[str, Any]] = {}
        for old in existing.sort_values("version").to_dict("records"):
            latest[tuple(old[k] for k in spec.key)] = {str(c): v for c, v in old.items()}

        new_rows: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        added = corrected = unchanged = 0
        for row in rows:
            key = tuple(row[k] for k in spec.key)
            if key in seen:
                raise ValueError(f"duplicate {spec.name} key in one batch: {key}")
            seen.add(key)
            prev = latest.get(key)
            if prev is not None and all(_same(row[v], prev[v]) for v in spec.values):
                unchanged += 1
                continue
            rec = {c: row.get(c) for c in spec.columns}
            rec["observed_at_utc"] = observed_at_utc
            if prev is None:
                rec["version"] = 1
                added += 1
            else:
                rec["version"] = int(prev["version"]) + 1
                rec["released_at_utc"] = observed_at_utc
                corrected += 1
            if rec["released_at_utc"] is None:
                raise ValueError(f"{spec.name} row {key} has no released_at_utc")
            if spec.name == "bars":
                rec["quality_flags"] = row.get("quality_flags") or ""
            new_rows.append(rec)

        if new_rows:
            fresh = pd.DataFrame(new_rows, columns=spec.columns)
            table = fresh if existing.empty else pd.concat([existing, fresh], ignore_index=True)
            self._write(spec, table)
        return AppendResult(added, corrected, unchanged)

    def _write(self, spec: TableSpec, table: pd.DataFrame) -> None:
        table = table.sort_values([*spec.key, "version"], kind="mergesort").reset_index(drop=True)
        for col in ("observed_at_utc", "released_at_utc"):
            table[col] = pd.to_datetime(table[col], utc=True)
        path = self._path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        table.to_parquet(tmp, index=False, engine="pyarrow")
        os.replace(tmp, path)

    def view(self, spec: TableSpec, as_of_utc: datetime | None = None) -> pd.DataFrame:
        """Latest version of each key that was released by as_of_utc (all rows if None)."""
        df = self.load(spec)
        if as_of_utc is not None and not df.empty:
            df = df[df["released_at_utc"] <= pd.Timestamp(as_of_utc)]
        return latest_versions(df, spec)

    def bars(self, source: str, as_of_utc: datetime | None = None) -> dict[str, list[Bar]]:
        return frame_bars(self.view(BARS, as_of_utc), source)

    def actions(
        self, source: str, as_of_utc: datetime | None = None
    ) -> dict[str, list[CorporateAction]]:
        return frame_actions(self.view(ACTIONS, as_of_utc), source)

    def series(
        self, series: str, as_of_utc: datetime | None = None, source: str | None = None
    ) -> list[SeriesObservation]:
        return frame_series(self.view(SERIES, as_of_utc), series, source)


def latest_versions(df: pd.DataFrame, spec: TableSpec) -> pd.DataFrame:
    """The highest version of each key among the given rows."""
    df = df.sort_values([*spec.key, "version"], kind="mergesort")
    df = df.drop_duplicates(subset=list(spec.key), keep="last")
    return df.reset_index(drop=True)


def frame_bars(df: pd.DataFrame, source: str) -> dict[str, list[Bar]]:
    """Bars of one source from a table view, by symbol in key (date) order."""
    out: dict[str, list[Bar]] = {}
    for rec in df[df["source"] == source].to_dict("records"):
        adj = rec["adj_close"]
        out.setdefault(rec["symbol"], []).append(
            Bar(
                symbol=rec["symbol"],
                session_date=rec["session_date"],
                open=rec["open"],
                high=rec["high"],
                low=rec["low"],
                close=rec["close"],
                volume=int(rec["volume"]),
                source=rec["source"],
                adj_close=None if adj is None or math.isnan(adj) else float(adj),
                version=int(rec["version"]),
                observed_at_utc=rec["observed_at_utc"].to_pydatetime(),
                released_at_utc=rec["released_at_utc"].to_pydatetime(),
            )
        )
    return out


def frame_actions(df: pd.DataFrame, source: str) -> dict[str, list[CorporateAction]]:
    out: dict[str, list[CorporateAction]] = {}
    for rec in df[df["source"] == source].to_dict("records"):
        out.setdefault(rec["symbol"], []).append(
            CorporateAction.model_validate(
                {
                    "symbol": rec["symbol"],
                    "ex_date": rec["ex_date"],
                    "pay_date": rec["pay_date"],
                    "action_type": rec["action_type"],
                    "amount": Decimal(rec["amount"]),
                    "currency": rec["currency"],
                    "source": rec["source"],
                    "version": int(rec["version"]),
                    "observed_at_utc": rec["observed_at_utc"].to_pydatetime(),
                    "released_at_utc": rec["released_at_utc"].to_pydatetime(),
                }
            )
        )
    return out


def frame_series(
    df: pd.DataFrame, series: str, source: str | None = None
) -> list[SeriesObservation]:
    df = df[df["series"] == series]
    if source is not None:
        df = df[df["source"] == source]
    return [
        SeriesObservation(
            series=rec["series"],
            obs_date=rec["obs_date"],
            value=rec["value"],
            source=rec["source"],
            version=int(rec["version"]),
            observed_at_utc=rec["observed_at_utc"].to_pydatetime(),
            released_at_utc=rec["released_at_utc"].to_pydatetime(),
        )
        for rec in df.sort_values("obs_date", kind="mergesort").to_dict("records")
    ]


def bar_row(bar: Bar, released_at_utc: datetime) -> dict[str, Any]:
    return {
        "symbol": bar.symbol,
        "session_date": bar.session_date,
        "source": bar.source,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "adj_close": math.nan if bar.adj_close is None else bar.adj_close,
        "released_at_utc": released_at_utc,
    }


def action_row(action: CorporateAction, released_at_utc: datetime) -> dict[str, Any]:
    return {
        "symbol": action.symbol,
        "ex_date": action.ex_date,
        "action_type": action.action_type,
        "source": action.source,
        "pay_date": action.pay_date,
        "amount": str(action.amount),
        "currency": None if action.currency is None else action.currency.value,
        "released_at_utc": released_at_utc,
    }


def series_row(obs: SeriesObservation, released_at_utc: datetime) -> dict[str, Any]:
    return {
        "series": obs.series,
        "obs_date": obs.obs_date,
        "source": obs.source,
        "value": obs.value,
        "released_at_utc": released_at_utc,
    }


def published_dates(store: VersionedStore, series_names: Sequence[str]) -> list[date]:
    """All dates with an observation in any of the series, across sources and versions."""
    df = store.load(SERIES)
    if df.empty:
        return []
    return sorted(set(df[df["series"].isin(list(series_names))]["obs_date"]))
