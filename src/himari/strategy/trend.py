"""Trend signal (§7.1): m = ln(W_t / W_{t-L}) - ln(B_t / B_{t-L}); on only when m > 0."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from himari.accounting.indices import CashIndex, TotalReturnIndex


@dataclass(frozen=True)
class TrendSignal:
    momentum: float | None  # None when the fund is not eligible
    on: bool
    eligible: bool


def trend_signal(wealth: Sequence[float], cash: Sequence[float], lookback: int) -> TrendSignal:
    """Wealth and cash levels aligned on the fund's valid observations, oldest first.

    Fewer than lookback + 1 observations (lookback returns) means not eligible and off.
    Exactly zero momentum is off.
    """
    if len(wealth) != len(cash):
        raise ValueError("wealth and cash must be aligned")
    if len(wealth) < lookback + 1:
        return TrendSignal(None, False, False)
    m = math.log(wealth[-1] / wealth[-1 - lookback]) - math.log(cash[-1] / cash[-1 - lookback])
    return TrendSignal(m, m > 0.0, True)


def aligned_levels(index: TotalReturnIndex, cash: CashIndex) -> tuple[list[float], list[float]]:
    """W and B on the fund's sessions where both have a value (bridged gap days drop out)."""
    by_day = {d: float(b) for d, b in zip(cash.dates, cash.level, strict=True) if math.isfinite(b)}
    pairs = [
        (float(w), by_day[d])
        for d, w in zip(index.dates, index.wealth, strict=True)
        if math.isfinite(w) and d in by_day
    ]
    return [w for w, _ in pairs], [b for _, b in pairs]
