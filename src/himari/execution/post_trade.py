"""Post-trade limit check (§9.2, amendment v1.6).

After the band, the post-trade portfolio is the holdings plus this decision's banded trades. If
its sigma_p or any stress loss (single-factor or joint) exceeds its limit by more than
`gate_tolerance_rel` of the limit:

1. shrink this decision's buys pro rata until it fits (the largest fitting scale, in steps of
   1/1000; sigma_p need not fall monotonically as buys shrink, so the steps are scanned);
2. if it still does not fit with no buys, trade every fund to its gated target, ignoring the
   band (reason post_trade_limit).

So buys always shrink before any holding is cut beyond the band's plan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from himari.gates.ceiling import portfolio_vol
from himari.risk.stress import stress_test

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from himari.core.config import FrozenConfig, StressConfig
    from himari.execution.band import BandDecision

    FloatArray = npt.NDArray[np.float64]

SHRINK_STEPS = 1000
EPS = 1e-12


@dataclass(frozen=True)
class RiskLimits:
    vol_ceiling: float
    single_limit: float
    joint_limit: float
    tolerance_rel: float

    @classmethod
    def from_config(cls, cfg: FrozenConfig) -> RiskLimits:
        return cls(
            vol_ceiling=cfg.risk.portfolio_vol_ceiling,
            single_limit=cfg.stress.single_factor_limit,
            joint_limit=cfg.stress.joint_limit,
            tolerance_rel=cfg.rebalance.gate_tolerance_rel,
        )


@dataclass(frozen=True)
class RiskCheck:
    vol: float
    worst_single: float
    worst_single_name: str
    joint: float
    fits: bool

    def describe(self) -> str:
        return (
            f"vol {self.vol:.2%}, worst single {self.worst_single:.2%} "
            f"({self.worst_single_name}), joint {self.joint:.2%}"
        )


def check_risk(
    weights: Mapping[str, float],
    symbols: Sequence[str],
    cov: FloatArray,
    stress: StressConfig,
    limits: RiskLimits,
) -> RiskCheck:
    """sigma_p and stress losses against their limits, each with the relative tolerance."""
    vol = portfolio_vol(weights, symbols, cov)
    st = stress_test(weights, stress)
    k = 1.0 + limits.tolerance_rel
    fits = (
        vol <= limits.vol_ceiling * k + EPS
        and st.worst_single <= limits.single_limit * k + EPS
        and st.joint <= limits.joint_limit * k + EPS
    )
    return RiskCheck(vol, st.worst_single, st.worst_single_name, st.joint, fits)


@dataclass(frozen=True)
class TradePlan:
    weights: dict[str, float]  # post-trade weight of every fund
    reasons: dict[str, str]  # the funds that trade, with their reason codes
    buy_scale: float  # 1.0 unless buys were shrunk
    fallback: bool  # every fund traded to its gated target, ignoring the band
    banded: RiskCheck  # the band's plan, before any change
    final: RiskCheck  # the post-trade portfolio

    @property
    def changed(self) -> bool:
        return self.buy_scale < 1.0 or self.fallback


def plan_trades(
    current: Mapping[str, float],
    gated: Mapping[str, float],
    band: Mapping[str, BandDecision],
    symbols: Sequence[str],
    cov: FloatArray,
    stress: StressConfig,
    limits: RiskLimits,
) -> TradePlan:
    funds = sorted(band)
    held = {s: current.get(s, 0.0) for s in funds}
    banded = {s: band[s].target if band[s].trade else held[s] for s in funds}
    reasons = {s: band[s].reason for s in funds if band[s].trade}
    first = check_risk(banded, symbols, cov, stress, limits)
    if first.fits:
        return TradePlan(banded, reasons, 1.0, False, first, first)

    buys = {s: banded[s] - held[s] for s in funds if banded[s] > held[s]}
    if buys:
        for k in range(SHRINK_STEPS - 1, -1, -1):
            scale = k / SHRINK_STEPS
            trial = {s: held[s] + scale * buys[s] if s in buys else banded[s] for s in funds}
            chk = check_risk(trial, symbols, cov, stress, limits)
            if chk.fits:
                kept = {s: r for s, r in reasons.items() if s not in buys or scale > 0.0}
                return TradePlan(trial, kept, scale, False, first, chk)

    final = {s: gated.get(s, 0.0) for s in funds}
    fallback_reasons = {
        s: reasons[s] if s in reasons and banded[s] == final[s] else "post_trade_limit"
        for s in funds
        if final[s] != held[s]
    }
    chk = check_risk(final, symbols, cov, stress, limits)
    return TradePlan(final, fallback_reasons, 1.0, True, first, chk)
