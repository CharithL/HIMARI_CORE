"""Q6 over a whole window (v1.10: the M6 baseline waits until every fund passes against IBKR;
v1.11: a missing IBKR bar is allowed only on a session without trades), and the ingestion
commands' preconditions."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from himari.cli import app
from himari.core.config import load_config
from himari.core.types import Bar
from himari.data.coverage import cross_check_audit
from himari.data.storage import BARS, VersionedStore, bar_row
from himari.sim.run import ingest_demo_fixtures

ROOT = Path(__file__).parents[2]
CONFIG = ROOT / "config" / "frozen_v1.yaml"


def bar(day: int, close: float, source: str, volume: int = 1, symbol: str = "X") -> Bar:
    return Bar(symbol=symbol, session_date=date(2025, 1, day), open=close, high=close, low=close,
               close=close, volume=volume, source=source)  # fmt: skip


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


def test_only_a_session_without_trades_may_lack_a_cross_check_bar() -> None:
    """v1.11: no IBKR bar is expected on a day with no trades, so a missing cross-check bar
    passes only when the primary shows zero volume, and it is listed; any other one fails."""
    primary = {"X": [bar(6, 10.0, "p"), bar(7, 10.0, "p", volume=0), bar(8, 10.0, "p")]}
    cross = {"X": [bar(6, 10.0, "c"), bar(8, 10.0, "c")]}
    (a,) = cross_check_audit(primary, cross, ["X"], date(2025, 1, 6), date(2025, 1, 8), 0.005)
    assert (a.shared, a.missing, a.no_trade, a.passed) == (2, [], [date(2025, 1, 7)], True)
    traded = {"X": [*primary["X"], bar(9, 10.0, "p")]}  # it traded on the 9th: IBKR should have it
    (b,) = cross_check_audit(traded, cross, ["X"], date(2025, 1, 6), date(2025, 1, 9), 0.005)
    assert (b.missing, b.no_trade, b.passed) == ([date(2025, 1, 9)], [date(2025, 1, 7)], False)


def test_q6_report_lists_the_sessions_without_trades(tmp_path: Path) -> None:
    t = datetime(2026, 1, 1, tzinfo=UTC)
    eodhd = [bar(d, 10.0, "eodhd", volume=0 if d == 7 else 100, symbol="VCN") for d in (6, 7, 8, 9)]
    ibkr = [bar(d, 10.0, "ibkr", symbol="VCN") for d in (6, 8)]
    VersionedStore(tmp_path / "clean").append(BARS, [bar_row(b, t) for b in eodhd + ibkr], t)
    args = ["data", "q6", "--from", "2025-01-06", "--to", "2025-01-09", "--data-dir",
            str(tmp_path), "--config", str(CONFIG)]  # fmt: skip
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "FAIL VCN: 2 shared sessions, 0 mismatches, 1 missing, 1 without trades" in result.output
    assert "zero volume on eodhd and no ibkr bar (allowed): 2025-01-07" in result.output
    assert "traded on eodhd but no ibkr bar (fails): 2025-01-09" in result.output


def test_q6_command_on_the_fixture_store(tmp_path: Path) -> None:
    """The eight funds agree with the fixture's cross-check source; CBIL has no data there."""
    ingest_demo_fixtures(load_config(CONFIG), ROOT, tmp_path / "clean")
    args = ["data", "q6", "--primary", "csv", "--crosscheck", "xcheck", "--from", "2024-01-02",
            "--to", "2025-12-31", "--data-dir", str(tmp_path), "--config", str(CONFIG)]  # fmt: skip
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    lines = {
        line.split()[1].rstrip(":"): line.split()[0]
        for line in result.output.splitlines()
        if line.startswith(("PASS", "FAIL"))
    }
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
