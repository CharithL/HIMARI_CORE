"""EWMA volatility, TSX-date return matrix, correlation, shrinkage and covariance (§7.2)."""

import math
from datetime import date

import numpy as np
import pytest

from himari.accounting.indices import TotalReturnIndex
from himari.risk.covariance import (
    covariance,
    ewma_correlation,
    shrink_to_identity,
    tsx_return_matrix,
)
from himari.risk.ewma import annualized_vol, ewma_variance, observations


class TestEwmaVolatility:
    def test_seeded_with_mean_square_then_recursive(self) -> None:
        v = ewma_variance([0.01, -0.01, 0.02, 0.0], lam=0.5, init=2)
        assert math.isnan(v[0])
        assert v[1] == pytest.approx(1e-4, abs=1e-18)
        assert v[2] == pytest.approx(0.5 * 1e-4 + 0.5 * 4e-4, abs=1e-18)
        assert v[3] == pytest.approx(0.5 * v[2], abs=1e-18)

    def test_gap_days_are_not_observations(self) -> None:
        with_gap = [0.01, math.nan, -0.01, 0.02]
        assert list(observations(with_gap)) == [0.01, -0.01, 0.02]
        assert ewma_variance(with_gap, 0.5, 2)[-1] == ewma_variance([0.01, -0.01, 0.02], 0.5, 2)[-1]

    def test_annualized(self) -> None:
        sigma = annualized_vol([0.01, -0.01], lam=0.9, init=2, annualization=252)
        assert sigma == pytest.approx(math.sqrt(252 * 1e-4), abs=1e-15)

    def test_not_enough_history(self) -> None:
        assert annualized_vol([0.01] * 251, 0.97, 252, 252) is None


def index(dates: list[date], wealth: list[float]) -> TotalReturnIndex:
    n = len(dates)
    return TotalReturnIndex("X", dates, np.full(n, math.nan), np.array(wealth), [None] * n)


class TestTsxReturnMatrix:
    def test_us_sessions_compound_into_the_next_tsx_interval(self) -> None:
        tsx = [date(2025, 12, 24), date(2025, 12, 29)]  # TSX closed Dec 25-26
        tsx_fund = index(tsx, [100.0, 101.0])
        us_fund = index(
            [date(2025, 12, 24), date(2025, 12, 26), date(2025, 12, 29)], [1.0, 1.1, 1.21]
        )
        r = tsx_return_matrix({"T": tsx_fund, "U": us_fund}, ["T", "U"], tsx)
        assert r.shape == (1, 2)
        assert r[0, 0] == pytest.approx(0.01, abs=1e-15)
        assert r[0, 1] == pytest.approx(0.21, abs=1e-15)  # Dec 26 compounded in

    def test_bridged_gap_uses_last_valid_level(self) -> None:
        tsx = [date(2025, 4, 17), date(2025, 4, 21), date(2025, 4, 22)]
        us = index(tsx, [1.0, math.nan, 1.03])  # no value on the gap day
        r = tsx_return_matrix({"U": us}, ["U"], tsx)
        assert r[0, 0] == 0.0 and r[1, 0] == pytest.approx(0.03, abs=1e-15)

    def test_nan_before_a_fund_starts(self) -> None:
        tsx = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
        late = index([date(2025, 1, 3), date(2025, 1, 6)], [1.0, 1.1])
        r = tsx_return_matrix({"L": late}, ["L"], tsx)
        assert math.isnan(r[0, 0]) and r[1, 0] == pytest.approx(0.1, abs=1e-15)


class TestCorrelation:
    def test_seed_is_mean_outer_product(self) -> None:
        rows = np.array([[0.01, 0.02], [-0.01, -0.02]])
        corr = ewma_correlation(rows, lam=0.9, init=2)
        assert corr is not None
        assert corr[0, 1] == pytest.approx(1.0, abs=1e-12)

    def test_rows_before_a_fund_starts_are_skipped(self) -> None:
        rows = np.array([[0.05, math.nan], [0.01, 0.01], [0.01, -0.01]])
        corr = ewma_correlation(rows, lam=0.9, init=2)
        assert corr is not None
        assert corr[0, 1] == pytest.approx(0.0, abs=1e-12)

    def test_not_enough_common_rows(self) -> None:
        assert ewma_correlation(np.array([[0.01, math.nan]]), 0.9, 1) is None

    def test_shrinkage_and_covariance(self) -> None:
        rho = np.array([[1.0, -0.2], [-0.2, 1.0]])
        rho_s = shrink_to_identity(rho, 0.5)
        assert rho_s[0, 1] == pytest.approx(-0.1, abs=1e-15)
        cov = covariance([0.15, 0.05], rho_s)
        assert cov[0, 0] == pytest.approx(0.0225, abs=1e-15)
        assert cov[0, 1] == pytest.approx(-0.1 * 0.15 * 0.05, abs=1e-15)
