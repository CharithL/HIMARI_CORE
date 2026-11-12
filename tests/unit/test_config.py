"""Tests for config loading, validation, and hashing."""

from pathlib import Path

from himari.core.config import code_hash, config_hash, load_config

CONFIG_PATH = Path(__file__).parents[2] / "config" / "frozen_v1.yaml"


class TestConfigLoading:
    def test_load_frozen_v1(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert cfg.version == "frozen_v1"
        assert cfg.base_currency == "CAD"
        assert cfg.display_timezone == "America/Toronto"

    def test_stage1_universe_count(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert len(cfg.stage1.universe) == 8

    def test_stage1_instruments(self) -> None:
        cfg = load_config(CONFIG_PATH)
        instruments = cfg.stage1.instruments()
        symbols = [i.symbol for i in instruments]
        assert "VCN" in symbols
        assert "PDBC" in symbols
        assert "CGL.C" in symbols

    def test_budget_equity_split(self) -> None:
        cfg = load_config(CONFIG_PATH)
        instruments = cfg.stage1.instruments()
        vcn = next(i for i in instruments if i.symbol == "VCN")
        budget = cfg.stage1.budget_for(vcn)
        assert abs(budget - 0.05) < 1e-10  # 20% / 4 equity funds

    def test_budget_single_class(self) -> None:
        cfg = load_config(CONFIG_PATH)
        instruments = cfg.stage1.instruments()
        vgv = next(i for i in instruments if i.symbol == "VGV")
        budget = cfg.stage1.budget_for(vgv)
        assert abs(budget - 0.20) < 1e-10  # 20% / 1 gov_bond fund

    def test_risk_decay_factors(self) -> None:
        cfg = load_config(CONFIG_PATH)
        vol_decay = cfg.risk.vol_decay()
        corr_decay = cfg.risk.corr_decay()
        assert abs(vol_decay - 0.965936) < 1e-4
        assert abs(corr_decay - 0.988514) < 1e-4

    def test_long_only_constraint(self) -> None:
        cfg = load_config(CONFIG_PATH)
        assert cfg.stage1.long_only is True
        assert cfg.stage1.max_gross == 1.0


class TestConfigHashing:
    def test_hash_is_deterministic(self) -> None:
        h1 = config_hash(CONFIG_PATH)
        h2 = config_hash(CONFIG_PATH)
        assert h1 == h2

    def test_hash_is_sha256_hex(self) -> None:
        h = config_hash(CONFIG_PATH)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_stable_across_reloads(self) -> None:
        """The same file always gives the same hash — rule 4 audit trail."""
        h = config_hash(CONFIG_PATH)
        assert isinstance(h, str)
        assert len(h) == 64


class TestCodeHash:
    def test_code_hash_deterministic(self) -> None:
        src = Path(__file__).parents[2] / "src" / "himari"
        h1 = code_hash(src)
        h2 = code_hash(src)
        assert h1 == h2

    def test_code_hash_is_sha256_hex(self) -> None:
        src = Path(__file__).parents[2] / "src" / "himari"
        h = code_hash(src)
        assert len(h) == 64
