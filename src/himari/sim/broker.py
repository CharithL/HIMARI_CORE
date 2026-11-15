"""SimBroker: the backtest broker and fill model (§10.4).

- Price: the next session's close (sensitivity variant: its open) on the fund's own exchange,
  moved by the cost model: pay up when buying, receive less when selling (§11).
- Participation cap: a fill may not exceed `participation_cap` of the trailing 20-session median
  traded value (close x volume). The rest carries to the fund's next sessions, at most
  `carry_sessions` of them, and is then logged as unfilled.
- No session: the order waits. A halt (zero volume) or a missing bar on a session day: no fill,
  and the session counts toward the carry limit.
- Whole shares only; no short positions; no borrowing: buys are cut to the cash on hand.
- Every order carries an idempotency key; a second order with the same key is refused.
- A USD-listed buy is funded just before it executes: CAD converted to USD for the buy plus
  the buffer (`fx_funding`), at the day's Bank of Canada rate moved by the conversion cost.
- Splits and distributions follow the simulator's market data: shares and open orders change
  on the ex-date; distributions are paid on the pay date, net of withholding for US-listed
  funds (an entitlement without a pay date is never paid by the simulator). A reinvested
  distribution (v1.6) pays nothing.
- Interest (v1.6): each session first accrues interest for the calendar days since the last
  one, on the settled cash at its end (a trade's cash settles after the configured number of
  business days), at the cash_interest model's rate for the NAV the engine last marked; a
  completed month is posted to cash at the first session of the next month.

Within a session: interest, corporate actions, payments, queued conversions, sells, funding,
buys.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import cache
from typing import TYPE_CHECKING

from himari.accounting.ledger import InterestRecord, money
from himari.core.ids import order_idempotency_key
from himari.core.types import Fill, Order, OrderAck, Position
from himari.data.calendars import get_calendar, session_close_utc
from himari.execution.orders import FX_FUNDING, funding_need

if TYPE_CHECKING:
    from himari.execution.costs import CostModel
    from himari.sim.market import MarketTruth

MEDIAN_SESSIONS = 20  # §10.4: trailing 20-session median traded value
PRICE_QUANTUM = Decimal("0.000001")


@cache
def _is_session(calendar: str, day: date) -> bool:
    return bool(get_calendar(calendar).is_session(day.isoformat()))


@dataclass
class SimOrder:
    order_id: str  # the idempotency key
    decision_id: str
    symbol: str
    side: str
    shares: int
    remaining: int
    currency: str
    calendar: str
    reference_price: float  # the last close when submitted, in the fund's currency
    submitted: date
    attempts: int = 0
    status: str = "open"  # open | filled | expired | cancelled
    delayed: int = 0  # sessions passed over by the delay variant (M5 robustness pack)


@dataclass(frozen=True)
class FxFill:
    day: date
    purpose: str
    sell_currency: str
    sell_amount: Decimal
    buy_currency: str
    buy_amount: Decimal
    execution_rate: float  # CAD per USD, after the conversion cost
    valuation_rate: float  # the day's Bank of Canada rate
    cost_cad: Decimal


@dataclass(frozen=True)
class FillDetail:
    fill: Fill
    decision_id: str
    currency: str
    market_price: float  # the close (or open) before costs
    side_bps: float
    stressed: bool
    capped: bool  # the participation cap bound
    cash_limited: bool


@dataclass(frozen=True)
class Unfilled:
    order_id: str
    decision_id: str
    symbol: str
    side: str
    shares: int
    reason: str


@dataclass
class SessionReport:
    day: date
    fills: list[FillDetail] = field(default_factory=list)
    conversions: list[FxFill] = field(default_factory=list)
    unfilled: list[Unfilled] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    paid: list[tuple[str, Decimal, str]] = field(default_factory=list)  # symbol, net, currency
    splits: list[tuple[str, Decimal]] = field(default_factory=list)
    interest: list[InterestRecord] = field(default_factory=list)  # postings (v1.6)
    accrued: dict[str, Decimal] = field(default_factory=dict)  # unposted, at the session end

    @property
    def activity(self) -> bool:
        return bool(self.fills or self.conversions or self.paid or self.splits or self.interest)


@dataclass
class _Entitlement:
    symbol: str
    pay_date: date | None
    currency: str
    net: Decimal


class SimBroker:
    def __init__(
        self,
        truth: MarketTruth,
        costs: CostModel,
        calendars: Mapping[str, str],
        currencies: Mapping[str, str],
        exchanges: Mapping[str, str],
        us_listed: frozenset[str],
        withholding_rate: Decimal,
        participation_cap: float,
        carry_sessions: int,
        funding_buffer: float,
        fx_max_age_days: int,
        cash: Mapping[str, Decimal],
        stressed: Callable[[str, date], bool] = lambda symbol, day: False,
        fill_at: str = "close",
        last_buys: frozenset[str] = frozenset(),
        delay_sessions: int = 0,
    ) -> None:
        if fill_at not in ("close", "open"):
            raise ValueError("fill_at must be 'close' or 'open'")
        if delay_sessions < 0:
            raise ValueError("delay_sessions cannot be negative")
        # robustness pack (§12.3, v1.7): every order waits this many extra sessions of its
        # fund's calendar before it can fill; never part of the baseline
        self.delay_sessions = delay_sessions
        self.truth = truth
        self.costs = costs
        self.calendars = dict(calendars)
        self.currencies = dict(currencies)
        self.exchanges = dict(exchanges)
        self.us_listed = us_listed
        self.withholding_rate = withholding_rate
        self.participation_cap = participation_cap
        self.carry_sessions = carry_sessions
        self.funding_buffer = funding_buffer
        self.fx_max_age_days = fx_max_age_days
        self.stressed = stressed
        self.fill_at = fill_at
        self.last_buys = last_buys  # v1.7: the cash sleeve buys after every other buy
        self.day: date | None = None
        self._cash: dict[str, Decimal] = {c: money(v) for c, v in cash.items()}
        self._positions: dict[str, int] = {}
        self._orders: dict[str, SimOrder] = {}
        self._fills: list[Fill] = []
        self._queued_fx: list[tuple[str, str, str, Decimal]] = []  # purpose, sell, buy, amount
        self._entitlements: list[_Entitlement] = []
        self._last_session: date | None = None
        self._unsettled: list[tuple[date, str, Decimal]] = []  # settle date, currency, cash
        self._accrued: dict[tuple[str, int, int], Decimal] = {}  # currency, year, month
        self._nav_cad: float | None = None
        self._usd_to_cad: float | None = None

    # ---- Broker protocol (§10.2) -------------------------------------------------------
    def positions(self) -> dict[str, Position]:
        return {s: Position(symbol=s, shares=n) for s, n in sorted(self._positions.items())}

    def cash(self) -> dict[str, Decimal]:
        return dict(sorted(self._cash.items()))

    def submit(self, orders: list[Order]) -> list[OrderAck]:
        acks: list[OrderAck] = []
        for o in orders:
            key = o.idempotency_key or order_idempotency_key(o.decision_id, o.symbol)
            if key in self._orders:
                acks.append(
                    OrderAck(
                        order_id=key, decision_id=o.decision_id, symbol=o.symbol, status="duplicate"
                    )
                )
                continue
            if (
                o.symbol not in self.calendars
                or o.side not in ("buy", "sell")
                or o.shares <= 0
                or self.day is None
            ):
                acks.append(
                    OrderAck(
                        order_id=key, decision_id=o.decision_id, symbol=o.symbol, status="rejected"
                    )
                )
                continue
            self._orders[key] = SimOrder(
                order_id=key,
                decision_id=o.decision_id,
                symbol=o.symbol,
                side=o.side,
                shares=o.shares,
                remaining=o.shares,
                currency=self.currencies[o.symbol],
                calendar=self.calendars[o.symbol],
                reference_price=self._last_close(o.symbol, self.day),
                submitted=self.day,
            )
            acks.append(
                OrderAck(
                    order_id=key, decision_id=o.decision_id, symbol=o.symbol, status="submitted"
                )
            )
        return acks

    def fills(self, since_utc: datetime) -> list[Fill]:
        return [
            f for f in self._fills if f.fill_time_utc is not None and f.fill_time_utc >= since_utc
        ]

    def cancel(self, order_id: str) -> None:
        order = self._orders.get(order_id)
        if order is not None and order.status == "open":
            order.status = "cancelled"

    # ---- simulator controls ------------------------------------------------------------
    def open_orders(self) -> list[SimOrder]:
        return [o for o in self._orders.values() if o.status == "open"]

    def order(self, order_id: str) -> SimOrder | None:
        return self._orders.get(order_id)

    def queue_conversion(self, purpose: str, sell: str, buy: str, amount: Decimal) -> None:
        """A conversion carried out at the start of the next session."""
        self._queued_fx.append((purpose, sell, buy, money(amount)))

    def start(self, day: date) -> None:
        """Set the day before the first session (orders submitted now execute after it)."""
        self.day = day

    def mark_nav(self, nav_cad: float, usd_to_cad: float) -> None:
        """The account NAV after a session; it sets the interest tier for the next accrual."""
        self._nav_cad, self._usd_to_cad = nav_cad, usd_to_cad

    def settled_cash(self, currency: str, as_of: date) -> Decimal:
        """Cash whose trades have settled by the end of `as_of`."""
        pending = sum(
            (amt for settle, cur, amt in self._unsettled if cur == currency and settle > as_of),
            Decimal(0),
        )
        return self._cash.get(currency, Decimal(0)) - pending

    def accrued_interest(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for (cur, _, _), amount in sorted(self._accrued.items()):
            out[cur] = out.get(cur, Decimal(0)) + amount
        return {c: money(v) for c, v in out.items()}

    def run_session(self, day: date) -> SessionReport:
        if self.day is not None and day <= self.day:
            raise ValueError(f"sessions must move forward: {day} after {self.day}")
        previous, self.day = self.day, day
        rep = SessionReport(day)
        if previous is not None:
            self._accrue_interest(previous, day)
            self._post_interest(day, rep)
        self._unsettled = [u for u in self._unsettled if u[0] > day]
        self._corporate_actions(day, rep)
        self._pay(day, rep)
        self._run_queued_fx(day, rep)
        due: list[SimOrder] = []
        for o in sorted(self._orders.values(), key=lambda o: (o.symbol, o.order_id)):
            if o.status != "open" or o.submitted >= day or not _is_session(o.calendar, day):
                continue
            if o.delayed < self.delay_sessions:  # the delay variant passes this session over
                o.delayed += 1
                continue
            due.append(o)
        tradable: list[SimOrder] = []
        for o in due:
            bar = self.truth.bar(o.symbol, day)
            o.attempts += 1
            if bar is None or bar.volume == 0:
                rep.waiting.append(
                    f"{o.symbol}: {'no bar' if bar is None else 'halted (zero volume)'}"
                )
            else:
                tradable.append(o)
        for o in tradable:
            if o.side == "sell":
                self._fill(o, day, rep)
        self._fund_usd_buys(
            [o for o in tradable if o.side == "buy" and o.currency == "USD"], day, rep
        )
        buys = [o for o in tradable if o.side == "buy"]
        for o in [o for o in buys if o.symbol not in self.last_buys] + [
            o for o in buys if o.symbol in self.last_buys
        ]:
            self._fill(o, day, rep)
        for o in due:
            if o.status == "open" and o.attempts > self.carry_sessions:
                o.status = "expired"
                rep.unfilled.append(
                    Unfilled(
                        o.order_id,
                        o.decision_id,
                        o.symbol,
                        o.side,
                        o.remaining,
                        f"not filled after {o.attempts} sessions",
                    )
                )
        for c, v in self._cash.items():
            if v < 0:
                raise AssertionError(f"{c} cash went negative ({v}) on {day}")
        rep.accrued = self.accrued_interest()
        return rep

    # ---- internals ---------------------------------------------------------------------
    def _last_close(self, symbol: str, day: date) -> float:
        dates = self.truth.dates.get(symbol, [])
        i = bisect.bisect_right(dates, day) - 1
        bar = self.truth.bar(symbol, dates[i]) if i >= 0 else None
        return math.nan if bar is None else bar.close

    def _corporate_actions(self, day: date, rep: SessionReport) -> None:
        """Actions whose ex-date is after the previous session and on or before today."""
        after = self._last_session or (day - timedelta(days=1))
        self._last_session = day
        for a in self.truth.actions_between(after, day):
            sym, currency = a.symbol, self.currencies.get(a.symbol, "CAD")
            held = self._positions.get(sym, 0)
            if a.action_type == "split":
                exact = Decimal(held) * a.amount
                whole = int(exact)
                bar = self.truth.bar(sym, day)
                if exact != whole and bar is not None:
                    self._cash[currency] = self._cash.get(currency, Decimal(0)) + money(
                        (exact - whole) * Decimal(repr(bar.close))
                    )
                self._set_position(sym, whole)
                for o in self._orders.values():
                    if o.symbol == sym and o.status == "open":
                        o.remaining = int(Decimal(o.remaining) * a.amount)
                        o.reference_price = o.reference_price / float(a.amount)
                rep.splits.append((sym, a.amount))
            elif a.action_type == "cash" and held:
                gross = money(a.amount * held)
                rate = self.withholding_rate if sym in self.us_listed else Decimal(0)
                self._entitlements.append(
                    _Entitlement(sym, a.pay_date, currency, gross - money(gross * rate))
                )

    def _accrue_interest(self, previous: date, day: date) -> None:
        """Interest for each calendar day in (previous, day], on the settled cash at the end of
        `previous`, at the benchmark known for `previous` (as the cash index, §5.6)."""
        model = self.costs.interest
        if model is None or self._nav_cad is None or not self._usd_to_cad:
            return
        nav_usd = self._nav_cad / self._usd_to_cad
        benchmark = self.truth.corra_on_or_before(previous)
        for cur in sorted(self._cash):
            settled = float(self.settled_cash(cur, previous))
            daily = model.interest(cur, settled, benchmark, nav_usd, 1)
            if daily <= 0:
                continue
            d = previous
            while d < day:
                d += timedelta(days=1)
                key = (cur, d.year, d.month)
                self._accrued[key] = self._accrued.get(key, Decimal(0)) + Decimal(repr(daily))

    def _post_interest(self, day: date, rep: SessionReport) -> None:
        """Credit every month that has ended."""
        for key in sorted(self._accrued):
            cur, year, month = key
            if (year, month) < (day.year, day.month):
                amount = money(self._accrued.pop(key))
                if amount:
                    self._cash[cur] = self._cash.get(cur, Decimal(0)) + amount
                    rep.interest.append(InterestRecord(day, cur, amount, f"{year}-{month:02d}"))

    def _pay(self, day: date, rep: SessionReport) -> None:
        keep: list[_Entitlement] = []
        for e in self._entitlements:
            if e.pay_date is not None and e.pay_date <= day:
                self._cash[e.currency] = self._cash.get(e.currency, Decimal(0)) + e.net
                rep.paid.append((e.symbol, e.net, e.currency))
            else:
                keep.append(e)
        self._entitlements = keep

    def _rate(self, day: date) -> float | None:
        return self.truth.usd_to_cad(day, self.fx_max_age_days)

    def _convert(
        self, day: date, purpose: str, sell: str, buy: str, amount: Decimal
    ) -> FxFill | None:
        rate = self._rate(day)
        if rate is None or amount <= 0:
            return None
        amount = min(money(amount), self._cash.get(sell, Decimal(0)))
        if amount <= 0:
            return None
        cost = self.costs.fx_conversion_bps / 10_000 * self.costs.multiplier
        if sell == "CAD" and buy == "USD":
            exec_rate = rate * (1.0 + cost)
            bought = money(float(amount) / exec_rate)
            cost_cad = money(float(amount) - float(bought) * rate)
        elif sell == "USD" and buy == "CAD":
            exec_rate = rate * (1.0 - cost)
            bought = money(float(amount) * exec_rate)
            cost_cad = money(float(amount) * rate - float(bought))
        else:
            raise ValueError(f"unsupported conversion {sell}->{buy}")
        self._cash[sell] -= amount
        self._cash[buy] = self._cash.get(buy, Decimal(0)) + bought
        return FxFill(day, purpose, sell, amount, buy, bought, exec_rate, rate, cost_cad)

    def _run_queued_fx(self, day: date, rep: SessionReport) -> None:
        queued, self._queued_fx = self._queued_fx, []
        for purpose, sell, buy, amount in queued:
            done = self._convert(day, purpose, sell, buy, amount)
            if done is None:
                limit = self.fx_max_age_days
                reason = f"no Bank of Canada rate within {limit} days"
                rep.waiting.append(f"{purpose} {sell}->{buy} {amount}: {reason}")
                self._queued_fx.append((purpose, sell, buy, amount))
            else:
                rep.conversions.append(done)

    def _fund_usd_buys(self, buys: list[SimOrder], day: date, rep: SessionReport) -> None:
        if not buys:
            return
        # the buy is the shares plus their commission; the buffer covers the price move
        cost = sum(
            o.remaining * o.reference_price
            + self.costs.commission(o.currency, o.remaining, o.reference_price)
            for o in buys
        )
        need = funding_need(cost, float(self._cash.get("USD", Decimal(0))), self.funding_buffer)
        rate = self._rate(day)
        if need <= 0 or rate is None:
            if need > 0:
                rep.waiting.append(
                    f"{FX_FUNDING}: no Bank of Canada rate within {self.fx_max_age_days} days"
                )
            return
        fx_cost = self.costs.fx_conversion_bps / 10_000 * self.costs.multiplier
        done = self._convert(
            day, FX_FUNDING, "CAD", "USD", Decimal(repr(need * rate * (1.0 + fx_cost)))
        )
        if done is not None:
            rep.conversions.append(done)

    def _fill(self, o: SimOrder, day: date, rep: SessionReport) -> None:
        bar = self.truth.bar(o.symbol, day)
        if bar is None:
            return
        market = bar.close if self.fill_at == "close" else bar.open
        median = self.truth.median_traded_value(o.symbol, day, MEDIAN_SESSIONS)
        cap = 0 if median is None else math.floor(self.participation_cap * median / bar.close)
        capped = cap < o.remaining
        qty = min(o.remaining, cap)
        if o.side == "sell":
            qty = min(qty, self._positions.get(o.symbol, 0))
        stressed = self.stressed(o.symbol, day)
        bps = self.costs.side_bps(o.symbol, stressed)
        price = Decimal(repr(self.costs.fill_price(market, o.side, o.symbol, stressed))).quantize(
            PRICE_QUANTUM
        )
        cash_limited = False
        if o.side == "buy":
            available = self._cash.get(o.currency, Decimal(0))
            while qty > 0 and money(price * qty) + self._commission(o, qty, price) > available:
                cash_limited = True
                qty = min(qty - 1, int(available / price))
        if qty <= 0:
            return
        commission = self._commission(o, qty, price)
        gross = money(price * qty)
        sign = 1 if o.side == "buy" else -1
        effect = -sign * gross - commission
        self._cash[o.currency] = self._cash.get(o.currency, Decimal(0)) + effect
        cycles = self.costs.settlement
        if cycles is not None:
            settles = cycles.settle_date(self.exchanges[o.symbol], o.calendar, day)
            self._unsettled.append((settles, o.currency, effect))
        self._set_position(o.symbol, self._positions.get(o.symbol, 0) + sign * qty)
        o.remaining -= qty
        if o.remaining == 0:
            o.status = "filled"
        fill = Fill(
            order_id=o.order_id,
            symbol=o.symbol,
            side=o.side,
            shares=qty,
            price=price,
            commission=commission,
            fill_time_utc=session_close_utc(o.calendar, day),
        )
        self._fills.append(fill)
        rep.fills.append(
            FillDetail(fill, o.decision_id, o.currency, market, bps, stressed, capped, cash_limited)
        )

    def _commission(self, o: SimOrder, qty: int, price: Decimal) -> Decimal:
        return money(self.costs.commission(o.currency, qty, float(price)))

    def _set_position(self, symbol: str, shares: int) -> None:
        if shares:
            self._positions[symbol] = shares
        else:
            self._positions.pop(symbol, None)
