"""Interest on cash (§9.4, amendment v1.6) and settlement cycles.

Cash earns interest per currency on its settled balance: nothing on the first `zero_first`
units, and (benchmark - spread) on the rest, scaled by NAV_USD / full_rate_nav_usd when NAV is
below that size. A currency without a benchmark proxy earns 0 (USD in Stage 1). Interest
accrues on calendar days / 365, like the cash index (§5.6), and is posted monthly.

A trade's cash settles a number of business days after the trade date; the number depends on
the exchange and the trade date (T+3, then T+2 from 2017-09-05, then T+1 from late May 2024).
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from typing import Any

from himari.data.calendars import sessions


@dataclass(frozen=True)
class CurrencyInterest:
    zero_first: float
    spread_below_benchmark: float
    benchmark_proxy: str | None  # "CORRA", or None: the currency earns nothing


@dataclass(frozen=True)
class InterestModel:
    model: str
    currencies: dict[str, CurrencyInterest]
    full_rate_nav_usd: float
    accrual: str
    posting: str

    @classmethod
    def from_config(cls, raw: Mapping[str, Any]) -> InterestModel:
        if raw.get("accrual") != "daily" or raw.get("posting") != "monthly":
            raise ValueError("cash_interest supports daily accrual with monthly posting")
        currencies = {}
        for cur in ("CAD", "USD"):
            c = raw[cur]
            proxy = c.get("benchmark_proxy")
            currencies[cur] = CurrencyInterest(
                zero_first=float(c["zero_first"]),
                spread_below_benchmark=float(c["spread_below_benchmark"]),
                benchmark_proxy=None if proxy in (None, "none") else str(proxy),
            )
        return cls(
            model=str(raw["model"]),
            currencies=currencies,
            full_rate_nav_usd=float(raw["full_rate_nav_usd"]),
            accrual=str(raw["accrual"]),
            posting=str(raw["posting"]),
        )

    def annual_rate(self, currency: str, benchmark_pct: float | None, nav_usd: float) -> float:
        """The rate paid on the balance above zero_first, as a fraction per year."""
        c = self.currencies.get(currency)
        if c is None or c.benchmark_proxy is None or benchmark_pct is None:
            return 0.0
        base = max(0.0, benchmark_pct / 100.0 - c.spread_below_benchmark)
        return base * min(1.0, max(0.0, nav_usd) / self.full_rate_nav_usd)

    def interest(
        self,
        currency: str,
        settled_balance: float,
        benchmark_pct: float | None,
        nav_usd: float,
        days: int,
    ) -> float:
        """Interest earned over `days` calendar days on a settled balance."""
        c = self.currencies.get(currency)
        if c is None or days <= 0:
            return 0.0
        excess = max(0.0, settled_balance - c.zero_first)
        return excess * self.annual_rate(currency, benchmark_pct, nav_usd) * days / 365.0


@dataclass(frozen=True)
class SettlementCycles:
    """Business days from trade date to settlement, by exchange and trade date."""

    by_exchange: dict[str, tuple[tuple[date, int], ...]]

    @classmethod
    def from_config(cls, raw: Mapping[str, Any]) -> SettlementCycles:
        out: dict[str, tuple[tuple[date, int], ...]] = {}
        for exchange, steps in raw.items():
            parsed = sorted((_as_date(s["from"]), int(s["days"])) for s in steps)
            out[str(exchange)] = tuple(parsed)
        return cls(out)

    def days(self, exchange: str, trade_date: date) -> int:
        steps = self.by_exchange[exchange]
        i = bisect.bisect_right([d for d, _ in steps], trade_date) - 1
        if i < 0:
            raise ValueError(f"no settlement cycle for {exchange} trades on {trade_date}")
        return steps[i][1]

    def settle_date(self, exchange: str, calendar: str, trade_date: date) -> date:
        return add_sessions(calendar, trade_date, self.days(exchange, trade_date))


def _as_date(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


@cache
def add_sessions(calendar: str, day: date, n: int) -> date:
    """The n-th session after `day` on the calendar."""
    if n <= 0:
        return day
    later = sessions(calendar, day + timedelta(days=1), day + timedelta(days=7 * n + 14))
    return later[n - 1]
