"""The post-trade limit check (§9.2, amendment v1.6), for random holdings, targets and risk.

The gated targets are built the way the gates leave them: within sigma_p and the stress limits.
Then, after the band and the check:
- the post-trade portfolio never exceeds sigma_p or a stress limit by more than 10%;
- buys shrink before any holding is cut: unless the fallback ran, no fund ends below the band's
  plan, and the fallback runs only when the plan fails even with every buy removed.

Half the examples put the holdings near their limits (0.6x to 1.4x of the binding one), trim
them a little and buy other funds, so the shrink and fallback branches are exercised.
"""

from pathlib import Path

import numpy as np
from hypothesis import assume, event, given, settings
from hypothesis import strategies as st

from himari.core.config import load_config
from himari.execution.band import apply_band
from himari.execution.post_trade import RiskLimits, check_risk, plan_trades
from himari.gates.ceiling import portfolio_vol
from himari.risk.stress import stress_test

CFG = load_config(Path(__file__).parents[2] / "config" / "frozen_v1.yaml")
SYMBOLS = tuple(i.symbol for i in CFG.stage1.instruments())
N = len(SYMBOLS)
LIMITS = RiskLimits.from_config(CFG)
STRICT = RiskLimits(LIMITS.vol_ceiling, LIMITS.single_limit, LIMITS.joint_limit, 0.0)


@st.composite
def covariance(draw: st.DrawFn) -> np.ndarray:
    loadings = np.array(
        draw(st.lists(st.floats(-1.0, 1.0, allow_nan=False), min_size=2 * N, max_size=2 * N))
    ).reshape(N, 2)
    corr = loadings @ loadings.T + 0.1 * np.eye(N)
    d = np.sqrt(np.diag(corr))
    corr = 0.5 * corr / np.outer(d, d) + 0.5 * np.eye(N)  # shrunk halfway, as the risk model
    sig = np.array(draw(st.lists(st.floats(0.02, 0.4), min_size=N, max_size=N)))
    return np.diag(sig) @ corr @ np.diag(sig)


@st.composite
def portfolio(draw: st.DrawFn) -> dict[str, float]:
    raw = draw(st.lists(st.floats(0.0, 0.25, allow_nan=False), min_size=N, max_size=N))
    zeroed = draw(st.lists(st.booleans(), min_size=N, max_size=N))
    return _capped({s: 0.0 if z else w for s, w, z in zip(SYMBOLS, raw, zeroed, strict=True)})


def _capped(w: dict[str, float]) -> dict[str, float]:
    total = sum(w.values())
    return {s: v / total for s, v in w.items()} if total > 1.0 else w


def risk_ratio(w: dict[str, float], cov: np.ndarray) -> float:
    st_ = stress_test(w, CFG.stress)
    return max(
        portfolio_vol(w, SYMBOLS, cov) / LIMITS.vol_ceiling,
        st_.worst_single / LIMITS.single_limit,
        st_.joint / LIMITS.joint_limit,
    )


def gated_like_the_gates(w: dict[str, float], cov: np.ndarray) -> dict[str, float]:
    """Scale to the ceiling and the stress limits, as the volatility and stress gates do."""
    r = risk_ratio(w, cov)
    k = min(1.0, 1.0 / r) if r > 0 else 1.0
    return {s: v * k * (1 - 1e-9) for s, v in w.items()}


@st.composite
def random_case(draw: st.DrawFn) -> tuple[dict[str, float], dict[str, float], np.ndarray]:
    cov = draw(covariance())
    return draw(portfolio()), gated_like_the_gates(draw(portfolio()), cov), cov


@st.composite
def near_limit_case(draw: st.DrawFn) -> tuple[dict[str, float], dict[str, float], np.ndarray]:
    """Gated targets at their binding limit and holdings 9-11.1% above them: just inside the
    10% relative band, so the kept holdings sit around the 10% tolerance. A quarter of the
    funds are not held and become new buys. (A breach needs this: a holding the band keeps
    is at most about 11% above its gated target, so the check is a rare backstop.)"""
    # uniform draws from a seed: Hypothesis's own floats favour the interval ends, which
    # rarely produce the near-limit geometry this case is for
    rng = np.random.default_rng(draw(st.integers(0, 2**32 - 1)))
    loadings = rng.uniform(-1.0, 1.0, (N, 2))
    corr = loadings @ loadings.T + 0.1 * np.eye(N)
    d = np.sqrt(np.diag(corr))
    corr = 0.5 * corr / np.outer(d, d) + 0.5 * np.eye(N)
    sig = rng.uniform(0.02, 0.4, N)
    cov = np.diag(sig) @ corr @ np.diag(sig)
    raw = {s: 0.0 if rng.random() < 0.3 else float(rng.uniform(0.0, 0.25)) for s in SYMBOLS}
    r = risk_ratio(raw, cov)
    assume(r > 0)
    gated = _capped({s: w / r * (1 - 1e-9) for s, w in raw.items()})  # at the binding limit
    held = {
        s: 0.0 if rng.random() < 0.25 else gated[s] * float(rng.uniform(1.09, 1.111))
        for s in SYMBOLS
    }
    return _capped(held), gated, cov


FORCED = st.lists(
    st.sampled_from([None, None, None, None, None, "halved", "safety_cut"]), min_size=N, max_size=N
)


def check_case(
    case: tuple[dict[str, float], dict[str, float], np.ndarray], forced: list[str | None]
) -> None:
    held, gated, cov = case
    assert check_risk(gated, SYMBOLS, cov, CFG.stress, STRICT).fits
    reasons = {s: r for s, r in zip(SYMBOLS, forced, strict=True) if r is not None}
    band = apply_band(gated, held, CFG.rebalance.band_abs, CFG.rebalance.band_rel, reasons)
    plan = plan_trades(held, gated, band, SYMBOLS, cov, CFG.stress, LIMITS)
    event("fallback" if plan.fallback else "buys shrunk" if plan.buy_scale < 1 else "fits")

    # never more than 10% over sigma_p or a stress limit
    after = check_risk(plan.weights, SYMBOLS, cov, CFG.stress, LIMITS)
    assert after.fits and plan.final.fits
    assert after.vol <= 1.1 * LIMITS.vol_ceiling + 1e-9
    assert after.worst_single <= 1.1 * LIMITS.single_limit + 1e-9
    assert after.joint <= 1.1 * LIMITS.joint_limit + 1e-9

    # buys shrink before any holding is cut
    banded = {s: b.target if b.trade else b.current for s, b in band.items()}
    no_buys = {s: min(banded[s], held.get(s, 0.0)) for s in banded}
    if plan.fallback:
        assert not check_risk(no_buys, SYMBOLS, cov, CFG.stress, LIMITS).fits
    else:
        for s, w in plan.weights.items():
            assert w >= min(banded[s], held.get(s, 0.0)) - 1e-12  # no cut beyond the band
            assert w <= max(banded[s], held.get(s, 0.0)) + 1e-12  # buys only shrink
        if plan.buy_scale < 1.0:
            assert not plan.banded.fits


@settings(max_examples=300, deadline=None)
@given(case=random_case(), forced=FORCED)
def test_random_portfolios(
    case: tuple[dict[str, float], dict[str, float], np.ndarray], forced: list[str | None]
) -> None:
    check_case(case, forced)


@settings(max_examples=600, deadline=None)
@given(case=near_limit_case(), forced=FORCED)
def test_holdings_at_the_band_edge(
    case: tuple[dict[str, float], dict[str, float], np.ndarray], forced: list[str | None]
) -> None:
    """Exercises the shrink and fallback branches (see near_limit_case)."""
    check_case(case, forced)
