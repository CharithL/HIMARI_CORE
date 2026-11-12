"""Property tests for target weights and caps (§7.3 as amended in v1.4; rules 5 and 6)."""

import math

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from himari.risk.covariance import covariance, shrink_to_identity
from himari.sizing.weights import CapGroup, embed_correlation, target_weights

SYMBOLS = ["VCN", "VUN", "VIU", "VEE", "VGV", "CGL.C", "PDBC", "DLR"]
BUDGETS = [0.05, 0.05, 0.05, 0.05, 0.20, 0.20, 0.20, 0.20]
GROUPS = [
    CapGroup("all_equity", ("VCN", "VUN", "VIU", "VEE"), 0.40),
    CapGroup("usd_cash_fund", ("DLR",), 0.15),
]
N = len(SYMBOLS)


@st.composite
def correlation(draw: st.DrawFn) -> np.ndarray:
    """A random valid correlation matrix from random factor loadings, shrunk halfway to I."""
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
    assert np.all(r.after_caps <= r.all_on + 1e-15)  # caps only scale down
    assert np.all(r.after_cash <= r.after_caps + 1e-15)
    assert np.array_equal(w, r.after_cash * np.array(on, dtype=float))  # signals come last
    all_on_vol = math.sqrt(r.all_on @ covariance(sig, corr) @ r.all_on)
    assert math.isclose(all_on_vol, 0.08, rel_tol=1e-9)


@settings(max_examples=200, deadline=None)
@given(corr=correlation(), sig=sigmas, on=signals, flip=st.integers(0, N - 1))
def test_switching_a_signal_off_never_raises_another_weight(
    corr: np.ndarray, sig: list[float], on: list[bool], flip: int
) -> None:
    """Rule 6 (v1.4): unused budget stays in cash; no other pre-gate weight moves."""
    before = target_weights(SYMBOLS, BUDGETS, sig, corr, on, 0.25, GROUPS, 0.08)
    off = list(on)
    off[flip] = False
    after = target_weights(SYMBOLS, BUDGETS, sig, corr, off, 0.25, GROUPS, 0.08)
    assert after.final[flip] == 0.0
    for i in range(N):
        if i != flip:
            assert after.final[i] == before.final[i]


@settings(max_examples=200, deadline=None)
@given(
    corr=correlation(),
    sig=sigmas,
    on=signals,
    ready=st.lists(st.booleans(), min_size=N, max_size=N),
)
def test_funds_not_eligible_keep_their_budget_in_cash(
    corr: np.ndarray, sig: list[float], on: list[bool], ready: list[bool]
) -> None:
    """v1.4: k covers the whole universe; a fund that is not eligible has zero weight."""
    idx = [i for i in range(N) if ready[i]]
    full = embed_correlation(ready, corr[np.ix_(idx, idx)])
    sigmas_or_none = [s if ok else None for s, ok in zip(sig, ready, strict=True)]
    r = target_weights(SYMBOLS, BUDGETS, sigmas_or_none, full, on, 0.25, GROUPS, 0.08)
    b = np.array(BUDGETS)
    assert math.isclose(r.scale, 0.08 / math.sqrt(b @ full @ b), rel_tol=1e-12)
    assert all(r.final[i] == 0.0 for i in range(N) if not ready[i])
    assert float(r.final.sum()) <= 1.0 + 1e-12
