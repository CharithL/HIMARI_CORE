"""Per-fund daily P&L and the robustness pack's leave-one-instrument-out rows (§12.3, v1.7).

A traded fund's P&L on session t, in CAD at the Bank of Canada rate of t:
- the change in its market value from t-1 to t;
- the cash its fills moved on t: sales less commissions, purchases plus commissions;
- its distributions booked on t (net of withholding) and cash in lieu from its splits;
- the revaluation of its unpaid USD distributions from the rate of t-1 to the rate of t.

What is left of the NAV change is cash: interest earned (the change in accrued interest plus
postings), the revaluation of USD cash, less conversion costs. `Attribution.residual` is what
neither explains, which is rounding only; the tests check it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from himari.sim.engine import BacktestResult

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class Attribution:
    days: list[date]  # sessions t: every NAV point after the first
    nav_prev: FloatArray  # NAV at t-1
    nav_change: FloatArray  # NAV(t) - NAV(t-1)
    pnl: dict[str, FloatArray]  # traded fund (the cash sleeve included) -> P&L on t, CAD
    cash: FloatArray  # interest + USD cash revaluation - conversion costs, CAD
    residual: FloatArray  # nav_change - funds - cash: rounding only


def attribute(res: BacktestResult) -> Attribution:
    pts = res.nav
    if len(pts) < 2:
        raise ValueError("need at least 2 NAV points")
    funds = list(res.universe) + ([res.sleeve] if res.sleeve else [])
    currency: dict[str, str] = {}
    flows: dict[tuple[str, date], float] = defaultdict(float)  # (fund, day) -> CAD
    rate = {p.day: p.usd_to_cad for p in pts}

    def fx(cur: str, day: date) -> float:
        return 1.0 if cur == "CAD" else rate[day]

    for day, det in res.fills:
        fill = det.fill
        currency[fill.symbol] = det.currency
        gross = float(fill.price) * fill.shares
        fee = float(fill.commission)
        moved = gross - fee if fill.side == "sell" else -gross - fee
        flows[(fill.symbol, day)] += moved * fx(det.currency, day)
    for d in res.ledger.distributions:
        currency.setdefault(d.symbol, d.currency)
        flows[(d.symbol, d.booked_on)] += float(d.net) * fx(d.currency, d.booked_on)
    for s in res.ledger.splits:
        cur = currency.get(s.symbol, "CAD")
        flows[(s.symbol, s.booked_on)] += float(s.cash_in_lieu) * fx(cur, s.booked_on)
    postings: dict[date, float] = defaultdict(float)
    for i in res.ledger.interest:
        postings[i.day] += float(i.amount) * fx(i.currency, i.day)
    conversion_cost: dict[date, float] = defaultdict(float)
    for c in res.ledger.conversions:
        conversion_cost[c.day] += float(c.cost_cad)
    usd_receivables = [d for d in res.ledger.distributions if d.currency == "USD"]

    def value(p_idx: int, fund: str) -> float:
        p = pts[p_idx]
        w = p.sleeve_weight if fund == res.sleeve else p.weights.get(fund, 0.0)
        return w * p.nav

    def unpaid_usd(fund: str, day: date) -> float:
        """The fund's USD distributions booked but not paid by the end of `day`."""
        return sum(
            float(d.net)
            for d in usd_receivables
            if d.symbol == fund and d.booked_on <= day and (d.paid_on is None or d.paid_on > day)
        )

    n = len(pts) - 1
    pnl: dict[str, FloatArray] = {fund: np.zeros(n) for fund in funds}
    cash_part: FloatArray = np.zeros(n)
    for k in range(1, len(pts)):
        t, prev = pts[k].day, pts[k - 1]
        move = pts[k].usd_to_cad - prev.usd_to_cad
        for fund in funds:
            pnl[fund][k - 1] = (
                value(k, fund)
                - value(k - 1, fund)
                + flows[(fund, t)]
                + unpaid_usd(fund, prev.day) * move
            )
        interest = pts[k].accrued_interest_cad - prev.accrued_interest_cad + postings[t]
        cash_part[k - 1] = interest + prev.cash.get("USD", 0.0) * move - conversion_cost[t]
    nav: FloatArray = np.array([p.nav for p in pts], dtype=np.float64)
    change: FloatArray = np.diff(nav)
    total: FloatArray = np.zeros(n)
    for v in pnl.values():
        total = total + v
    residual: FloatArray = change - total - cash_part
    return Attribution([p.day for p in pts[1:]], nav[:-1], change, pnl, cash_part, residual)


def leave_one_instrument_out(res: BacktestResult, periods_per_year: int = 252) -> dict[str, float]:
    """The annualized excess return over the measured sessions with each fund's P&L removed:
    252 x mean(r_ex - P&L_i / NAV(t-1)). For the research gate's "no single instrument" test
    every value must stay positive (judged at 2x costs)."""
    from himari.research.metrics import excess_returns, measured_series

    days, nav, cash_level = measured_series(res)
    r_ex = excess_returns(nav, cash_level)
    att = attribute(res)
    pos = {d: i for i, d in enumerate(att.days)}
    idx = [pos[d] for d in days[1:]]
    out: dict[str, float] = {}
    for f in res.universe:
        removed = r_ex - att.pnl[f][idx] / att.nav_prev[idx]
        out[f] = periods_per_year * float(removed.mean()) if removed.size else math.nan
    return out
