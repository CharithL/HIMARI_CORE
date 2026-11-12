"""Portfolio volatility ceiling (§8.2) and the daily safety check (§8.3)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from himari.core.types import GateLog

FloatArray = npt.NDArray[np.float64]


def portfolio_vol(weights: Mapping[str, float], symbols: Sequence[str], cov: FloatArray) -> float:
    """sigma_p = sqrt(w' Sigma w), with Sigma ordered like symbols."""
    unknown = set(weights) - set(symbols)
    if unknown:
        raise ValueError(f"no covariance for {sorted(unknown)}")
    w = np.array([weights.get(s, 0.0) for s in symbols], dtype=np.float64)
    return math.sqrt(max(float(w @ cov @ w), 0.0))


def apply_vol_ceiling(
    weights_in: Mapping[str, float], symbols: Sequence[str], cov: FloatArray, ceiling: float
) -> tuple[dict[str, float], GateLog]:
    """If sigma_p exceeds the ceiling, scale every weight by ceiling / sigma_p."""
    vol = portfolio_vol(weights_in, symbols, cov)
    scale = ceiling / vol if vol > ceiling else 1.0
    out = {s: w * scale for s, w in weights_in.items()}
    log = GateLog(
        gate_name="vol_ceiling",
        weights_in=dict(weights_in),
        weights_out=out,
        bound=scale < 1.0,
        scale_applied=scale,
        details={"portfolio_vol": vol, "ceiling": ceiling},
    )
    return out, log


@dataclass(frozen=True)
class SafetyCheck:
    triggered: bool
    vol: float
    targets: dict[str, float]  # holdings brought back to the ceiling when triggered


def daily_safety_check(
    holdings: Mapping[str, float],
    symbols: Sequence[str],
    cov: FloatArray,
    ceiling: float,
    multiple: float,
) -> SafetyCheck:
    """After each close: above multiple * ceiling, cut the holdings back to the ceiling.

    The cuts execute in the next session; risk is added back only at the next weekly decision.
    """
    vol = portfolio_vol(holdings, symbols, cov)
    if vol > multiple * ceiling:
        scale = ceiling / vol
        return SafetyCheck(True, vol, {s: w * scale for s, w in holdings.items()})
    return SafetyCheck(False, vol, dict(holdings))
