"""`himari report baseline --demo` (M5): the research pack on the fixtures, every run logged,
and the research report written; `himari register status` reads the register."""

import sqlite3
from contextlib import closing
from pathlib import Path

from typer.testing import CliRunner

from himari.cli import app

runner = CliRunner()


def test_demo_research_report(tmp_path: Path) -> None:
    result = runner.invoke(app, ["report", "baseline", "--demo", "--state-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Research gate at 2x: FAIL" in result.output  # one synthetic year cannot pass
    # a negative leave-one-out row fails even though the year rows cannot be computed
    assert "FAIL No dependence on a single crisis or instrument" in result.output
    assert "PDBC left out" in result.output
    (run,) = (tmp_path / "runs").iterdir()
    page = (run / "research.html").read_text(encoding="utf-8")
    for heading in (
        "Research gate (§12.7)",
        "Metrics (§12.2)",
        "Benchmarks (§12.3)",
        "Statistics (§12.4)",
        "Robustness pack (§12.3)",
        "Gate statistics",
    ):
        assert heading in page
    assert "all signals on" in page and "equal-weight timing" in page and "CAD cash" in page
    assert "0 real-data rows (10 fixture or demo rows excluded" in page
    for name in ("1x-all-signals-on", "2x-equal-weight-timing", "2x-fills-one-session-later"):
        assert (run / name / "decisions.jsonl").exists()
    with closing(sqlite3.connect(tmp_path / "trials.sqlite")) as con:
        hyps = [h for (h,) in con.execute("SELECT hypothesis_id FROM trials ORDER BY seq")]
        returns = con.execute("SELECT COUNT(*) FROM trial_returns").fetchone()[0]
    assert hyps == ["baseline", "baseline"] + ["adhoc"] * 8 and returns > 0


def test_register_status() -> None:
    result = runner.invoke(app, ["register", "status"])
    assert result.exit_code == 0, result.output
    assert "H1" in result.output and "Endpoints reached: 0 of 10" in result.output


def test_the_real_baseline_report_waits_for_m6(tmp_path: Path) -> None:
    result = runner.invoke(app, ["report", "baseline", "--state-dir", str(tmp_path)])
    assert result.exit_code == 2 and "M6" in result.output
