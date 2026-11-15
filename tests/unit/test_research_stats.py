"""§12.4 statistics (M5): Appendix F goldens on the real functions, plus checks that the
bootstrap is paired, stationary and deterministic, and that PBO separates skill from noise."""

import math

import numpy as np
import pytest

from himari.research.stats import (
    CHUNK,
    dsr,
    expected_max_sharpe,
    holm,
    mean_rows,
    min_trl,
    paired_bootstrap,
    pbo,
    psr,
    sharpe_moments,
    sharpe_rows,
    stationary_indices,
)

DAILY = math.sqrt(252)


class TestHolm:
    def test_appendix_f_golden(self) -> None:
        """p-values [0.01, 0.04, 0.03, 0.20] at alpha 0.05: only the first is rejected."""
        assert holm([0.01, 0.04, 0.03, 0.20], 0.05) == [True, False, False, False]

    def test_step_down_stops_at_the_first_failure(self) -> None:
        # thresholds 0.0125, 0.0167, 0.025, 0.05: the third sorted p (0.03) fails, so 0.04 is
        # kept even though it is below alpha
        assert holm([0.001, 0.01, 0.03, 0.04], 0.05) == [True, True, False, False]
        assert holm([0.001, 0.002, 0.003], 0.05) == [True, True, True]
        assert holm([0.5, 0.6], 0.05) == [False, False]

    def test_keeps_the_input_order(self) -> None:
        assert holm([0.20, 0.01, 0.04, 0.03], 0.05) == [False, True, False, False]


class TestSharpeInference:
    def test_min_trl_appendix_f_golden(self) -> None:
        """Normal returns, one-sided 95%, SR* = 0: about 10.82 years at an annual Sharpe of 0.5
        and about 2.71 at 1.0. The full formula gives 10.83 and 2.71; the guide's figures are
        its leading term (z / SR)^2, so the tolerance is 0.02 years."""
        assert min_trl(0.5 / DAILY) / 252 == pytest.approx(10.82, abs=0.02)
        assert min_trl(1.0 / DAILY) / 252 == pytest.approx(2.71, abs=0.02)

    def test_min_trl_formula(self) -> None:
        sr, z = 0.05, 1.6448536269514722
        expected = 1 + (1 - (-0.5) * sr + (6.0 - 1) / 4 * sr**2) * (z / sr) ** 2
        assert min_trl(sr, 0.0, skew=-0.5, kurt=6.0) == pytest.approx(expected, rel=1e-12)
        assert min_trl(0.01, sr_star=0.02) == math.inf

    def test_psr_is_one_half_at_the_benchmark(self) -> None:
        """Appendix F: PSR(SR*) = 0.5 when SR = SR*."""
        for sr, skew, kurt in ((0.05, 0.0, 3.0), (0.1, -1.0, 8.0), (-0.02, 0.5, 4.0)):
            assert psr(sr, sr, 1000, skew, kurt) == pytest.approx(0.5, abs=1e-12)

    def test_psr_rises_with_the_track_record(self) -> None:
        values = [psr(0.05, 0.0, t) for t in (100, 500, 2500)]
        assert values == sorted(values) and values[-1] > 0.99

    def test_dsr_falls_as_the_trial_count_rises(self) -> None:
        """Appendix F: DSR falls as N rises, other things equal (N = 10 ... 10,000, §12.4)."""
        m = (0.06, 2772, -0.3, 5.0)
        values = [dsr(*m, n_trials=n, var_trials=0.0004) for n in (10, 100, 1000, 10000)]
        assert all(a > b for a, b in zip(values, values[1:], strict=False))

    @pytest.mark.parametrize("n", [100, 1000])
    def test_expected_max_sharpe_matches_simulation(self, n: int) -> None:
        """SR0* approximates the expected maximum of N unskilled trials' Sharpe ratios."""
        draws = np.random.default_rng(7).standard_normal((4000, n)).max(axis=1)
        assert expected_max_sharpe(n, 1.0) == pytest.approx(draws.mean(), rel=0.02)
        assert expected_max_sharpe(n, 0.25) == pytest.approx(0.5 * expected_max_sharpe(n, 1.0))

    def test_moments_of_a_normal_sample(self) -> None:
        x = np.random.default_rng(1).normal(0.001, 0.01, 200_000)
        m = sharpe_moments(x)
        assert m.t == 200_000 and m.sr == pytest.approx(0.1, abs=0.01)
        assert m.skew == pytest.approx(0.0, abs=0.02) and m.kurt == pytest.approx(3.0, abs=0.05)

    def test_invalid_inputs_are_refused(self) -> None:
        with pytest.raises(ValueError):
            expected_max_sharpe(1, 0.01)
        with pytest.raises(ValueError):
            sharpe_moments([0.01, 0.01, 0.01])
        with pytest.raises(ValueError):
            sharpe_moments([0.01, float("nan"), 0.02])


