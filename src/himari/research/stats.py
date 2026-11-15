"""Research statistics (§12.4): the paired stationary bootstrap, Holm, PSR, DSR, MinTRL and PBO.

Overlapping windows are not independent observations, so inference always uses the bootstrap.
Every function here is pure and deterministic: the bootstrap draws from numpy's PCG64 generator
seeded with `research.seed`, in chunks of a fixed size (the draw order is part of the method),
so the same inputs give the same p-value on every machine.

Approved (v1.8): the bootstrap p-value is one-sided, for improvement against zero, from the
centred distribution of the challenger-minus-baseline statistic, p = (1 + #{d* - d >= d}) /
(B + 1); a register threshold such as "Sharpe difference >= 0.10" applies to the point estimate.

Implementation choices, to confirm (M5):
- a per-period Sharpe ratio uses the sample standard deviation (ddof 1); skewness and ordinary
  kurtosis use population moments (ddof 0);
- PBO drops the earliest T mod S sessions so the S blocks are equal, and ranks out-of-sample
  performance ordinally (ties rank low).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]
Statistic = Callable[[FloatArray], FloatArray]  # rows of resampled series -> one value per row

EULER_GAMMA = 0.5772156649  # §12.4
CHUNK = 250  # resamples per draw: fixed, because the draw order defines the bootstrap
_NORMAL = NormalDist()


def _series(x: ArrayLike, name: str = "series") -> FloatArray:
    a: FloatArray = np.asarray(x, dtype=np.float64)
    if a.ndim != 1 or a.size < 2:
        raise ValueError(f"{name}: need a one-dimensional series of at least 2 observations")
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{name}: contains NaN or infinite values")
    return a


# ---- statistics on rows of (resampled) series ---------------------------------------------
def mean_rows(x: FloatArray) -> FloatArray:
    out: FloatArray = x.mean(axis=1)
    return out


def sharpe_rows(x: FloatArray, periods_per_year: int = 252) -> FloatArray:
    """Annualized Sharpe ratio of each row: mean / sample std x sqrt(periods); 0 if constant."""
    sd = x.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out: FloatArray = np.where(sd > 0, x.mean(axis=1) / sd, 0.0) * math.sqrt(periods_per_year)
    return out


# ---- the stationary bootstrap (Politis and Romano 1994) ------------------------------------
def stationary_indices(n: int, mean_block: float, size: int, rng: np.random.Generator) -> IntArray:
    """`size` resampled index paths of length n. A path starts at a uniform random index; at
    each later step it starts a new block at a uniform random index with probability
    1 / mean_block and otherwise moves to the next index, wrapping from n - 1 to 0."""
    if n < 1 or size < 1 or mean_block < 1:
        raise ValueError("need n >= 1, size >= 1 and mean_block >= 1")
    starts = rng.integers(0, n, size=(size, n), dtype=np.int64)
    new_block = rng.random((size, n)) < 1.0 / mean_block
    new_block[:, 0] = True
    steps = np.arange(n, dtype=np.int64)
    first = np.maximum.accumulate(np.where(new_block, steps, 0), axis=1)  # the block's first step
    rows = np.arange(size)[:, None]
    out: IntArray = (starts[rows, first] + (steps - first)) % n
    return out


@dataclass(frozen=True)
class BootstrapTest:
    observed: float  # statistic(challenger) - statistic(baseline) on the sample
    p_value: float  # one-sided, for improvement
    resamples: int
    mean_block: float
    seed: int


def paired_bootstrap(
    baseline: ArrayLike,
    challenger: ArrayLike,
    statistic: Statistic,
    mean_block: float = 20.0,
    resamples: int = 10_000,
    seed: int = 20261111,
) -> BootstrapTest:
    """One-sided test that the challenger improves on the baseline (§12.4). Both series are
    resampled with the same stationary-bootstrap indices, so the test is paired; the p-value
    comes from the centred distribution of the difference: p = (1 + #{d* - d >= d}) / (B + 1)."""
    a, b = _series(baseline, "baseline"), _series(challenger, "challenger")
    if a.shape != b.shape:
        raise ValueError("baseline and challenger must cover the same sessions")
    if resamples < 1:
        raise ValueError("need at least one resample")
    observed = float(statistic(b[None, :])[0] - statistic(a[None, :])[0])
    rng = np.random.default_rng(seed)
    exceed = done = 0
    while done < resamples:
        size = min(CHUNK, resamples - done)
        idx = stationary_indices(a.size, mean_block, size, rng)
        diff = statistic(b[idx]) - statistic(a[idx])
        exceed += int(np.count_nonzero(diff - observed >= observed))
        done += size
    return BootstrapTest(observed, (1 + exceed) / (resamples + 1), resamples, mean_block, seed)


# ---- Holm ----------------------------------------------------------------------------------
def holm(p_values: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Holm's step-down procedure: sort the p-values in ascending order and reject p_(k) while
    p_(k) <= alpha / (m - k + 1), stopping at the first failure. Returns one reject flag per
    hypothesis, in the input order."""
    m = len(p_values)
    reject = [False] * m
    for k, i in enumerate(sorted(range(m), key=lambda j: p_values[j]), start=1):
        if p_values[i] > alpha / (m - k + 1):
            break
        reject[i] = True
    return reject


# ---- PSR, DSR and MinTRL (Bailey and López de Prado) ----------------------------------------
@dataclass(frozen=True)
class SharpeMoments:
    sr: float  # per-period Sharpe ratio (not annualized)
    skew: float  # skewness, gamma_3
    kurt: float  # ordinary kurtosis, gamma_4 (3 for a normal distribution)
    t: int  # observations


def sharpe_moments(returns: ArrayLike) -> SharpeMoments:
    x = _series(returns, "returns")
    sd = float(x.std(ddof=1))
    if sd == 0.0:
        raise ValueError("returns are constant: the Sharpe ratio is undefined")
    z = x - x.mean()
    m2 = float(np.mean(z**2))
    return SharpeMoments(
        sr=float(x.mean()) / sd,
        skew=float(np.mean(z**3)) / m2**1.5,
        kurt=float(np.mean(z**4)) / m2**2,
        t=int(x.size),
    )


def _variance_term(sr: float, skew: float, kurt: float) -> float:
    v = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if v <= 0.0:
        raise ValueError("the Sharpe ratio's variance term is not positive")
    return v


def psr(sr: float, sr_star: float, t: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """Probabilistic Sharpe ratio: the probability that the true per-period Sharpe ratio exceeds
    sr_star, given T observations with skewness gamma_3 and ordinary kurtosis gamma_4."""
    if t < 2:
        raise ValueError("need at least 2 observations")
    z = (sr - sr_star) * math.sqrt(t - 1) / math.sqrt(_variance_term(sr, skew, kurt))
    return _NORMAL.cdf(z)


def expected_max_sharpe(n_trials: int, var_trials: float) -> float:
    """SR0* (§12.4): the per-period Sharpe ratio expected from the best of N unskilled trials
    whose Sharpe ratios have variance V."""
    if n_trials < 2:
        raise ValueError("the deflated Sharpe ratio needs at least 2 trials")
    if var_trials < 0.0:
        raise ValueError("the variance of the trials' Sharpe ratios cannot be negative")
    g = EULER_GAMMA
    return math.sqrt(var_trials) * (
        (1.0 - g) * _NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
        + g * _NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    )


def dsr(sr: float, t: int, skew: float, kurt: float, n_trials: int, var_trials: float) -> float:
    """Deflated Sharpe ratio: PSR against SR0* for N trials with Sharpe variance V."""
    return psr(sr, expected_max_sharpe(n_trials, var_trials), t, skew, kurt)


def min_trl(
    sr: float, sr_star: float = 0.0, skew: float = 0.0, kurt: float = 3.0, alpha: float = 0.05
) -> float:
    """Minimum track record length, in periods, for a one-sided test at level alpha that the
    per-period Sharpe ratio exceeds sr_star; infinite when sr <= sr_star."""
    if sr <= sr_star:
        return math.inf
    z = _NORMAL.inv_cdf(1.0 - alpha)
    return 1.0 + _variance_term(sr, skew, kurt) * (z / (sr - sr_star)) ** 2


# ---- PBO by combinatorially symmetric cross-validation -------------------------------------
@dataclass(frozen=True)
class PBOResult:
    pbo: float  # share of splits whose in-sample best trial ranks at or below the OOS median
    splits: int
    logits: FloatArray  # per split: ln(w / (1 - w)), w = OOS rank of the IS best / (N + 1)


def pbo(returns: ArrayLike, partitions: int = 16) -> PBOResult:
    """Probability of backtest overfitting (Bailey, Borwein, López de Prado and Zhu), by CSCV
    with the Sharpe ratio as the performance measure. `returns` is T x N: the per-period
    returns of N trials over the same T sessions. Diagnostic only (§12.4)."""
    m: FloatArray = np.asarray(returns, dtype=np.float64)
    if m.ndim != 2 or m.shape[1] < 2:
        raise ValueError("need a T x N matrix of returns with at least 2 trials")
    if partitions < 2 or partitions % 2:
        raise ValueError("partitions must be an even number of at least 2")
    rows = m.shape[0] // partitions
    if rows < 2 or not np.all(np.isfinite(m)):
        raise ValueError("need finite returns and at least 2 sessions per partition")
    m = m[m.shape[0] - rows * partitions :]  # drop the earliest sessions: equal blocks
    blocks = m.reshape(partitions, rows, m.shape[1])
    s1, s2 = blocks.sum(axis=1), (blocks**2).sum(axis=1)  # partitions x N
    combos = np.array(list(itertools.combinations(range(partitions), partitions // 2)))
    in_sample = np.zeros((len(combos), partitions))
    in_sample[np.arange(len(combos))[:, None], combos] = 1.0
    n = rows * partitions // 2

    def sharpe(mask: FloatArray) -> FloatArray:
        a1, a2 = mask @ s1, mask @ s2
        var = (a2 - a1 * a1 / n) / (n - 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out: FloatArray = np.where(var > 0, (a1 / n) / np.sqrt(var), 0.0)
        return out

    sr_in, sr_out = sharpe(in_sample), sharpe(1.0 - in_sample)
    best = np.argmax(sr_in, axis=1)
    best_out = sr_out[np.arange(len(combos)), best]
    rank = 1 + np.count_nonzero(sr_out < best_out[:, None], axis=1)  # ordinal; 1 = worst
    w = rank / (m.shape[1] + 1.0)
    logits: FloatArray = np.log(w / (1.0 - w))
    return PBOResult(float(np.mean(logits <= 0.0)), len(combos), logits)
