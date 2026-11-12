"""Appendix F stress goldens, computed by the real stress model and gate (§8.5)."""

from pathlib import Path

import pytest

from himari.core.config import load_config
from himari.gates.stress import apply_stress_gate
from himari.risk.stress import bond_price_change, stress_test

STRESS = load_config(Path(__file__).parents[2] / "config" / "frozen_v1.yaml").stress

EXAMPLE_1 = {"VCN": 0.05, "VUN": 0.05, "VGV": 0.25, "CGL.C": 0.10, "DLR": 0.15}
EXAMPLE_2 = {
    "VCN": 0.055,
    "VUN": 0.055,
    "VIU": 0.055,
    "VEE": 0.055,
    "VGV": 0.229,
    "CGL.C": 0.229,
    "PDBC": 0.183,
    "DLR": 0.138,
}


class TestBondPriceChange:
    def test_plus_and_minus_50bp(self) -> None:
        vgv = STRESS.loadings["VGV"]["CA_YIELD"]
        assert not isinstance(vgv, float)
        assert bond_price_change(vgv.duration, vgv.convexity, 0.005) == pytest.approx(
            -0.036625, abs=1e-15
        )
        assert bond_price_change(vgv.duration, vgv.convexity, -0.005) == pytest.approx(
            0.038375, abs=1e-15
        )


class TestStressExample1:
    def test_losses_and_scale(self) -> None:
        r = stress_test(EXAMPLE_1, STRESS)
        assert r.worst_single == pytest.approx(0.015000, abs=5e-7)
        assert r.worst_single_name == "CAD_UP+"
        assert r.joint == pytest.approx(0.024156, abs=5e-7)
        assert r.scale == 1.0

    def test_gate_leaves_weights_alone(self) -> None:
        out, log = apply_stress_gate(EXAMPLE_1, STRESS)
        assert out == EXAMPLE_1 and not log.bound


class TestStressExample2:
    def test_losses_and_scale(self) -> None:
        r = stress_test(EXAMPLE_2, STRESS)
        assert r.worst_single == pytest.approx(0.035750, abs=5e-7)
        assert r.worst_single_name == "CAD_UP+"
        assert r.joint == pytest.approx(0.060407, abs=5e-7)
        assert r.scale == pytest.approx(0.827717, abs=5e-7)

    def test_after_scaling(self) -> None:
        out, log = apply_stress_gate(EXAMPLE_2, STRESS)
        assert log.bound and log.scale_applied == pytest.approx(0.827717, abs=5e-7)
        again = stress_test(out, STRESS)
        assert again.worst_single == pytest.approx(0.029591, abs=5e-7)
        assert again.joint == pytest.approx(0.050000, abs=1e-12)
        assert again.scale == pytest.approx(1.0, abs=1e-12)

    def test_cad_up_diagnostic_is_logged_not_applied(self) -> None:
        r = stress_test(EXAMPLE_2, STRESS)
        assert r.all_adverse_including_cad_up > r.joint  # CAD +5% on top of the joint scenario
        _, log = apply_stress_gate(EXAMPLE_2, STRESS)
        assert log.details["diagnostic_all_adverse_including_cad_up"] == pytest.approx(
            r.all_adverse_including_cad_up
        )
        assert log.scale_applied == pytest.approx(r.scale)
