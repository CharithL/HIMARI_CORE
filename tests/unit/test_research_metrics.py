"""§12.2 return metrics (M5): hand-computed values on the real functions."""

import math
from datetime import date

import numpy as np
import pytest

from himari.research.metrics import (
    cer,
    excess_returns,
    expected_shortfall,
    leave_one_year_out,
    longest_underwater,
    max_drawdown,
    return_metrics,
    sharpe,
)
from himari.research.stats import sharpe_rows


def test_excess_returns_are_nav_less_cash() -> None:
    r = excess_returns([100.0, 101.0, 100.5], [1.0, 1.0001, 1.0002])
    assert r == pytest.approx([0.01 - 0.0001, 100.5 / 101 - 1 - (1.0002 / 1.0001 - 1)])


def test_drawdown_and_time_underwater() -> None:
    nav = [100, 120, 90, 110, 119, 125, 124]
    assert max_drawdown(nav) == pytest.approx(90 / 120 - 1)  # -25%
    assert longest_underwater(nav) == 3  # 90, 110, 119 below the 120 peak; then 124 once
    assert longest_underwater([100, 101, 102]) == 0
    assert longest_underwater([100, 90, 95]) == 2  # never recovers: counted to the end


def test_expected_shortfall_is_the_mean_of_the_worst_tail() -> None:
    r = np.zeros(80)
    r[[5, 40]] = [-0.05, -0.03]
    assert expected_shortfall(r) == pytest.approx(0.04)  # ceil(2.5% x 80) = 2 worst losses
    r81 = np.append(r, -0.01)
    assert expected_shortfall(r81) == pytest.approx(0.03)  # ceil(2.025) = 3: 5%, 3%, 1%


def test_cer_formula() -> None:
    assert cer(np.full(10, 0.0004)) == pytest.approx(252 * 0.0004)  # no variance penalty
    x = np.random.default_rng(3).normal(0.0003, 0.008, 1000)
    expected = 252 * x.mean() - 1.5 * 252 * x.var(ddof=1)
    assert cer(x) == pytest.approx(expected, rel=1e-12)


def test_sharpe_matches_the_bootstrap_statistic() -> None:
    x = np.random.default_rng(5).normal(0.0004, 0.01, 1500)
    assert sharpe(x) == pytest.approx(float(sharpe_rows(x[None, :])[0]), rel=1e-12)
    assert sharpe(np.zeros(5)) == 0.0


def test_return_metrics_on_a_steady_series() -> None:
    """NAV growing 0.05% a session against cash at 0.01%: r_ex = 0.04% every session."""
    days = 253
    nav = 100 * 1.0005 ** np.arange(days)
    cash = 1.0001 ** np.arange(days)
    m = return_metrics(nav, cash)
    assert m.sessions == 252
    assert m.annual_return == pytest.approx(1.0005**252 - 1)
    assert m.annual_excess_return == pytest.approx(252 * 0.0004, rel=1e-6)
    assert m.max_drawdown == 0.0 and m.longest_underwater == 0
    assert m.es == pytest.approx(-0.0004, rel=1e-6)  # the worst "loss" is a gain here
    assert m.cer == pytest.approx(252 * 0.0004, rel=1e-6)


def test_leave_one_year_out() -> None:
    days = [date(2020, 6, 1), date(2020, 6, 2), date(2021, 6, 1), date(2022, 6, 1)]
    r = [0.001, 0.003, -0.002, 0.0005]
    out = leave_one_year_out(days, r)
    assert out == pytest.approx(
        {2020: 252 * (-0.002 + 0.0005) / 2, 2021: 252 * 0.0045 / 3, 2022: 252 * 0.002 / 3}
    )
    assert out[2020] < 0  # this row would fail the research gate's "no single crisis" test


def test_invalid_inputs_are_refused() -> None:
    with pytest.raises(ValueError):
        excess_returns([100.0, 101.0], [1.0, 1.0, 1.0])
    with pytest.raises(ValueError):
        max_drawdown([100.0, -1.0])
    with pytest.raises(ValueError):
        expected_shortfall([], 0.975)
    with pytest.raises(ValueError):
        leave_one_year_out([date(2020, 1, 1)], [0.1, 0.2])
    assert math.isnan(leave_one_year_out([date(2020, 1, 2)], [0.1])[2020])
