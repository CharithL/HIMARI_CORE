"""Account ledger (§9.4): cash by currency, positions, trades, commissions, conversions,
distributions with withholding, interest, and NAV in CAD.

Money is Decimal, rounded to cents per transaction. Two FX rates are kept apart: conversions
record the broker's execution rate; NAV uses the Bank of Canada valuation rate. Conversions
are tagged and never count as strategy trades.

Distributions: the entitlement is booked on the ex-date from the shares held at the end of
the previous session, net of withholding for US-listed funds, and becomes cash on the pay date.
An entitlement whose data has no pay date stays receivable (it counts in NAV) until a data
version supplies one. An action seen late is booked from the position history, as of its
ex-date. A reinvested distribution (v1.6) is recorded but moves no cash.

Interest (v1.6): the broker accrues it on settled cash and posts it monthly; the ledger books
each posting as reported, and the accrued, unposted amount counts in NAV.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from himari.core.types import CorporateAction, Fill

CENT = Decimal("0.01")


def money(x: float | Decimal) -> Decimal:
    """Round to cents, half to even."""
    return (x if isinstance(x, Decimal) else Decimal(repr(x))).quantize(CENT, ROUND_HALF_EVEN)


@dataclass(frozen=True)
class TradeRecord:
    day: date
    order_id: str
    decision_id: str
    symbol: str
    side: str
    shares: int
    price: Decimal
    currency: str
    gross: Decimal
    commission: Decimal
    reason: str  # band | full_exit | post_trade_limit | halved | safety_cut (v1.6)
    gates: str = ""  # the gates that cut this fund and the scale each applied (v1.6)


@dataclass(frozen=True)
class ConversionRecord:
    day: date
    purpose: str  # fx_funding | usd_surplus
    sell_currency: str
    sell_amount: Decimal
    buy_currency: str
    buy_amount: Decimal
    execution_rate: float  # CAD per USD paid or received, after the conversion cost
    valuation_rate: float  # Bank of Canada CAD per USD for the day
    cost_cad: Decimal


@dataclass(frozen=True)
class DistributionRecord:
    symbol: str
    ex_date: date
    pay_date: date | None
    currency: str
    shares: int
    per_share: Decimal
    gross: Decimal
    withholding: Decimal
    net: Decimal
    booked_on: date
    paid_on: date | None = None


@dataclass(frozen=True)
class ReinvestedRecord:
    """A reinvested year-end distribution: units consolidated, no cash (v1.6)."""

    symbol: str
    ex_date: date
    shares: int
    per_share: Decimal
    amount: Decimal  # notional; no cash moves
    booked_on: date


@dataclass(frozen=True)
class InterestRecord:
    day: date  # posting date
    currency: str
    amount: Decimal
    period: str  # the month it was earned, YYYY-MM


@dataclass(frozen=True)
class SplitRecord:
    symbol: str
    ex_date: date
    ratio: Decimal
    shares_before: int
    shares_added: int
    cash_in_lieu: Decimal
    booked_on: date


@dataclass(frozen=True)
class Valuation:
    cash: dict[str, float]
    positions_cad: dict[str, float]
    receivables_cad: float
    accrued_interest_cad: float
    usd_to_cad: float
    nav: float

    def weights(self) -> dict[str, float]:
        return {s: v / self.nav for s, v in self.positions_cad.items()} if self.nav > 0 else {}


@dataclass
class Ledger:
    cash: dict[str, Decimal]
    withholding_rate: Decimal
    us_listed: frozenset[str]
    positions: dict[str, int] = field(default_factory=dict)
    trades: list[TradeRecord] = field(default_factory=list)
    conversions: list[ConversionRecord] = field(default_factory=list)
    distributions: list[DistributionRecord] = field(default_factory=list)
    reinvested: list[ReinvestedRecord] = field(default_factory=list)
    interest: list[InterestRecord] = field(default_factory=list)
    splits: list[SplitRecord] = field(default_factory=list)
    accrued_interest: dict[str, Decimal] = field(default_factory=dict)
    _history_days: list[date] = field(default_factory=list)
    _history: list[dict[str, int]] = field(default_factory=list)

    # ---- trading -----------------------------------------------------------------------
    def book_fill(
        self, day: date, fill: Fill, currency: str, decision_id: str, reason: str, gates: str = ""
    ) -> TradeRecord:
        gross = money(fill.price * fill.shares)
        commission = money(fill.commission)
        sign = 1 if fill.side == "buy" else -1
        self.cash[currency] = self.cash.get(currency, Decimal(0)) - sign * gross - commission
        held = self.positions.get(fill.symbol, 0) + sign * fill.shares
        if held < 0:
            raise ValueError(f"{fill.symbol}: a fill would leave a short position ({held})")
        self._set_position(fill.symbol, held)
        rec = TradeRecord(
            day=day,
            order_id=fill.order_id,
            decision_id=decision_id,
            symbol=fill.symbol,
            side=fill.side,
            shares=fill.shares,
            price=fill.price,
            currency=currency,
            gross=gross,
            commission=commission,
            reason=reason,
            gates=gates,
        )
        self.trades.append(rec)
        return rec

    def book_conversion(self, rec: ConversionRecord) -> None:
        self.cash[rec.sell_currency] = (
            self.cash.get(rec.sell_currency, Decimal(0)) - rec.sell_amount
        )
        self.cash[rec.buy_currency] = self.cash.get(rec.buy_currency, Decimal(0)) + rec.buy_amount
        self.conversions.append(rec)

    # ---- interest ------------------------------------------------------------------------
    def book_interest(self, rec: InterestRecord) -> None:
        self.cash[rec.currency] = self.cash.get(rec.currency, Decimal(0)) + rec.amount
        self.interest.append(rec)

    def set_accrued_interest(self, accrued: Mapping[str, Decimal]) -> None:
        """The broker's accrued, unposted interest by currency (counts in NAV)."""
        self.accrued_interest = {c: v for c, v in sorted(accrued.items()) if v}

    # ---- corporate actions -------------------------------------------------------------
    def held_before(self, symbol: str, day: date) -> int:
        """Shares held at the end of the last recorded session before `day`."""
        i = bisect.bisect_left(self._history_days, day)
        return self._history[i - 1].get(symbol, 0) if i > 0 else 0

    def book_distribution(
        self, action: CorporateAction, booked_on: date, currency: str
    ) -> DistributionRecord | None:
        shares = self.held_before(action.symbol, action.ex_date)
        if shares == 0:
            return None
        gross = money(action.amount * shares)
        rate = self.withholding_rate if action.symbol in self.us_listed else Decimal(0)
        withholding = money(gross * rate)
        rec = DistributionRecord(
            action.symbol,
            action.ex_date,
            action.pay_date,
            currency,
            shares,
            action.amount,
            gross,
            withholding,
            gross - withholding,
            booked_on,
        )
        self.distributions.append(rec)
        self.collect(booked_on)  # a pay date already past when the data arrived pays now
        return rec

    def book_reinvested(self, action: CorporateAction, booked_on: date) -> ReinvestedRecord | None:
        shares = self.held_before(action.symbol, action.ex_date)
        if shares == 0:
            return None
        rec = ReinvestedRecord(
            action.symbol,
            action.ex_date,
            shares,
            action.amount,
            money(action.amount * shares),
            booked_on,
        )
        self.reinvested.append(rec)
        return rec

    def book_split(
        self, action: CorporateAction, booked_on: date, post_split_price: float, currency: str
    ) -> SplitRecord:
        """New shares for the holding at the ex-date; a fraction is paid as cash in lieu."""
        before = self.held_before(action.symbol, action.ex_date)
        exact = Decimal(before) * action.amount
        whole = int(exact)  # toward zero
        added = whole - before
        lieu = money((exact - whole) * Decimal(repr(post_split_price)))
        self._set_position(action.symbol, self.positions.get(action.symbol, 0) + added)
        self.cash[currency] = self.cash.get(currency, Decimal(0)) + lieu
        rec = SplitRecord(
            action.symbol, action.ex_date, action.amount, before, added, lieu, booked_on
        )
        self.splits.append(rec)
        return rec

    def collect(self, day: date) -> list[DistributionRecord]:
        """Pay every entitlement whose pay date has arrived."""
        paid: list[DistributionRecord] = []
        for i, rec in enumerate(self.distributions):
            if rec.paid_on is None and rec.pay_date is not None and rec.pay_date <= day:
                self.cash[rec.currency] = self.cash.get(rec.currency, Decimal(0)) + rec.net
                self.distributions[i] = replace(rec, paid_on=day)
                paid.append(self.distributions[i])
        return paid

    def receivables(self) -> list[DistributionRecord]:
        return [r for r in self.distributions if r.paid_on is None]

    def overdue(self, day: date, days: int) -> list[DistributionRecord]:
        """Receivables still unpaid `days` calendar days after their ex-date."""
        return [r for r in self.receivables() if day >= r.ex_date + timedelta(days=days)]

    # ---- end of day and valuation ------------------------------------------------------
    def close_day(self, day: date) -> None:
        if self._history_days and day <= self._history_days[-1]:
            raise ValueError(f"days must increase: {day} after {self._history_days[-1]}")
        self._history_days.append(day)
        self._history.append(dict(self.positions))

    def value(
        self, prices: Mapping[str, float], currencies: Mapping[str, str], usd_to_cad: float
    ) -> Valuation:
        """NAV in CAD at the valuation rate: cash, positions at their last close, receivables
        and accrued interest."""
        fx = {"CAD": 1.0, "USD": usd_to_cad}
        positions = {
            s: n * prices[s] * fx[currencies[s]] for s, n in sorted(self.positions.items()) if n
        }
        cash = {c: float(v) for c, v in sorted(self.cash.items())}
        recv = sum(float(r.net) * fx[r.currency] for r in self.receivables())
        accrued = sum(float(v) * fx[c] for c, v in self.accrued_interest.items())
        nav = sum(v * fx[c] for c, v in cash.items()) + sum(positions.values()) + recv + accrued
        if not math.isfinite(nav):
            raise ValueError("NAV is not finite")
        return Valuation(cash, positions, recv, accrued, usd_to_cad, nav)

    def _set_position(self, symbol: str, shares: int) -> None:
        if shares:
            self.positions[symbol] = shares
        else:
            self.positions.pop(symbol, None)
