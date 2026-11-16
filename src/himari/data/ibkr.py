"""IBKR daily bars (§5.2 IbkrSource): the independent price cross-check for Q6 (amendment v1.10).

Bars come from the TWS API through IB Gateway or TWS (`ib_async`, the optional `broker` extra),
for all nine funds:
- daily bars of type TRADES over regular trading hours. IBKR's TWS API documentation says
  "TRADES data is adjusted for splits, but not dividends", so the adapter reverses the split
  adjustment with the splits already ingested from the primary source, and Q6 compares
  as-traded closes. A disagreement about a split then shows up as a Q6 mismatch;
- contracts are qualified at runtime: TSX funds on primary exchange TSE in CAD, PDBC on NASDAQ
  in USD. A class suffix such as CGL.C is matched through IBKR's own symbol search, never
  guessed;
- IBKR publishes no fixed history depth: `earliest(symbol)` asks for each fund's earliest daily
  bar (reqHeadTimestamp);
- IBKR's bars carry no distribution history, so `fetch_actions` returns nothing.

Market data needs a live, funded IBKR account (USD 500 of equity plus the subscription fees,
per IBKR's market-data pages) with TSX and NASDAQ L1 data; the paper account shares it. The
connection settings are IBKR_HOST, IBKR_PORT and IBKR_CLIENT_ID (§18).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from himari.core.types import Bar, CorporateAction
from himari.data.sources import DataSource

# listing exchange -> IBKR primary exchange and currency
LISTINGS = {"TSX": ("TSE", "CAD"), "NASDAQ": ("NASDAQ", "USD")}
PRICE_DECIMALS = 6


@dataclass(frozen=True)
class IbkrBar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class HistoricalBars(Protocol):
    """The TWS API calls the adapter needs; `IbAsyncBars` implements them over ib_async."""

    def resolve(self, symbol: str, primary_exchange: str, currency: str) -> int:
        """IBKR's contract id for the listing, or an error if there is not exactly one."""
        ...

    def head_date(self, con_id: int) -> date | None: ...

    def daily_trades(self, con_id: int, start: date, end: date) -> list[IbkrBar]: ...


SplitLookup = Callable[[str], Sequence[tuple[date, Decimal]]]  # symbol -> (ex-date, ratio)


class IbkrSource(DataSource):
    name = "ibkr"

    def __init__(
        self, bars: HistoricalBars, exchanges: Mapping[str, str], splits: SplitLookup
    ) -> None:
        """`exchanges` maps each symbol to TSX or NASDAQ; `splits` gives the splits already
        known from the primary source, used to undo IBKR's split adjustment."""
        self.bars = bars
        self.exchanges = dict(exchanges)
        self.splits = splits
        self._con_ids: dict[str, int] = {}

    def con_id(self, symbol: str) -> int:
        if symbol not in self._con_ids:
            primary, currency = LISTINGS[self.exchanges[symbol]]
            self._con_ids[symbol] = self.bars.resolve(symbol, primary, currency)
        return self._con_ids[symbol]

    def earliest(self, symbol: str) -> date | None:
        """IBKR's earliest daily bar for the fund."""
        return self.bars.head_date(self.con_id(symbol))

    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        splits = sorted(self.splits(symbol))
        out = []
        for b in self.bars.daily_trades(self.con_id(symbol), start, end):
            # IBKR divides prices before each later split by its ratio; multiply back
            factor = math.prod(float(r) for ex, r in splits if ex > b.day)
            out.append(
                Bar(
                    symbol=symbol,
                    session_date=b.day,
                    open=round(b.open * factor, PRICE_DECIMALS),
                    high=round(b.high * factor, PRICE_DECIMALS),
                    low=round(b.low * factor, PRICE_DECIMALS),
                    close=round(b.close * factor, PRICE_DECIMALS),
                    volume=round(b.volume / factor),
                    source=self.name,
                )
            )
        return [b for b in out if start <= b.session_date <= end]

    def fetch_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        return []  # IBKR's bars carry no distributions; the issuers' files serve Q10


class IbAsyncBars:
    """HistoricalBars over ib_async, connected to IB Gateway or TWS. Not exercised by the tests
    (no gateway there); its first live run is the smoke test."""

    def __init__(self, host: str, port: int, client_id: int, timeout: float = 60.0) -> None:
        from ib_async import IB  # the optional `broker` extra

        self.ib: Any = IB()
        self.ib.connect(host, port, clientId=client_id, readonly=True, timeout=timeout)
        self._contracts: dict[int, Any] = {}

    def resolve(self, symbol: str, primary_exchange: str, currency: str) -> int:
        from ib_async import Stock

        wanted = {symbol, symbol.replace(".", " "), symbol.replace(".", "-")}
        found: dict[int, Any] = {}
        for desc in self.ib.reqMatchingSymbols(symbol.split(".")[0]) or []:
            c = desc.contract
            same_listing = c.primaryExchange == primary_exchange and c.currency == currency
            if c.secType == "STK" and same_listing and c.symbol in wanted:
                found[c.conId] = c
        if len(found) != 1:
            raise LookupError(f"{symbol}: {len(found)} IBKR contracts on {primary_exchange}")
        con_id = int(next(iter(found)))
        (contract,) = self.ib.qualifyContracts(Stock(conId=con_id, exchange="SMART"))
        self._contracts[con_id] = contract
        return con_id

    def head_date(self, con_id: int) -> date | None:
        ts = self.ib.reqHeadTimeStamp(self._contracts[con_id], "TRADES", True, 1)
        return ts.date() if isinstance(ts, datetime) else None

    def daily_trades(self, con_id: int, start: date, end: date) -> list[IbkrBar]:
        years = max(1, math.ceil((end - start).days / 365.25) + 1)
        bars = self.ib.reqHistoricalData(
            self._contracts[con_id],
            endDateTime=(end + timedelta(days=1)).strftime("%Y%m%d 00:00:00"),
            durationStr=f"{years} Y",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        return [
            IbkrBar(
                b.date, float(b.open), float(b.high), float(b.low), float(b.close), int(b.volume)
            )  # fmt: skip
            for b in bars or []
        ]
