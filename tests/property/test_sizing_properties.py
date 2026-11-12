"""Property tests for target weights and caps (§7.3, rules 5 and 6)."""

import math

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from himari.risk.covariance import covariance, shrink_to_identity
from himari.sizing.weights import CapGroup, target_weights

SYMBOLS = ["VCN", "VUN", "VIU", "VEE", "VGV", "CGL.C", "PDBC", "DLR"]
BUDGETS = [0.05, 0.05, 0.05, 0.05, 0.20, 0.20, 0.20, 0.20]
GROUPS = [
    CapGroup("all_equity", ("VCN", "VUN", "VIU", "VEE"), 0.40),
    CapGroup("usd_cash_fund", ("DLR",), 0.15),
]
N = len(SYMBOLS)


@st.composite
def correlation(draw: st.DrawFn) -> np.ndarray:
    """A random valid correlation matrix from random factor loadings."""
    k = draw(st.integers(min_value=1, max_value=3))
    values = draw(st.lists(st.floats(-1.0, 1.0, allow_nan=False), min_size=N * k, max_size=N * k))
    a = np.array(values).reshape(N, k)
    cov = a @ a.T + 0.05 * np.eye(N)
    d = np.sqrt(np.diag(cov))
    return shrink_to_identity(cov / np.outer(d, d), 0.5)


sigmas = st.lists(st.floats(0.02, 0.60, allow_nan=False), min_size=N, max_size=N)
signals = st.lists(st.booleans(), min_size=N, max_size=N)


@settings(max_examples=200, deadline=None)
@given(corr=correlation(), sig=sigmas, on=signals)
def test_weights_respect_every_limit(corr: np.ndarray, sig: list[float], on: list[bool]) -> None:
    r = target_weights(SYMBOLS, BUDGETS, sig, corr, on, 0.25, GROUPS, 0.08)
    w = r.final
    assert np.all(w >= 0.0)  # long-or-cash
    assert float(w.sum()) <= 1.0 + 1e-12  # no leverage
    assert np.all(w <= 0.25 + 1e-12)
    assert float(w[:4].sum()) <= 0.40 + 1e-12
    assert w[7] <= 0.15 + 1e-12
    assert all(w[i] == 0.0 for i in range(N) if not on[i])
    assert np.all(r.after_caps <= r.after_signals + 1e-15)  # caps only scale down
    assert np.all(w <= r.after_caps + 1e-15)
    all_on_vol = math.sqrt(r.all_on @ covariance(sig, corr) @ r.all_on)
    assert math.isclose(all_on_vol, 0.08, rel_tol=1e-9)


@settings(max_examples=200, deadline=None)
@given(corr=correlation(), sig=sigmas, on=signals, flip=st.integers(0, N - 1))
def test_common_scale_ignores_other_signals(
    corr: np.ndarray, sig: list[float], on: list[bool], flip: int
) -> None:
    """§7.3 step 3: k is computed as if every signal were on, so switching one fund off leaves
    every other fund's pre-cap weight unchanged. (Steps 5-6 run after the signals, so a cap or
    the cash constraint that stops binding can still let others grow; see test_trend_and_sizing.)
    """
    before = target_weights(SYMBOLS, BUDGETS, sig, corr, on, 0.25, GROUPS, 0.08)
    off = list(on)
    off[flip] = False
    after = target_weights(SYMBOLS, BUDGETS, sig, corr, off, 0.25, GROUPS, 0.08)
    assert before.scale == after.scale
    for i in range(N):
        if i != flip:
            assert after.after_signals[i] == before.after_signals[i]
