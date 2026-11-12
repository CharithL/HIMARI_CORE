"""Appendix F goldens for M2, computed by the real strategy, risk and sizing code."""

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from himari.accounting.indices import CashIndex, TotalReturnIndex
from himari.core.config import load_config
from himari.risk.covariance import covariance, shrink_to_identity
from himari.risk.ewma import ewma_variance
from himari.sizing.weights import CapGroup, SizingResult, target_weights
from himari.strategy.trend import aligned_levels, trend_signal

CFG = load_config(Path(__file__).parents[2] / "config" / "frozen_v1.yaml")
LOOKBACK = CFG.signal.lookback_sessions


class TestEwmaDecay:
    def test_decay_factors(self) -> None:
        assert CFG.risk.vol_decay() == pytest.approx(0.965936, abs=5e-7)
        assert CFG.risk.corr_decay() == pytest.approx(0.988514, abs=5e-7)

    def test_recursion_uses_the_decay(self) -> None:
        lam = CFG.risk.vol_decay()
        r = [0.01, -0.01, 0.02]
        v = ewma_variance(r, lam, init=2)
        assert v[1] == pytest.approx(1e-4, abs=1e-18)
        assert v[2] == pytest.approx(lam * 1e-4 + (1 - lam) * 4e-4, abs=1e-18)


def path(total: float, n: int) -> list[float]:
    """n + 1 levels compounding evenly from 1 to total."""
    step = total ** (1.0 / n)
    return [step**i for i in range(n + 1)]


class TestTrendSignal:
    def test_wealth_up_6_cash_up_2_5_is_on(self) -> None:
        sig = trend_signal(path(1.06, LOOKBACK), path(1.025, LOOKBACK), LOOKBACK)
        assert sig.momentum == pytest.approx(0.033576, abs=5e-7)
        assert sig.on and sig.eligible

    def test_wealth_up_2_cash_up_2_5_is_off(self) -> None:
        sig = trend_signal(path(1.02, LOOKBACK), path(1.025, LOOKBACK), LOOKBACK)
        assert sig.momentum == pytest.approx(-0.004890, abs=5e-7)
        assert not sig.on and sig.eligible

    def test_through_index_objects(self) -> None:
        """Same golden via aligned_levels on real index types, with a bridged gap day."""
        days = [date(2024, 1, 1) + timedelta(days=i) for i in range(LOOKBACK + 2)]
        w = path(1.06, LOOKBACK)
        wealth = np.array([w[0], math.nan, *w[1:]])  # a gap day without a value
        index = TotalReturnIndex(
            "X", days, np.full(len(days), math.nan), wealth, [None] * len(days)
        )
        b = path(1.025, LOOKBACK)
        cash = CashIndex(days, np.array([b[0], b[0], *b[1:]]), np.full(len(days), math.nan))
        sig = trend_signal(*aligned_levels(index, cash), LOOKBACK)
        assert sig.momentum == pytest.approx(0.033576, abs=5e-7)


class TestSizingToyCase:
    """Three funds; budgets 0.5/0.3/0.2; sigma 0.15/0.05/0.07; rho_AB -0.2, rho_AC 0.1, rho_BC 0."""

    rho = np.array([[1.0, -0.2, 0.1], [-0.2, 1.0, 0.0], [0.1, 0.0, 1.0]])
    sigmas = [0.15, 0.05, 0.07]

    def run(self) -> tuple[SizingResult, np.ndarray]:
        corr = shrink_to_identity(self.rho, CFG.risk.corr_shrink_to_identity)
        result = target_weights(
            symbols=["A", "B", "C"],
            budgets=[0.5, 0.3, 0.2],
            sigmas=self.sigmas,
            corr=corr,
            signals=[True, True, False],
            per_instrument_cap=0.25,
            groups=[CapGroup("A_alone", ("A",), 0.40), CapGroup("C_only", ("C",), 0.15)],
            vol_ceiling=CFG.risk.portfolio_vol_ceiling,
        )
        return result, covariance(self.sigmas, corr)

    def test_every_step(self) -> None:
        r, sigma = self.run()
        assert list(r.unit) == pytest.approx([3.333333, 6.000000, 2.857143], abs=5e-7)
        assert r.scale == pytest.approx(0.133333, abs=5e-7)
        all_on = r.all_on
        assert list(all_on) == pytest.approx([0.444444, 0.800000, 0.380952], abs=5e-7)
        assert math.sqrt(all_on @ sigma @ all_on) == pytest.approx(0.08, abs=1e-12)
        # v1.4: caps and the cash constraint act on the all-on weights, signals come last
        assert list(r.after_caps) == pytest.approx([0.25, 0.25, 0.15], abs=1e-15)
        assert sum(r.after_caps) == pytest.approx(0.65, abs=1e-15)
        assert list(r.after_cash) == list(r.after_caps)  # 0.65 <= 1: no cash scaling
        final = r.final
        assert list(final) == pytest.approx([0.25, 0.25, 0.0], abs=1e-15)
        assert math.sqrt(final @ sigma @ final) == pytest.approx(0.038324, abs=5e-7)
