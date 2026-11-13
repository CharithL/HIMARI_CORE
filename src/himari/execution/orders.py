"""Order plan (§9.3): whole shares toward zero, a CAD 100 minimum, and PDBC's USD funding.

Shares are `trunc(w * NAV_CAD / price_CAD)`. Orders below `min_order_cad` are skipped and
logged. A USD-listed buy is funded by converting CAD to USD for the buy plus a buffer just
before it executes (`fx_funding`); sales leave their proceeds in USD; surplus USD goes back to
CAD at a weekly decision once it exceeds a share of NAV (`usd_surplus`). Conversions are tagged
so they never count as strategy trades.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from himari.core.ids import order_idempotency_key

FX_FUNDING = "fx_funding"
USD_SURPLUS = "usd_surplus"


@dataclass(frozen=True)
class PlannedOrder:
    decision_id: str
    symbol: str
    side: str  # "buy" or "sell"
    shares: int
    currency: str  # the fund's trading currency
    reference_price: float  # decision close, in the fund's currency
    reference_price_cad: float
    reason: str  # band | full_exit | post_trade_limit | halved | safety_cut (v1.6)
    gates: tuple[tuple[str, float], ...] = ()  # gates that cut this fund, with their scales

    @property
    def idempotency_key(self) -> str:
        return order_idempotency_key(self.decision_id, self.symbol)

    @property
    def value_cad(self) -> float:
        return self.shares * self.reference_price_cad

    def gates_text(self) -> str:
        return "; ".join(f"{name} x{scale:.3f}" for name, scale in self.gates)


@dataclass(frozen=True)
class SkippedOrder:
    decision_id: str
    symbol: str
    shares: int  # signed: positive buys, negative sells
    value_cad: float
    reason: str


def build_orders(
    decision_id: str,
    targets: Mapping[str, float],
    held_shares: Mapping[str, int],
    prices: Mapping[str, float],
    fx_to_cad: Mapping[str, float],
    currencies: Mapping[str, str],
    nav_cad: float,
    min_order_cad: float,
    reasons: Mapping[str, str],
    gates: Mapping[str, Sequence[tuple[str, float]]] | None = None,
) -> tuple[list[PlannedOrder], list[SkippedOrder]]:
    """Orders that move the funds in `targets` (only the funds that trade) to their weights;
    each carries its fund's reason code and the gates that cut it."""
    gates = gates or {}
    orders: list[PlannedOrder] = []
    skipped: list[SkippedOrder] = []
    for sym in sorted(targets):
        price_cad = prices[sym] * fx_to_cad[currencies[sym]]
        target = math.trunc(targets[sym] * nav_cad / price_cad)
        delta = target - held_shares.get(sym, 0)
        if delta == 0:
            continue
        value = abs(delta) * price_cad
        if value < min_order_cad:
            skipped.append(
                SkippedOrder(decision_id, sym, delta, value, f"below {min_order_cad:g} CAD")
            )
            continue
        orders.append(
            PlannedOrder(
                decision_id=decision_id,
                symbol=sym,
                side="buy" if delta > 0 else "sell",
                shares=abs(delta),
                currency=currencies[sym],
                reference_price=prices[sym],
                reference_price_cad=price_cad,
                reason=reasons[sym],
                gates=tuple(gates.get(sym, ())),
            )
        )
    return orders, skipped


def funding_need(usd_buy_cost: float, usd_cash: float, buffer: float) -> float:
    """USD to buy before a USD-listed purchase: the buy plus the buffer, less USD on hand."""
    return max(0.0, usd_buy_cost * (1.0 + buffer) - usd_cash)


def usd_surplus(
    usd_cash: float, reserved_usd: float, usd_to_cad: float, nav_cad: float, threshold: float
) -> float:
    """USD to convert back to CAD at a weekly decision: the USD not reserved for planned buys,
    once it exceeds `threshold` of NAV; otherwise 0."""
    surplus = usd_cash - reserved_usd
    return surplus if surplus * usd_to_cad > threshold * nav_cad else 0.0
