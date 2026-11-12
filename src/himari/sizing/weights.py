"""Target weights (§7.3). After the common scale every step only scales down."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from himari.risk.covariance import covariance

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class CapGroup:
    name: str
    members: tuple[str, ...]
    max: float


@dataclass(frozen=True)
class SizingResult:
    symbols: tuple[str, ...]
    unit: FloatArray  # u_i = b_i / sigma_i
    scale: float  # k, from all eligible funds as if every signal were on
    all_on: FloatArray  # k * u
    after_signals: FloatArray  # k * u * s
    after_caps: FloatArray
    final: FloatArray  # after the cash constraint
    bound: tuple[str, ...]  # caps and constraints that cut a weight

    def weights(self) -> dict[str, float]:
        return {s: float(w) for s, w in zip(self.symbols, self.final, strict=True)}


def target_weights(
    symbols: Sequence[str],
    budgets: Sequence[float],
    sigmas: Sequence[float],
    corr: FloatArray,
    signals: Sequence[bool],
    per_instrument_cap: float,
    groups: Sequence[CapGroup],
    vol_ceiling: float,
    max_gross: float = 1.0,
) -> SizingResult:
    """Steps 1-6 of §7.3 for the eligible funds; sigmas are floored, corr is already shrunk."""
    n = len(symbols)
    b = np.asarray(budgets, dtype=np.float64)
    sig = np.asarray(sigmas, dtype=np.float64)
    s = np.asarray(signals, dtype=np.float64)
    if not (len(b) == len(sig) == len(s) == n and corr.shape == (n, n)):
        raise ValueError("budgets, sigmas, signals and corr must match the symbols")
    if not np.all(np.isfinite(sig) & (sig > 0)):
        raise ValueError("volatilities must be positive")

    unit: FloatArray = b / sig
    port_var = float(unit @ covariance(sig, corr) @ unit)
    scale = vol_ceiling / math.sqrt(port_var)
    all_on: FloatArray = scale * unit
    w: FloatArray = all_on * s
    after_signals = w.copy()

    bound: list[str] = []
    capped = np.minimum(w, per_instrument_cap)
    bound += [
        f"per_instrument:{sym}" for sym, a, c in zip(symbols, w, capped, strict=True) if c < a
    ]
    w = capped
    for g in groups:
        idx = [i for i, sym in enumerate(symbols) if sym in g.members]
        total = float(w[idx].sum())
        if total > g.max:
            w[idx] *= g.max / total
            bound.append(f"group:{g.name}")
    after_caps = w.copy()

    gross = float(w.sum())
    if gross > max_gross:
        w = w * (max_gross / gross)
        bound.append("cash_constraint")
    return SizingResult(
        tuple(symbols), unit, scale, all_on, after_signals, after_caps, w, tuple(bound)
    )
