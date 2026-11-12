"""Zero-mean EWMA volatility (§7.2)."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


def observations(returns: Sequence[float] | FloatArray) -> FloatArray:
    """Finite returns only. A bridged multi-day return is one observation; gap days are none."""
    r = np.asarray(returns, dtype=np.float64)
    finite: FloatArray = r[np.isfinite(r)]
    return finite


def ewma_variance(returns: Sequence[float] | FloatArray, lam: float, init: int) -> FloatArray:
    """v_t = lam * v_{t-1} + (1 - lam) * r_t^2, seeded with the mean of r^2 over the first init.

    Element i is the variance after observation i; NaN until init observations exist.
    """
    r = observations(returns)
    v = np.full(len(r), math.nan)
    if len(r) < init:
        return v
    v[init - 1] = float(np.mean(r[:init] ** 2))
    for i in range(init, len(r)):
        v[i] = lam * v[i - 1] + (1.0 - lam) * r[i] ** 2
    return v


def annualized_vol(
    returns: Sequence[float] | FloatArray, lam: float, init: int, annualization: int
) -> float | None:
    """sigma = sqrt(annualization * v_t) after the latest observation; None without history."""
    v = ewma_variance(returns, lam, init)
    if len(v) == 0 or math.isnan(v[-1]):
        return None
    return math.sqrt(annualization * float(v[-1]))
