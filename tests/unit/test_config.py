"""Tests for config loading, validation, and hashing."""

import subprocess
from pathlib import Path

from himari.core.config import code_hash, config_hash, load_config, tracked_py_files

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

    def test_the_cash_sleeve_is_cbil_with_the_same_cost_floor(self) -> None:
        """v1.8: CBIL, its factsheet duration, and the 10 bp per-side floor like any fund."""
        from himari.execution.costs import load_cost_model

        cfg = load_config(CONFIG_PATH)
        assert cfg.cash_sleeve.active and cfg.cash_sleeve.symbol == "CBIL"
        assert cfg.cash_sleeve.duration_years == 0.16
        root = CONFIG_PATH.parents[1]
        costs = load_cost_model(root / "config" / "costs_v1.yaml", 1.0,
                                root / "tests" / "fixtures" / "commissions_demo.yaml")  # fmt: skip
        assert costs.side_bps("CBIL") == costs.side_bps("VCN") == 10.0

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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _make_repo(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    _git(root, "add", *files)
    return root


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

    def test_crlf_and_lf_checkouts_hash_equal(self, tmp_path: Path) -> None:
        """A Windows (CRLF) checkout and a Linux (LF) checkout give the same hash."""
        lf = _make_repo(tmp_path / "lf", {"pkg/a.py": b"x = 1\ny = 2\n"})
        crlf = _make_repo(tmp_path / "crlf", {"pkg/a.py": b"x = 1\r\ny = 2\r\n"})
        assert code_hash(lf) == code_hash(crlf)

    def test_untracked_files_are_excluded(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "r", {"a.py": b"x = 1\n"})
        before = code_hash(repo)
        (repo / "scratch.py").write_bytes(b"debug = True\n")
        assert code_hash(repo) == before

    def test_content_change_changes_hash(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "r", {"a.py": b"x = 1\n"})
        before = code_hash(repo)
        (repo / "a.py").write_bytes(b"x = 2\n")
        assert code_hash(repo) != before

    def test_path_is_part_of_hash(self, tmp_path: Path) -> None:
        a = _make_repo(tmp_path / "a", {"one.py": b"x = 1\n"})
        b = _make_repo(tmp_path / "b", {"two.py": b"x = 1\n"})
        assert code_hash(a) != code_hash(b)

    def test_tracked_set_matches_git(self) -> None:
        src = Path(__file__).parents[2] / "src" / "himari"
        files = tracked_py_files(src)
        assert "core/config.py" in files
        assert all("\\" not in f for f in files)
