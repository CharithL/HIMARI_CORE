"""EODHD end-of-day adapter (§5.2 VendorSource): the M6 primary source (EOD Historical plan).

Endpoints (eodhd.com/financial-apis, one API call per request):
- `eod/{TICKER}`: date, open, high, low, close (as traded, raw), adjusted_close (adjusted for
  splits and distributions, used by Q5) and volume (split-adjusted);
- `div/{TICKER}`: date (the ex-date), declarationDate, recordDate, paymentDate, period, value
  (split-adjusted), unadjustedValue (as paid) and currency;
- `splits/{TICKER}`: date and split ("2.000000/1.000000");
- `exchange-symbol-list/{EXCHANGE}`: Code, Name, Country, Exchange, Currency, Type, Isin.

Tickers are SYMBOL.EXCHANGE with TO for the TSX and US for NASDAQ. A class suffix such as CGL.C
is resolved through the exchange's symbol list, never guessed. The vendor does not mark
reinvested (non-cash) distributions, so the Q10 confirmations (§6) still decide those.

The API key comes from VENDOR_API_KEY (§18). It never appears in errors, logs or cache file
names; responses are cached on disk so a rerun spends no calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from himari.core.types import Bar, CorporateAction, Currency
from himari.data.sources import DataSource

BASE_URL = "https://eodhd.com/api"
EXCHANGE_CODES = {"TSX": "TO", "NASDAQ": "US"}
# httpx logs each request URL at INFO, and EODHD takes the key as a query parameter (§18)
logging.getLogger("httpx").setLevel(logging.WARNING)


class EodhdError(RuntimeError):
    """A failed vendor request. Its message never contains the API key."""


class EodhdClient:
    """GET requests with an on-disk cache; `calls` counts the requests sent to the vendor."""

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        cache_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise EodhdError("no API key: set VENDOR_API_KEY in .env (see §18)")
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        self._http = httpx.Client(transport=transport, timeout=timeout)
        self.calls = 0

    def _cache_path(self, request: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / (hashlib.sha256(request.encode()).hexdigest()[:24] + ".json")

    def get(self, path: str, **params: str) -> Any:
        request = path + "?" + urlencode(sorted(params.items()))  # no key in the cache key
        cached = self._cache_path(request)
        if cached is not None and cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))["data"]
        query = {**params, "fmt": "json", "api_token": self._key}
        try:
            resp = self._http.get(f"{self.base_url}/{path}", params=query)
        except httpx.HTTPError as exc:  # the exception text would include the URL and key
            raise EodhdError(f"{path}: {type(exc).__name__}") from None
        self.calls += 1
        if resp.status_code != 200:
            raise EodhdError(f"{path}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise EodhdError(f"{path}: the response is not JSON") from None
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            retrieved = datetime.now(UTC).replace(microsecond=0).isoformat()
            payload = {"request": request, "retrieved_utc": retrieved, "data": data}
            cached.write_text(json.dumps(payload), encoding="utf-8")
        return data


def _date(text: object) -> date | None:
    if not isinstance(text, str) or not text or text.startswith("0000"):
        return None
    return date.fromisoformat(text[:10])


def _ratio(text: str) -> Decimal:
    """'2.000000/1.000000' -> 2 (new shares per old share)."""
    try:
        new, old = (Decimal(x) for x in text.split("/"))
    except (ValueError, InvalidOperation):
        raise EodhdError(f"unreadable split ratio {text!r}") from None
    if new <= 0 or old <= 0:
        raise EodhdError(f"unreadable split ratio {text!r}")
    return new / old


class EodhdSource(DataSource):
    name = "eodhd"

    def __init__(self, client: EodhdClient, exchanges: Mapping[str, str]) -> None:
        """`exchanges` maps each symbol to its listing exchange (TSX or NASDAQ)."""
        self.client = client
        self.exchanges = dict(exchanges)
        self._tickers: dict[str, str] = {}

    def ticker(self, symbol: str) -> str:
        if symbol in self._tickers:
            return self._tickers[symbol]
        code = EXCHANGE_CODES[self.exchanges[symbol]]
        if "." not in symbol:
            ticker = f"{symbol}.{code}"
        else:
            listing = self.client.get(f"exchange-symbol-list/{code}")
            wanted = {symbol, symbol.replace(".", "-"), symbol.replace(".", "_"),
                      symbol.replace(".", "")}  # fmt: skip
            found = sorted({row.get("Code", "") for row in listing} & wanted)
            if len(found) != 1:
                raise EodhdError(f"{symbol}: {len(found)} matching codes in the {code} list")
            ticker = f"{found[0]}.{code}"
        self._tickers[symbol] = ticker
        return ticker

    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        rows = self.client.get(
            f"eod/{self.ticker(symbol)}", **{"from": start.isoformat(), "to": end.isoformat()}
        )
        out = []
        for r in rows:
            adj = r.get("adjusted_close")
            out.append(
                Bar(
                    symbol=symbol,
                    session_date=date.fromisoformat(r["date"]),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=int(r.get("volume") or 0),
                    source=self.name,
                    adj_close=float(adj) if adj is not None else None,
                )
            )
        return out

    def fetch_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        span = {"from": start.isoformat(), "to": end.isoformat()}
        ticker = self.ticker(symbol)
        out: list[CorporateAction] = []
        for d in self.client.get(f"div/{ticker}", **span):
            amount = d.get("unadjustedValue")
            amount = d.get("value") if amount is None else amount
            ex = _date(d.get("date"))
            if ex is None or amount is None:
                raise EodhdError(f"{symbol}: a distribution without an ex-date or amount")
            out.append(
                CorporateAction(
                    symbol=symbol,
                    ex_date=ex,
                    pay_date=_date(d.get("paymentDate")),
                    action_type="cash",
                    amount=Decimal(str(amount)),
                    currency=Currency(d["currency"]) if d.get("currency") else None,
                    source=self.name,
                )
            )
        for s in self.client.get(f"splits/{ticker}", **span):
            ex = _date(s.get("date"))
            if ex is None:
                raise EodhdError(f"{symbol}: a split without a date")
            out.append(
                CorporateAction(
                    symbol=symbol,
                    ex_date=ex,
                    action_type="split",
                    amount=_ratio(str(s["split"])),
                    source=self.name,
                )
            )
        return out
