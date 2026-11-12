"""Golden tests from Appendix F of the build guide.

These values are frozen. If a test fails, the code is wrong, not the test.
"""

import math


class TestEWMADecayFactors:
    def test_vol_decay_half_life_20(self) -> None:
        lam = 0.5 ** (1.0 / 20)
        assert abs(lam - 0.965936) < 1e-4

    def test_corr_decay_half_life_60(self) -> None:
        lam = 0.5 ** (1.0 / 60)
        assert abs(lam - 0.988514) < 1e-4


class TestTrendSignal:
    def test_positive_excess_return(self) -> None:
        """Wealth +6%, cash +2.5% over 252 sessions => signal ON."""
        m = math.log(1.06) - math.log(1.025)
        assert abs(m - 0.033576) < 1e-4
        assert m > 0  # signal = 1

    def test_negative_excess_return(self) -> None:
        """Wealth +2%, cash +2.5% => signal OFF."""
        m = math.log(1.02) - math.log(1.025)
        assert abs(m - (-0.004890)) < 1e-4
        assert m < 0  # signal = 0


class TestBondPriceChange:
    def test_plus_50bp(self) -> None:
        """Duration 7.5, convexity 70, +50bp."""
        dy = 0.005
        dp = -7.5 * dy + 0.5 * 70 * dy**2
        assert abs(dp - (-0.036625)) < 1e-6

    def test_minus_50bp(self) -> None:
        """Duration 7.5, convexity 70, -50bp."""
        dy = -0.005
        dp = -7.5 * dy + 0.5 * 70 * dy**2
        assert abs(dp - 0.038375) < 1e-6


class TestHolmCorrection:
    def test_appendix_f_example(self) -> None:
        """p-values [0.01, 0.04, 0.03, 0.20], alpha=0.05 => only first rejected."""
        p_values = [0.01, 0.04, 0.03, 0.20]
        alpha = 0.05
        m = len(p_values)

        indexed = sorted(enumerate(p_values), key=lambda x: x[1])
        rejected = [False] * m

        for k, (orig_idx, p) in enumerate(indexed):
            threshold = alpha / (m - k)
            if p <= threshold:
                rejected[orig_idx] = True
            else:
                break

        assert rejected[0] is True  # p=0.01 rejected
        assert rejected[1] is False  # p=0.04 not rejected
        assert rejected[2] is False  # p=0.03 not rejected
        assert rejected[3] is False  # p=0.20 not rejected


class TestSizingToyCase:
    """3-instrument toy case from Appendix F."""

    def test_unit_weights(self) -> None:
        budgets = [0.5, 0.3, 0.2]
        sigmas = [0.15, 0.05, 0.07]
        u = [b / s for b, s in zip(budgets, sigmas)]
        assert abs(u[0] - 3.333333) < 1e-4
        assert abs(u[1] - 6.000000) < 1e-4
        assert abs(u[2] - 2.857143) < 1e-4

    def test_scaling_and_caps(self) -> None:
        import numpy as np

        budgets = [0.5, 0.3, 0.2]
        sigmas = [0.15, 0.05, 0.07]
        u = np.array([b / s for b, s in zip(budgets, sigmas)])

        rho = np.array(
            [
                [1.0, -0.2, 0.1],
                [-0.2, 1.0, 0.0],
                [0.1, 0.0, 1.0],
            ]
        )
        rho_s = 0.5 * rho + 0.5 * np.eye(3)

        D = np.diag(sigmas)
        Sigma = D @ rho_s @ D

        port_vol_all_on = math.sqrt(u @ Sigma @ u)
        k = 0.08 / port_vol_all_on

        w_all_on = k * u
        vol_check = math.sqrt(w_all_on @ Sigma @ w_all_on)
        assert abs(vol_check - 0.08) < 1e-6

        signals = [1, 1, 0]
        w = k * u * np.array(signals)

        # Per-instrument cap: 0.25
        w = np.minimum(w, 0.25)
        # Group cap for A alone: 0.40
        if w[0] > 0.40:
            w[0] = 0.40
        # C cap: 0.15
        w[2] = min(w[2], 0.15)

        assert abs(w[0] - 0.25) < 1e-6
        assert abs(w[1] - 0.25) < 1e-6
        assert abs(w[2] - 0.0) < 1e-6
        assert abs(sum(w) - 0.50) < 1e-6

        final_vol = math.sqrt(w @ Sigma @ w)
        assert abs(final_vol - 0.038324) < 1e-4


class TestStressExamples:
    def test_stress_example_1(self) -> None:
        """Weights: VCN 0.05, VUN 0.05, VGV 0.25, CGL.C 0.10, DLR 0.15.
        Worst single-factor 0.015000, joint 0.024156, scale 1.0."""
        weights = {"VCN": 0.05, "VUN": 0.05, "VGV": 0.25, "CGL.C": 0.10, "DLR": 0.15}

        # Single-factor losses
        equity_loss = (weights["VCN"] + weights["VUN"]) * 0.07
        assert abs(equity_loss - 0.007) < 1e-6

        gold_loss = weights["CGL.C"] * 0.08
        assert abs(gold_loss - 0.008) < 1e-6

        # CAD_UP: VUN, CGL.C, DLR have CAD_UP loading of -1
        cad_up_loss = (weights["VUN"] + weights["CGL.C"] + weights["DLR"]) * 0.05
        assert abs(cad_up_loss - 0.015) < 1e-6

        # Bond: VGV duration 7.5, convexity 70, +50bp
        bond_loss_up = weights["VGV"] * abs(-7.5 * 0.005 + 0.5 * 70 * 0.005**2)
        assert abs(bond_loss_up - weights["VGV"] * 0.036625) < 1e-6

        worst_single = max(equity_loss, gold_loss, cad_up_loss, bond_loss_up)
        assert abs(worst_single - 0.015) < 1e-4

        # Joint: equity -7%, gold -8%, commod -10%, yield +50bp, no FX
        joint = (
            (weights["VCN"] + weights["VUN"]) * 0.07
            + weights["CGL.C"] * 0.08
            + weights["VGV"] * 0.036625
        )
        assert abs(joint - 0.024156) < 1e-3

        scale = min(1.0, 0.03 / worst_single, 0.05 / joint)
        assert abs(scale - 1.0) < 1e-6

    def test_stress_example_2_requires_scaling(self) -> None:
        """Worst single CAD_UP = 0.035750, joint = 0.060407, scale = 0.827717."""
        weights = {
            "VCN": 0.055,
            "VUN": 0.055,
            "VIU": 0.055,
            "VEE": 0.055,
            "VGV": 0.229,
            "CGL.C": 0.229,
            "PDBC": 0.183,
            "DLR": 0.138,
        }

        cad_up_exposed = (
            weights["VUN"]
            + weights["VIU"]
            + weights["VEE"]
            + weights["CGL.C"]
            + weights["PDBC"]
            + weights["DLR"]
        )
        cad_up_loss = cad_up_exposed * 0.05
        assert abs(cad_up_loss - 0.035750) < 1e-3

        equity_loss = (weights["VCN"] + weights["VUN"] + weights["VIU"] + weights["VEE"]) * 0.07
        gold_loss = weights["CGL.C"] * 0.08
        commod_loss = weights["PDBC"] * 0.10
        bond_loss = weights["VGV"] * 0.036625

        joint = equity_loss + gold_loss + commod_loss + bond_loss
        assert abs(joint - 0.060407) < 2e-3

        scale = min(1.0, 0.03 / cad_up_loss, 0.05 / joint)
        assert abs(scale - 0.827717) < 5e-3
