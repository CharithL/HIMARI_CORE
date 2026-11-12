"""Stage 1 decision (§7): trend signals, the risk model and target weights for one date.

Every input is cut at the decision date here, so data after it cannot change the decision.
Data-quality faults and the risk gates (§8) are applied afterwards; they only cut.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

from himari.risk.covariance import ewma_correlation, shrink_to_identity, tsx_return_matrix
from himari.risk.ewma import annualized_vol
from himari.sizing.weights import CapGroup, SizingResult, target_weights
from himari.strategy.trend import TrendSignal, aligned_levels, trend_signal

if TYPE_CHECKING:
    from himari.accounting.indices import CashIndex, TotalReturnIndex
    from himari.core.config import FrozenConfig

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class FundRisk:
    symbol: str
    signal: TrendSignal
    sigma: float | None  # EWMA volatility, annualized
    sigma_floored: float | None
    tsx_returns: int  # returns available on the common TSX grid


@dataclass(frozen=True)
class Stage1Decision:
    decision_date: date
    funds: dict[str, FundRisk]
    eligible: tuple[str, ...]
    corr: FloatArray | None  # shrunk correlation over the eligible funds
    sizing: SizingResult | None

    def weights(self) -> dict[str, float]:
        """Every fund in the universe; ineligible or off funds hold zero (their budget is cash)."""
        sized = self.sizing.weights() if self.sizing is not None else {}
        return {sym: sized.get(sym, 0.0) for sym in self.funds}


def _upto_index(index: TotalReturnIndex, day: date) -> TotalReturnIndex:
    n = sum(1 for d in index.dates if d <= day)
    flags = {d: f for d, f in index.flags.items() if d <= day}
    return replace(
        index,
        dates=index.dates[:n],
        returns=index.returns[:n],
        wealth=index.wealth[:n],
        released_at=index.released_at[:n],
        flags=flags,
    )


def _upto_cash(cash: CashIndex, day: date) -> CashIndex:
    n = sum(1 for d in cash.dates if d <= day)
    flags = {d: f for d, f in cash.flags.items() if d <= day}
    return replace(
        cash, dates=cash.dates[:n], level=cash.level[:n], rate=cash.rate[:n], flags=flags
    )


def stage1_decision(
    cfg: FrozenConfig,
    decision_date: date,
    indices: Mapping[str, TotalReturnIndex],
    cash: Mapping[str, CashIndex],
    tsx_dates: Sequence[date],
) -> Stage1Decision:
    """Signals, volatilities, correlation and target weights as of the decision date.

    indices and cash hold each fund's CAD wealth index and cash index on the fund's own
    sessions; tsx_dates are XTSE sessions. A fund is eligible when it has a signal, a
    volatility and at least init_sessions returns on the common TSX grid.
    """
    risk = cfg.risk
    instruments = cfg.stage1.instruments()
    symbols = [i.symbol for i in instruments]
    idx = {s: _upto_index(indices[s], decision_date) for s in symbols}
    grid = [d for d in tsx_dates if d <= decision_date]
    tsx = tsx_return_matrix(idx, symbols, grid)
    counts = np.isfinite(tsx).sum(axis=0)

    funds: dict[str, FundRisk] = {}
    for j, inst in enumerate(instruments):
        sym = inst.symbol
        wealth, levels = aligned_levels(idx[sym], _upto_cash(cash[sym], decision_date))
        signal = trend_signal(wealth, levels, cfg.signal.lookback_sessions)
        sigma = annualized_vol(
            idx[sym].returns, risk.vol_decay(), risk.init_sessions, risk.annualization
        )
        floor = risk.vol_floor[inst.asset_class.value]
        floored = None if sigma is None else max(sigma, floor)
        funds[sym] = FundRisk(sym, signal, sigma, floored, int(counts[j]))

    eligible = tuple(
        s
        for s in symbols
        if funds[s].signal.eligible
        and funds[s].sigma_floored is not None
        and funds[s].tsx_returns >= risk.init_sessions
    )
    if not eligible:
        return Stage1Decision(decision_date, funds, eligible, None, None)

    cols = [symbols.index(s) for s in eligible]
    rho = ewma_correlation(tsx[:, cols], risk.corr_decay(), risk.init_sessions)
    if rho is None:
        raise RuntimeError("eligible funds lack a common TSX history; eligibility check is wrong")
    corr = shrink_to_identity(rho, risk.corr_shrink_to_identity)
    by_symbol = {i.symbol: i for i in instruments}
    sigmas = [funds[s].sigma_floored for s in eligible]
    sizing = target_weights(
        symbols=eligible,
        budgets=[cfg.stage1.budget_for(by_symbol[s]) for s in eligible],
        sigmas=[x for x in sigmas if x is not None],
        corr=corr,
        signals=[funds[s].signal.on for s in eligible],
        per_instrument_cap=cfg.stage1.caps.per_instrument,
        groups=[CapGroup(g.name, tuple(g.members), g.max) for g in cfg.stage1.caps.groups],
        vol_ceiling=risk.portfolio_vol_ceiling,
        max_gross=cfg.stage1.max_gross,
    )
    if not np.all(np.isfinite(sizing.final)):
        raise RuntimeError("target weights are not finite")
    return Stage1Decision(decision_date, funds, eligible, corr, sizing)
