"""The cash sleeve (§7.4, amendment v1.7): idle CAD cash held in one CAD-listed T-bill or
money-market ETF.

Cash management only: no signal, no risk budget, not part of w, not scaled by any gate, left
out of sigma_p and the stress gate. In rule 5, cash means broker cash plus the sleeve.

Weekly, after the risk order plan is built, with C the CAD cash expected after that plan (buys,
sells, USD funding and estimated costs), buffer = max(buffer_nav x NAV, buffer_min_cad) and
band = max(band_nav x NAV, band_min_cad):

- C < buffer / 2: sell sleeve shares to bring C back to the buffer (rounded up; always trades;
  reason sleeve_funding);
- C > buffer + band: buy sleeve shares with C - buffer (rounded down; reason sleeve_sweep);
- otherwise no sleeve order.

KILL and VENUE_FAULT stop every sleeve order; a data fault on the sleeve stops sleeve buys only;
HALVED and SUSPENDED do not affect it. The sleeve is used only from the fund's own inception.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from himari.execution.orders import funding_need

if TYPE_CHECKING:
    from himari.core.config import CashSleeveConfig
    from himari.execution.costs import CostModel
    from himari.execution.orders import PlannedOrder

SLEEVE_FUNDING = "sleeve_funding"
SLEEVE_SWEEP = "sleeve_sweep"


def expected_cad_after(
    orders: Sequence[PlannedOrder],
    cad_cash: float,
    usd_cash: float,
    usd_to_cad: float,
    costs: CostModel,
    funding_buffer: float,
    usd_surplus: float = 0.0,
) -> float:
    """CAD cash after the plan: CAD buys and sells at their reference prices moved by the cost
    model, commissions, the CAD spent funding USD buys (with the buffer) and any USD surplus
    converted back. USD sales leave their proceeds in USD."""
    cad = cad_cash
    usd_buys = 0.0
    fx_cost = costs.fx_conversion_bps / 10_000 * costs.multiplier
    for o in orders:
        bps = costs.side_bps(o.symbol) / 10_000
        commission = costs.commission(o.currency, o.shares, o.reference_price)
        if o.currency == "USD":
            if o.side == "buy":
                usd_buys += o.shares * o.reference_price + commission
            continue
        if o.side == "buy":
            cad -= o.shares * o.reference_price * (1.0 + bps) + commission
        else:
            cad += o.shares * o.reference_price * (1.0 - bps) - commission
    cad -= funding_need(usd_buys, usd_cash, funding_buffer) * usd_to_cad * (1.0 + fx_cost)
    cad += usd_surplus * usd_to_cad * (1.0 - fx_cost)
    return cad


@dataclass(frozen=True)
class SleevePlan:
    side: str | None  # "buy", "sell" or None
    shares: int
    reason: str  # sleeve_funding | sleeve_sweep | ""
    expected_cad: float
    buffer: float
    band: float
    note: str = ""


def plan_sleeve(
    expected_cad: float,
    nav: float,
    cfg: CashSleeveConfig,
    price: float,
    side_bps: float,
    commission: Callable[[int, float], float],
    held_shares: int,
    buys_allowed: bool = True,
) -> SleevePlan:
    buffer, band = cfg.buffer(nav), cfg.band(nav)

    def plan(side: str | None, shares: int, reason: str, note: str = "") -> SleevePlan:
        return SleevePlan(side, shares, reason, expected_cad, buffer, band, note)

    if expected_cad < buffer / 2:
        need = buffer - expected_cad
        net = price * (1.0 - side_bps / 10_000)
        shares = max(1, math.ceil(need / net))
        while shares * net - commission(shares, price) < need:  # round up, net of costs
            shares += 1
        if held_shares <= 0:
            return plan(None, 0, "", "sleeve empty: nothing to sell")
        return plan("sell", min(shares, held_shares), SLEEVE_FUNDING)
    if expected_cad > buffer + band:
        if not buys_allowed:
            return plan(None, 0, "", "sleeve data fault: no sweep")
        amount = expected_cad - buffer
        gross = price * (1.0 + side_bps / 10_000)
        shares = math.floor(amount / gross)
        while shares > 0 and shares * gross + commission(shares, price) > amount:  # round down
            shares -= 1
        return plan("buy", shares, SLEEVE_SWEEP) if shares > 0 else plan(None, 0, "")
    return plan(None, 0, "")
