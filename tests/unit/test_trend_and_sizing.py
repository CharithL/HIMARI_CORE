"""Trend signal eligibility (§7.1) and the sizing steps (§7.3)."""

import math

import numpy as np
import pytest

from himari.sizing.weights import CapGroup, target_weights
from himari.strategy.trend import trend_signal


class TestTrendSignal:
    def test_needs_lookback_plus_one_observations(self) -> None:
        assert not trend_signal([1.0] * 252, [1.0] * 252, 252).eligible
        assert trend_signal([1.0] * 253, [1.0] * 253, 252).eligible

    def test_exactly_zero_is_off(self) -> None:
        sig = trend_signal([1.0, 1.1], [1.0, 1.1], 1)
        assert sig.momentum == pytest.approx(0.0, abs=1e-15) and not sig.on

    def test_uses_the_value_lookback_observations_back(self) -> None:
        wealth = [1.0, 5.0, 1.2]  # only the first and last count for lookback 2
        assert trend_signal(wealth, [1.0, 1.0, 1.0], 2).momentum == pytest.approx(math.log(1.2))

    def test_misaligned_inputs_raise(self) -> None:
        with pytest.raises(ValueError, match="aligned"):
            trend_signal([1.0, 1.0], [1.0], 1)


IDENTITY3 = np.eye(3)
NO_GROUPS: list[CapGroup] = []


def size(signals: list[bool], **kw: object) -> np.ndarray:
    args: dict[str, object] = {
        "symbols": ["A", "B", "C"],
        "budgets": [0.2, 0.2, 0.2],
        "sigmas": [0.10, 0.10, 0.10],
        "corr": IDENTITY3,
        "signals": signals,
        "per_instrument_cap": 1.0,
        "groups": NO_GROUPS,
        "vol_ceiling": 0.08,
    }
    args.update(kw)
    return target_weights(**args).final  # type: ignore[arg-type]


class TestSizing:
    def test_scale_uses_all_eligible_funds_as_if_on(self) -> None:
        kw = {"sigmas": [0.20, 0.20, 0.20]}  # low enough that no cap or cash limit binds
        all_on = size([True, True, True], **kw)
        one_off = size([True, True, False], **kw)
        assert list(one_off[:2]) == pytest.approx(list(all_on[:2]), abs=1e-15)
        assert one_off[2] == 0.0

    def test_cash_constraint_runs_after_signals(self) -> None:
        """§7.3 order (signals, then caps, then cash): when a fund switches off and the cash
        constraint stops binding, the others grow. Kept as written pending the rule-6 question."""
        all_on = size([True, True, True])  # sum before the cash constraint is 1.386
        one_off = size([True, True, False])
        assert sum(all_on) == pytest.approx(1.0, abs=1e-15)
        assert one_off[0] > all_on[0]

    def test_per_instrument_cap(self) -> None:
        w = size([True, True, True], per_instrument_cap=0.10)
        assert max(w) == pytest.approx(0.10, abs=1e-15)

    def test_group_scaled_proportionally(self) -> None:
        w = size(
            [True, True, True],
            sigmas=[0.05, 0.10, 0.10],
            groups=[CapGroup("AB", ("A", "B"), 0.2)],
        )
        assert w[0] + w[1] == pytest.approx(0.2, abs=1e-15)
        assert w[0] / w[1] == pytest.approx(2.0, abs=1e-12)  # proportions kept

    def test_cash_constraint(self) -> None:
        r = target_weights(
            ["A", "B"], [0.5, 0.5], [0.02, 0.02], np.eye(2), [True, True], 1.0, [], 0.08
        )
        assert sum(r.final) == pytest.approx(1.0, abs=1e-15)
        assert "cash_constraint" in r.bound

    def test_group_members_missing_from_eligible_set_are_ignored(self) -> None:
        w = size([True, True, True], groups=[CapGroup("XY", ("X", "Y"), 0.01)])
        assert w[0] > 0.01

    def test_invalid_inputs(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            size([True, True, True], sigmas=[0.1, 0.0, 0.1])
        with pytest.raises(ValueError, match="match"):
            size([True, True])
