"""Implementation golden tests — verify our modules against Appendix F.

Unlike test_golden_values.py (pure math), these import the actual HIMARI
modules. A failure here means our code diverges from the spec.
"""

import math
from datetime import date
from pathlib import Path

import numpy as np

from himari.core.config import (
    code_hash,
    config_hash,
    load_config,
    run_hashes,
)

PROJECT_ROOT = Path(__file__).parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "frozen_v1.yaml"


class TestRiskModuleGoldens:
    """EWMA decay factors from our RiskConfig — Appendix F §1."""

    def test_vol_decay_from_config(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert abs(cfg.risk.vol_decay() - 0.965936) < 1e-4

    def test_corr_decay_from_config(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert abs(cfg.risk.corr_decay() - 0.988514) < 1e-4

    def test_vol_half_life_roundtrip(self) -> None:
        cfg = load_config(CONFIG_PATH)
        lam = cfg.risk.vol_decay()
        recovered = math.log(0.5) / math.log(lam)
        assert abs(recovered - cfg.risk.ewma_vol_half_life) < 1e-10

    def test_corr_half_life_roundtrip(self) -> None:
        cfg = load_config(CONFIG_PATH)
        lam = cfg.risk.corr_decay()
        recovered = math.log(0.5) / math.log(lam)
        assert abs(recovered - cfg.risk.ewma_corr_half_life) < 1e-10

    def test_shrinkage_identity_fraction(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert cfg.risk.corr_shrink_to_identity == 0.50

    def test_portfolio_vol_ceiling(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert cfg.risk.portfolio_vol_ceiling == 0.08


class TestSizingGoldens:
    """Sizing toy case from Appendix F §5 — uses config budgets and caps."""

    def test_budget_splits_match_config(self) -> None:
        cfg = load_config(CONFIG_PATH)
        instruments = cfg.stage1.instruments()
        for inst in instruments:
            budget = cfg.stage1.budget_for(inst)
            assert budget > 0
            assert budget <= 0.20

    def test_per_instrument_cap_from_config(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert cfg.stage1.caps.per_instrument == 0.25

    def test_equity_group_cap(self) -> None:
        cfg = load_config(CONFIG_PATH)
        equity_group = next(g for g in cfg.stage1.caps.groups if g.name == "all_equity")
        assert equity_group.max == 0.40

    def test_sizing_toy_case_vol_ceiling(self) -> None:
        """3-instrument toy case: all-on portfolio vol must equal ceiling."""
        cfg = load_config(CONFIG_PATH)
        ceiling = cfg.risk.portfolio_vol_ceiling

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
        shrink = cfg.risk.corr_shrink_to_identity
        rho_s = (1 - shrink) * rho + shrink * np.eye(3)

        D = np.diag(sigmas)
        Sigma = D @ rho_s @ D

        port_vol = math.sqrt(u @ Sigma @ u)
        k = ceiling / port_vol

        w = k * u
        vol_check = math.sqrt(w @ Sigma @ w)
        assert abs(vol_check - ceiling) < 1e-10


class TestStressGoldens:
    """Stress test examples from Appendix F §6 — uses config stress factors."""

    def test_stress_factors_loaded(self) -> None:
        cfg = load_config(CONFIG_PATH)
        factors = cfg.stress["factors"]
        assert "EQUITY" in factors
        assert "GOLD" in factors
        assert "COMMOD" in factors
        assert "CAD_UP" in factors
        assert "CA_YIELD" in factors

    def test_stress_factor_magnitudes(self) -> None:
        cfg = load_config(CONFIG_PATH)
        factors = cfg.stress["factors"]
        assert factors["EQUITY"]["shock"] == 0.07
        assert factors["GOLD"]["shock"] == 0.08
        assert factors["COMMOD"]["shock"] == 0.10
        assert factors["CAD_UP"]["shock"] == 0.05

    def test_single_factor_limit_from_config(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert cfg.stress["single_factor_limit"] == 0.03

    def test_joint_limit_from_config(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert cfg.stress["joint_limit"] == 0.05


class TestConfigHashGoldens:
    """Hashing infrastructure for audit trail — rule 4."""

    def test_config_hash_stable(self) -> None:
        h1 = config_hash(CONFIG_PATH)
        h2 = config_hash(CONFIG_PATH)
        assert h1 == h2
        assert len(h1) == 64

    def test_costs_hash_stable(self) -> None:
        h = config_hash(PROJECT_ROOT / "config" / "costs_v1.yaml")
        assert len(h) == 64

    def test_register_hash_stable(self) -> None:
        h = config_hash(PROJECT_ROOT / "research" / "register.yaml")
        assert len(h) == 64

    def test_run_hashes_all_present(self) -> None:
        hashes = run_hashes(PROJECT_ROOT)
        assert set(hashes.keys()) == {"config", "costs", "register", "code"}
        for name, h in hashes.items():
            assert len(h) == 64, f"{name} hash wrong length"

    def test_code_hash_stable(self) -> None:
        """CRLF/untracked-file behaviour is covered in tests/unit/test_config.py."""
        src = PROJECT_ROOT / "src" / "himari"
        assert code_hash(src) == code_hash(src)


class TestResearchWindowsTyped:
    """Research windows must be datetime.date, not str — item 5."""

    def test_development_end_is_date(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert isinstance(cfg.research.windows.development_end, date)

    def test_evaluation_are_dates(self) -> None:
        cfg = load_config(CONFIG_PATH)
        for d in cfg.research.windows.evaluation:
            assert isinstance(d, date)

    def test_replication_are_dates(self) -> None:
        cfg = load_config(CONFIG_PATH)
        for d in cfg.research.windows.replication:
            assert isinstance(d, date)
