"""Data source interfaces (build guide §5.2) and their adapters.

DataSource (bars + corporate actions) and SeriesSource (FX and cash-rate series)
are the ports. CsvSource, BocValetSource and VendorSource are adapters.
Sources return rows in the order received so the quality gate (Q3) can see ordering faults.
"""

from __future__ import annotations

import csv
import json
import math
from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx

from himari.core.types import Bar, CorporateAction, SeriesObservation


class DataSource(ABC):
    """Unadjusted daily bars and corporate actions for one instrument."""

    name: str

    @abstractmethod
    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]: ...

    @abstractmethod
    def fetch_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]: ...


class SeriesSource(ABC):
    """Daily scalar series such as FXUSDCAD (CAD per USD) and AVG.INTWO (CORRA, % per year)."""

    name: str

    @abstractmethod
    def fetch_series(self, series: str, start: date, end: date) -> list[SeriesObservation]: ...


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class CsvSource(DataSource, SeriesSource):
    """Manual imports and test fixtures.

    Layout under root: bars/{SYMBOL}.csv, actions/{SYMBOL}.csv, series/{SERIES}.csv.
    """

    def __init__(self, root: Path, name: str = "csv") -> None:
        self.root = root
        self.name = name

    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        bars: list[Bar] = []
        for row in _read_csv(self.root / "bars" / f"{symbol}.csv"):
            d = date.fromisoformat(row["session_date"])
            if not start <= d <= end:
                continue
            adj = row.get("adj_close") or ""
            bars.append(
                Bar(
                    symbol=symbol,
                    session_date=d,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                    source=self.name,
                    adj_close=float(adj) if adj else None,
                )
            )
        return bars

    def fetch_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        actions: list[CorporateAction] = []
        for row in _read_csv(self.root / "actions" / f"{symbol}.csv"):
            ex = date.fromisoformat(row["ex_date"])
            if not start <= ex <= end:
                continue
            actions.append(
                CorporateAction.model_validate(
                    {
                        "symbol": symbol,
                        "ex_date": ex,
                        "pay_date": row.get("pay_date") or None,
                        "action_type": row["type"],
                        "amount": Decimal(row["amount"]),
                        "currency": row.get("currency") or None,
                        "source": self.name,
                    }
                )
            )
        return actions

    def fetch_series(self, series: str, start: date, end: date) -> list[SeriesObservation]:
        out: list[SeriesObservation] = []
        for row in _read_csv(self.root / "series" / f"{series}.csv"):
            d = date.fromisoformat(row["date"])
            if start <= d <= end:
                out.append(
                    SeriesObservation(
                        series=series, obs_date=d, value=float(row["value"]), source=self.name
                    )
                )
        return out


class BocValetSource(SeriesSource):
    """Bank of Canada Valet API: GET {base}/observations/{series}/json.

    fetch_raw returns the response unchanged (for data/raw/); parse turns it into
    observations. Blank values are skipped, never filled (rule 3).
    """

    name = "boc_valet"

    def __init__(
        self,
        base_url: str = "https://www.bankofcanada.ca/valet",
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client

    def fetch_raw(self, series: str, start: date, end: date) -> bytes:
        url = f"{self.base_url}/observations/{series}/json"
        params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        if self._client is not None:
            resp = self._client.get(url, params=params)
        else:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def parse(series: str, raw: bytes) -> list[SeriesObservation]:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or "observations" not in payload:
            raise ValueError(f"Valet response for {series} has no 'observations'")
        out: list[SeriesObservation] = []
        for obs in payload["observations"]:
            cell = obs.get(series)
            raw_value = cell.get("v") if isinstance(cell, dict) else None
            if raw_value is None or raw_value == "":
                continue
            value = float(raw_value)
            if not math.isfinite(value):
                raise ValueError(f"non-finite {series} value on {obs.get('d')}: {raw_value!r}")
            out.append(
                SeriesObservation(
                    series=series,
                    obs_date=date.fromisoformat(obs["d"]),
                    value=value,
                    source=BocValetSource.name,
                )
            )
        return out

    def fetch_series(self, series: str, start: date, end: date) -> list[SeriesObservation]:
        observations = self.parse(series, self.fetch_raw(series, start, end))
        return [o for o in observations if start <= o.obs_date <= end]


class VendorSource(DataSource):
    """Placeholder for the primary vendor (§5.2). The vendor is EODHD (v1.9): its adapter is
    `himari.data.eodhd.EodhdSource`; ingestion through `himari data update` is M6."""

    name = "vendor"

    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        raise NotImplementedError("use himari.data.eodhd.EodhdSource (v1.9); ingestion is M6")

    def fetch_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        raise NotImplementedError("use himari.data.eodhd.EodhdSource (v1.9); ingestion is M6")
