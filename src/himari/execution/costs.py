"""Cost model (§11, config/costs_v1.yaml).

ETF cost per side = max(per_side_floor, half_spread + impact) in bp, plus the broker commission.
Event-window sessions use spreads x stress_spread_multiplier. Every cost, commission and FX
conversion is scaled by the run's multiplier (1x or 2x; pass or fail is decided at 2x).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from himari.accounting.interest import InterestModel, SettlementCycles


@dataclass(frozen=True)
class CommissionSchedule:
    """Per-share commission with a minimum and a cap as a fraction of trade value."""

    per_share: float
    minimum: float
    max_fraction: float

    def charge(self, shares: int, price: float) -> float:
        if shares == 0:
            return 0.0
        value = abs(shares) * price
        return min(max(abs(shares) * self.per_share, self.minimum), self.max_fraction * value)


@dataclass(frozen=True)
class CostModel:
    per_side_floor_bps: float
    impact_bps: float
    half_spread_bps: dict[str, float]
    fx_conversion_bps: float
    stress_spread_multiplier: float
    commissions: dict[str, CommissionSchedule]  # by trading currency: CAD, USD
    multiplier: float = 1.0
    participation_cap: float = 0.005  # of the trailing median traded value (§10.4)
    carry_forward_sessions: int = 3
    us_withholding: float = 0.15  # on distributions of US-listed funds (§9.4)
    interest: InterestModel | None = None  # v1.6: interest on settled cash (not scaled)
    settlement: SettlementCycles | None = None  # v1.6: when trade cash settles

    def side_bps(self, symbol: str, stressed: bool = False) -> float:
        spread = self.half_spread_bps[symbol] * (self.stress_spread_multiplier if stressed else 1.0)
        return max(self.per_side_floor_bps, spread + self.impact_bps) * self.multiplier

    def fill_price(self, close: float, side: str, symbol: str, stressed: bool = False) -> float:
        """Pay up when buying, receive less when selling."""
        bps = self.side_bps(symbol, stressed) / 10_000
        return close * (1.0 + bps) if side == "buy" else close * (1.0 - bps)

    def commission(self, currency: str, shares: int, price: float) -> float:
        return self.commissions[currency].charge(shares, price) * self.multiplier

    def fx_cost(self, amount: float) -> float:
        return abs(amount) * self.fx_conversion_bps / 10_000 * self.multiplier


def load_cost_model(
    costs_path: Path, multiplier: float, commission_path: Path | None = None
) -> CostModel:
    """Costs from costs_v1.yaml. The commission schedule must be given explicitly: costs_v1.yaml
    says to load the broker's current schedule and never assume zero."""
    with open(costs_path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)
    etf = raw["etf"]
    if commission_path is None:
        raise ValueError(
            "no commission schedule: costs_v1.yaml requires the broker's current schedule "
            "(never assume zero); pass one explicitly"
        )
    with open(commission_path, encoding="utf-8") as f:
        comm: dict[str, Any] = yaml.safe_load(f)
    return CostModel(
        per_side_floor_bps=float(etf["per_side_floor_bps"]),
        impact_bps=float(etf["impact_bps"]),
        half_spread_bps={k: float(v) for k, v in etf["half_spread_bps"].items()},
        fx_conversion_bps=float(raw["fx_conversion"]["cost_bps"]),
        stress_spread_multiplier=float(raw["stress_spread_multiplier"]),
        commissions={
            cur: CommissionSchedule(
                float(c["per_share"]), float(c["minimum"]), float(c["max_fraction"])
            )
            for cur, c in comm["commissions"].items()
        },
        multiplier=multiplier,
        participation_cap=float(etf["participation_cap"]),
        carry_forward_sessions=int(etf["carry_forward_sessions"]),
        us_withholding=float(raw["withholding"]["us_listed_distributions"]),
        interest=InterestModel.from_config(raw["cash_interest"])
        if "cash_interest" in raw
        else None,
        settlement=(
            SettlementCycles.from_config(raw["settlement_days"])
            if "settlement_days" in raw
            else None
        ),
    )
