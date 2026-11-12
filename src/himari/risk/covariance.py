"""Correlation and covariance (§7.2).

EWMA correlation on a common TSX-date return matrix, shrunk toward the identity;
covariance Sigma = D rho_s D with D = diag(floored volatilities).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from himari.accounting.indices import TotalReturnIndex

FloatArray = npt.NDArray[np.float64]


def tsx_return_matrix(
    indices: Mapping[str, TotalReturnIndex], symbols: Sequence[str], tsx_dates: Sequence[date]
) -> FloatArray:
    """Returns between consecutive TSX dates (rows) for each symbol (columns).

    Each fund's wealth is read at its last valid observation on or before the TSX date, so
    US-session returns (and bridged FX gaps) are compounded into the next TSX interval.
    A cell is NaN until the fund has a value.
    """
    levels = np.full((len(tsx_dates), len(symbols)), math.nan)
    for j, sym in enumerate(symbols):
        idx = indices[sym]
        valid = [
            (d, float(w)) for d, w in zip(idx.dates, idx.wealth, strict=True) if math.isfinite(w)
        ]
        days = [d for d, _ in valid]
        for t, day in enumerate(tsx_dates):
            i = bisect.bisect_right(days, day) - 1
            if i >= 0:
                levels[t, j] = valid[i][1]
    out: FloatArray = levels[1:] / levels[:-1] - 1.0
    return out


def ewma_correlation(returns: FloatArray, lam: float, init: int) -> FloatArray | None:
    """Zero-mean EWMA covariance over the rows where every fund has a return, as a correlation.

    Seeded with the mean of r r' over the first init common rows; None without enough rows.
    """
    rows = returns[np.all(np.isfinite(returns), axis=1)]
    if len(rows) < init:
        return None
    cov = rows[:init].T @ rows[:init] / init
    for r in rows[init:]:
        cov = lam * cov + (1.0 - lam) * np.outer(r, r)
    scale = np.sqrt(np.diag(cov))
    if np.any(scale <= 0):
        raise ValueError("a fund has zero variance; its correlation is undefined")
    corr: FloatArray = cov / np.outer(scale, scale)
    np.fill_diagonal(corr, 1.0)
    return corr


def shrink_to_identity(corr: FloatArray, weight: float) -> FloatArray:
    """rho_s = (1 - weight) * rho + weight * I."""
    out: FloatArray = (1.0 - weight) * corr + weight * np.eye(len(corr))
    return out


def covariance(sigmas: Sequence[float] | FloatArray, corr: FloatArray) -> FloatArray:
    """Sigma = D rho D with D = diag(sigmas)."""
    d = np.diag(np.asarray(sigmas, dtype=np.float64))
    out: FloatArray = d @ corr @ d
    return out
