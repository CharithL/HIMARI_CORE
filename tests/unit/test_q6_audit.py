"""Q6 over a whole window (v1.10: the M6 baseline waits until every fund passes against IBKR),
and the ingestion commands' preconditions."""

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from himari.cli import app
from himari.core.config import load_config
from himari.core.types import Bar
from himari.data.coverage import cross_check_audit
from himari.sim.run import ingest_demo_fixtures

ROOT = Path(__file__).parents[2]
CONFIG = ROOT / "config" / "frozen_v1.yaml"


def bar(day: int, close: float, source: str) -> Bar:
    return Bar(symbol="X", session_date=date(2025, 1, day), open=close, high=close, low=close,
               close=close, volume=1, source=source)  # fmt: skip


def test_the_audit_counts_mismatches_and_missing_sessions() -> None:
    primary = {"X": [bar(6, 10.0, "p"), bar(7, 10.0, "p"), bar(8, 10.0, "p"), bar(9, 10.0, "p")]}
    cross = {"X": [bar(6, 10.04, "c"), bar(7, 10.2, "c"), bar(9, 10.0, "c")]}
    (a,) = cross_check_audit(primary, cross, ["X"], date(2025, 1, 6), date(2025, 1, 9), 0.005)
    assert (a.shared, a.mismatches, a.missing) == (3, [(date(2025, 1, 7), 10.0, 10.2)],
                                                   [date(2025, 1, 8)])  # fmt: skip
    assert not a.passed
    (b,) = cross_check_audit(primary, cross, ["X"], date(2025, 1, 9), date(2025, 1, 9), 0.005)
    assert b.passed
    (c,) = cross_check_audit({}, cross, ["X"], date(2025, 1, 6), date(2025, 1, 9), 0.005)
    assert c.shared == 0 and not c.passed  # nothing to compare is not a pass


def test_q6_command_on_the_fixture_store(tmp_path: Path) -> None:
    """The eight funds agree with the fixture's cross-check source; CBIL has no data there."""
    ingest_demo_fixtures(load_config(CONFIG), ROOT, tmp_path / "clean")
    args = ["data", "q6", "--primary", "csv", "--crosscheck", "xcheck", "--from", "2024-01-02",
            "--to", "2025-12-31", "--data-dir", str(tmp_path), "--config", str(CONFIG)]  # fmt: skip
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    lines = {line.split()[1].rstrip(":"): line.split()[0] for line in result.output.splitlines()}
    assert lines.pop("CBIL") == "FAIL" and set(lines.values()) == {"PASS"} and len(lines) == 8


@pytest.mark.parametrize("source", ["eodhd", "ibkr"])
def test_ingestion_preconditions(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EODHD needs its key; IBKR needs EODHD's splits in the store first."""
    monkeypatch.delenv("VENDOR_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    args = ["data", "update", "--source", source, "--data-dir", str(tmp_path),
            "--config", str(CONFIG)]  # fmt: skip
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    expected = "VENDOR_API_KEY" if source == "eodhd" else "Ingest EODHD first"
    assert expected in result.output