class TestStationaryBootstrap:
    def test_paths_continue_blocks_or_jump(self) -> None:
        n, size = 2000, 250
        idx = stationary_indices(n, 20.0, size, np.random.default_rng(3))
        assert idx.shape == (size, n) and idx.min() >= 0 and idx.max() < n
        continued = idx[:, 1:] == (idx[:, :-1] + 1) % n
        # a new block starts with probability 1/20; a jump can land on the next index by
        # chance (1/n), which is negligible here
        assert 1.0 - continued.mean() == pytest.approx(1 / 20, abs=0.003)

    def test_same_seed_same_p_value(self) -> None:
        rng = np.random.default_rng(11)
        a, b = rng.normal(0, 0.01, 800), rng.normal(0.0002, 0.01, 800)
        first = paired_bootstrap(a, b, sharpe_rows, resamples=600, seed=5)
        again = paired_bootstrap(a, b, sharpe_rows, resamples=600, seed=5)
        other = paired_bootstrap(a, b, sharpe_rows, resamples=600, seed=6)
        assert first == again and first.p_value != other.p_value

    def test_the_test_is_paired(self) -> None:
        """A challenger that is the baseline plus a constant beats it on every resample, so p
        reaches its floor 1 / (B + 1); unpaired resampling would not."""
        a = np.random.default_rng(2).normal(0, 0.01, 500)
        res = paired_bootstrap(a, a + 0.0005, mean_rows, resamples=CHUNK + 83)
        assert res.observed == pytest.approx(0.0005)
        assert res.p_value == pytest.approx(1 / (CHUNK + 84))
        assert paired_bootstrap(a, a, mean_rows, resamples=100).p_value == 1.0

    def test_better_and_worse_challengers(self) -> None:
        rng = np.random.default_rng(4)
        base = rng.normal(0.0, 0.01, 2500)
        better = base * 0.5 + rng.normal(0.0008, 0.005, 2500)
        assert paired_bootstrap(base, better, sharpe_rows, resamples=1000).p_value < 0.01
        assert paired_bootstrap(better, base, sharpe_rows, resamples=1000).p_value > 0.99

    def test_misaligned_series_are_refused(self) -> None:
        with pytest.raises(ValueError):
            paired_bootstrap([0.1, 0.2, 0.3], [0.1, 0.2], mean_rows)


class TestPBO:
    def test_sixteen_partitions_give_12870_splits(self) -> None:
        r = np.random.default_rng(8).normal(0, 0.01, (1600, 6))
        res = pbo(r, 16)
        assert res.splits == math.comb(16, 8) == 12870 and res.logits.shape == (12870,)

    def test_pure_noise_overfits_about_half_the_time(self) -> None:
        r = np.random.default_rng(9).normal(0, 0.01, (1600, 10))
        assert 0.2 < pbo(r).pbo < 0.8

    def test_a_persistent_edge_does_not_overfit(self) -> None:
        r = np.random.default_rng(10).normal(0, 0.01, (1600, 10))
        r[:, 3] += 0.001  # one trial with a real, persistent edge (Sharpe about 1.6 a year)
        assert pbo(r).pbo < 0.05

    def test_invalid_inputs_are_refused(self) -> None:
        with pytest.raises(ValueError):
            pbo(np.zeros((100, 1)))
        with pytest.raises(ValueError):
            pbo(np.zeros((100, 3)), partitions=15)
        with pytest.raises(ValueError):
            pbo(np.zeros((20, 3)), partitions=16)
