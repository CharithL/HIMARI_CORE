"""Return metrics (§12.2) on daily CAD excess returns r_ex = r - c: the strategy's daily return
less the cash index's (§5.6), over the measured sessions (warm-up excluded, §12.2 v1.6), at 1x
and 2x costs. Pure functions over aligned NAV and cash-index levels.

Implementation choices, to confirm (M5):
- r_ex is the arithmetic difference of the daily simple returns;
- the annualized excess return is 252 x mean(r_ex), the same term as in the CER, and is what
  the research gate's "net excess return" and the leave-one-out rows use; the annualized NAV
  return is geometric;
- 97.5% ES is the mean of the worst ceil(2.5% x T) daily losses (-r_ex), reported as a positive
  loss;
- variances and standard deviations are sample ones (ddof 1);
- the longest time underwater counts sessions from a NAV peak until NAV is back at or above it
  (to the end of the window if it never recovers).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import ArrayLike, NDArray

if TYPE_CHECKING:
    from himari.core.config import ResearchConfig
    from himari.sim.engine import BacktestResult

FloatArray = NDArray[np.float64]


def _levels(x: ArrayLike, name: str) -> FloatArray:
    a: FloatArray = np.asarray(x, dtype=np.float64)
    if a.ndim != 1 or a.size < 2:
        raise ValueError(f"{name}: need at least 2 levels")
    if not np.all(np.isfinite(a)) or np.any(a <= 0):
        raise ValueError(f"{name}: levels must be finite and positive")
    return a


def simple_returns(levels: ArrayLike) -> FloatArray:
    a = _levels(levels, "levels")
    out: FloatArray = a[1:] / a[:-1] - 1.0
    return out


def excess_returns(nav: ArrayLike, cash_level: ArrayLike) -> FloatArray:
    """Daily r_ex = NAV return - cash-index return, for aligned sessions."""
    n, c = _levels(nav, "nav"), _levels(cash_level, "cash_level")
    if n.shape != c.shape:
        raise ValueError("nav and cash_level must cover the same sessions")
    out: FloatArray = (n[1:] / n[:-1]) - (c[1:] / c[:-1])
    return out


def sharpe(r_ex: ArrayLike, periods_per_year: int = 252) -> float:
    x = np.asarray(r_ex, dtype=np.float64)
    sd = float(x.std(ddof=1))
    return float(x.mean()) / sd * math.sqrt(periods_per_year) if sd > 0 else 0.0


def max_drawdown(nav: ArrayLike) -> float:
    """The worst NAV / running peak - 1 (0 or negative)."""
    a = _levels(nav, "nav")
    return float(np.min(a / np.maximum.accumulate(a) - 1.0))


def longest_underwater(nav: ArrayLike) -> int:
    """The longest run of consecutive sessions with NAV below its running peak."""
    a = _levels(nav, "nav")
    under = a < np.maximum.accumulate(a)
    longest = run = 0
    for u in under:
        run = run + 1 if u else 0
        longest = max(longest, run)
    return longest


def expected_shortfall(r_ex: ArrayLike, level: float = 0.975) -> float:
    """The mean of the worst ceil((1 - level) x T) daily losses, as a positive number."""
    x = np.asarray(r_ex, dtype=np.float64)
    if x.size == 0 or not 0.0 < level < 1.0:
        raise ValueError("need returns and a level strictly between 0 and 1")
    k = max(1, math.ceil((1.0 - level) * x.size - 1e-9))
    return float(-np.mean(np.sort(x)[:k]))


def cer(r_ex: ArrayLike, risk_aversion: float = 3.0, periods_per_year: int = 252) -> float:
    """Certainty-equivalent return (§12.2): P x mean(r_ex) - (gamma / 2) x P x var(r_ex)."""
    x = np.asarray(r_ex, dtype=np.float64)
    var = float(x.var(ddof=1)) if x.size > 1 else 0.0
    return periods_per_year * float(x.mean()) - risk_aversion / 2.0 * periods_per_year * var


@dataclass(frozen=True)
class ReturnMetrics:
    sessions: int  # daily returns measured
    annual_return: float  # geometric, of NAV
    annual_excess_return: float  # 252 x mean(r_ex)
    annual_vol: float  # sqrt(252) x std(r_ex)
    sharpe: float
    max_drawdown: float
    longest_underwater: int  # sessions
    es: float  # 97.5% expected shortfall of r_ex, a positive daily loss
    cer: float


def return_metrics(
    nav: ArrayLike,
    cash_level: ArrayLike,
    periods_per_year: int = 252,
    risk_aversion: float = 3.0,
    es_level: float = 0.975,
) -> ReturnMetrics:
    n = _levels(nav, "nav")
    r_ex = excess_returns(n, cash_level)
    t = r_ex.size
    return ReturnMetrics(
        sessions=t,
        annual_return=float((n[-1] / n[0]) ** (periods_per_year / t) - 1.0),
        annual_excess_return=periods_per_year * float(r_ex.mean()),
        annual_vol=float(r_ex.std(ddof=1)) * math.sqrt(periods_per_year) if t > 1 else 0.0,
        sharpe=sharpe(r_ex, periods_per_year),
        max_drawdown=max_drawdown(n),
        longest_underwater=longest_underwater(n),
        es=expected_shortfall(r_ex, es_level),
        cer=cer(r_ex, risk_aversion, periods_per_year),
    )


def measured_series(res: BacktestResult) -> tuple[list[date], list[float], list[float]]:
    """Sessions, NAV and cash-index levels of a backtest from its metric start (§12.2: the
    warm-up is excluded). A gap in the cash index inside that span is refused, never filled."""
    from himari.sim.report import metric_start

    m0 = metric_start(res)
    points = [(p, c) for p, c in zip(res.nav, res.cash_level, strict=True) if p.day >= m0]
    if any(not math.isfinite(c) for _, c in points):
        raise ValueError("the cash index has a gap inside the measured sessions")
    return [p.day for p, _ in points], [p.nav for p, _ in points], [c for _, c in points]


def backtest_metrics(
    res: BacktestResult, research: ResearchConfig, periods_per_year: int = 252
) -> ReturnMetrics:
    """§12.2 return metrics of a backtest over its measured sessions."""
    _, nav, cash = measured_series(res)
    return return_metrics(nav, cash, periods_per_year, research.risk_aversion, research.es_level)


def leave_one_year_out(
    days: Sequence[date], r_ex: ArrayLike, periods_per_year: int = 252
) -> dict[int, float]:
    """Robustness pack (§12.3, v1.7): the annualized excess return with each calendar year's
    sessions removed, keyed by the year left out. `days[i]` is the session of `r_ex[i]`. For
    the research gate's "no single crisis" test every value must stay positive."""
    x = np.asarray(r_ex, dtype=np.float64)
    if len(days) != x.size:
        raise ValueError("one session date per return")
    years = np.array([d.year for d in days])
    out: dict[int, float] = {}
    for y in sorted(set(years.tolist())):
        rest = x[years != y]
        out[y] = periods_per_year * float(rest.mean()) if rest.size else math.nan
    return out
