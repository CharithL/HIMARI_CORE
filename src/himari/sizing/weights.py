"""Target weights (§7.3, amendment v1.4).

k = ceiling / sqrt(b' rho_s b) over the whole frozen universe; all-on weights a = k * b / sigma;
caps and the cash constraint act on a; signals come last (w = a * s), so switching a signal off
never raises another fund's weight. A fund that is not eligible keeps its budget in k with zero
correlation to the others; its weight stays 0 and its budget stays in cash.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class CapGroup:
    name: str
    members: tuple[str, ...]
    max: float


@dataclass(frozen=True)
class SizingResult:
    symbols: tuple[str, ...]
    eligible: tuple[bool, ...]
    unit: FloatArray  # u_i = b_i / sigma_i; 0 for a fund that is not eligible
    scale: float  # k
    all_on: FloatArray  # a = k * u
    after_caps: FloatArray  # a after the per-fund and group caps
    after_cash: FloatArray  # a after the cash constraint
    final: FloatArray  # w = a * s, before the gates
    bound: tuple[str, ...]  # caps and constraints that cut a weight

    def weights(self) -> dict[str, float]:
        return {s: float(w) for s, w in zip(self.symbols, self.final, strict=True)}


def target_weights(
    symbols: Sequence[str],
    budgets: Sequence[float],
    sigmas: Sequence[float | None],
    corr: FloatArray,
    signals: Sequence[bool],
    per_instrument_cap: float,
    groups: Sequence[CapGroup],
    vol_ceiling: float,
    max_gross: float = 1.0,
) -> SizingResult:
    """symbols is the whole frozen universe. sigmas are floored, None where not eligible.

    corr is the shrunk correlation over the universe; a fund that is not eligible must have
    zero correlation to every other fund.
    """
    n = len(symbols)
    b = np.asarray(budgets, dtype=np.float64)
    if not (len(b) == len(sigmas) == len(signals) == n and corr.shape == (n, n)):
        raise ValueError("budgets, sigmas, signals and corr must match the symbols")
    eligible = np.array([s is not None for s in sigmas])
    sig = np.array([s if s is not None else math.nan for s in sigmas], dtype=np.float64)
    if not np.all(sig[eligible] > 0):
        raise ValueError("volatilities must be positive")
    off_block = corr[~eligible][:, eligible]
    if off_block.size and np.any(off_block != 0.0):
        raise ValueError("a fund that is not eligible must have zero correlation to the others")

    unit: FloatArray = np.where(eligible, b / np.where(eligible, sig, 1.0), 0.0)
    scale = vol_ceiling / math.sqrt(float(b @ corr @ b))
    all_on: FloatArray = scale * unit

    bound: list[str] = []
    a: FloatArray = np.minimum(all_on, per_instrument_cap).astype(np.float64)
    bound += [
        f"per_instrument:{sym}" for sym, x, c in zip(symbols, all_on, a, strict=True) if c < x
    ]
    for g in groups:
        idx = [i for i, sym in enumerate(symbols) if sym in g.members]
        total = float(a[idx].sum())
        if total > g.max:
            a[idx] *= g.max / total
            bound.append(f"group:{g.name}")
    after_caps = a.copy()

    gross = float(a.sum())
    if gross > max_gross:
        a = a * (max_gross / gross)
        bound.append("cash_constraint")
    after_cash = a.copy()

    final: FloatArray = a * np.asarray(signals, dtype=np.float64)
    return SizingResult(
        tuple(symbols),
        tuple(bool(e) for e in eligible),
        unit,
        scale,
        all_on,
        after_caps,
        after_cash,
        final,
        tuple(bound),
    )


def embed_correlation(eligible: Sequence[bool], corr_eligible: FloatArray) -> FloatArray:
    """Universe correlation: the eligible block from the estimate, identity elsewhere."""
    mask = np.asarray(eligible, dtype=bool)
    out = np.eye(len(mask))
    idx = np.flatnonzero(mask)
    if len(idx) != corr_eligible.shape[0]:
        raise ValueError("corr_eligible must cover exactly the eligible funds")
    out[np.ix_(idx, idx)] = corr_eligible
    return out
