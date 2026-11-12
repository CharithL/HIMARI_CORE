"""Trend signal eligibility (§7.1) and the sizing steps (§7.3)."""

import math

import numpy as np
import pytest

from himari.sizing.weights import CapGroup, embed_correlation, target_weights
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
    def test_switching_a_signal_off_never_raises_another_fund(self) -> None:
        """v1.4: caps and the cash constraint act on the all-on weights, so the others stay put
        even when, as here, the cash constraint binds (all-on sum 1.386 before scaling)."""
        all_on = size([True, True, True])
        one_off = size([True, True, False])
        assert sum(all_on) == pytest.approx(1.0, abs=1e-15)
        assert list(one_off[:2]) == pytest.approx(list(all_on[:2]), abs=1e-15)
        assert one_off[2] == 0.0

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

    def test_group_cap_holds_when_a_member_is_off(self) -> None:
        kw = {"sigmas": [0.05, 0.10, 0.10], "groups": [CapGroup("AB", ("A", "B"), 0.2)]}
        on = size([True, True, True], **kw)
        a_off = size([False, True, True], **kw)
        assert a_off[1] == pytest.approx(on[1], abs=1e-15)  # B does not take A's share

    def test_cash_constraint(self) -> None:
        r = target_weights(
            ["A", "B"], [0.5, 0.5], [0.02, 0.02], np.eye(2), [True, True], 1.0, [], 0.08
        )
        assert sum(r.final) == pytest.approx(1.0, abs=1e-15)
        assert "cash_constraint" in r.bound

    def test_group_members_missing_from_the_universe_are_ignored(self) -> None:
        w = size([True, True, True], groups=[CapGroup("XY", ("X", "Y"), 0.01)])
        assert w[0] > 0.01

    def test_invalid_inputs(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            size([True, True, True], sigmas=[0.1, 0.0, 0.1])
        with pytest.raises(ValueError, match="match"):
            size([True, True])


class TestFundNotYetEligible:
    """v1.4: a fund without 252 sessions stays in k with zero correlation; weight 0."""

    corr = np.array([[1.0, 0.3, 0.0], [0.3, 1.0, 0.0], [0.0, 0.0, 1.0]])

    def test_k_uses_the_whole_universe(self) -> None:
        r = target_weights(
            ["A", "B", "C"], [0.2, 0.2, 0.2], [0.1, 0.1, None], self.corr,
            [True, True, True], 1.0, [], 0.08,
        )  # fmt: skip
        b = np.array([0.2, 0.2, 0.2])
        assert r.scale == pytest.approx(0.08 / math.sqrt(b @ self.corr @ b), abs=1e-15)
        assert r.final[2] == 0.0 and r.eligible == (True, True, False)

    def test_budget_stays_in_cash(self) -> None:
        """A and B are no larger than if C's budget were simply dropped from the universe."""
        with_c = target_weights(
            ["A", "B", "C"], [0.2, 0.2, 0.2], [0.1, 0.1, None], self.corr,
            [True, True, True], 1.0, [], 0.08,
        )  # fmt: skip
        without_c = target_weights(
            ["A", "B"], [0.2, 0.2], [0.1, 0.1], self.corr[:2, :2], [True, True], 1.0, [], 0.08
        )
        assert with_c.final[0] < without_c.final[0]

    def test_must_have_zero_correlation(self) -> None:
        bad = np.array([[1.0, 0.3, 0.1], [0.3, 1.0, 0.0], [0.1, 0.0, 1.0]])
        with pytest.raises(ValueError, match="zero correlation"):
            target_weights(
                ["A", "B", "C"], [0.2] * 3, [0.1, 0.1, None], bad, [True] * 3, 1.0, [], 0.08
            )

    def test_embed_correlation(self) -> None:
        block = np.array([[1.0, 0.4], [0.4, 1.0]])
        full = embed_correlation([True, False, True], block)
        assert full.tolist() == [[1.0, 0.0, 0.4], [0.0, 1.0, 0.0], [0.4, 0.0, 1.0]]
