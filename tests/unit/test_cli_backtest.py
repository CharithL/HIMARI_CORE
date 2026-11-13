"""`himari backtest`: the demo writes its report and logs every run; a real run needs the
broker's commission schedule."""

import sqlite3
from contextlib import closing
from pathlib import Path

from typer.testing import CliRunner

from himari.cli import app

runner = CliRunner()


def test_demo_reports_and_logs_both_cost_runs(tmp_path: Path) -> None:
    result = runner.invoke(app, ["backtest", "--demo", "--quiet", "--state-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "1x costs: NAV" in result.output and "2x costs: NAV" in result.output
    (run,) = (tmp_path / "runs").iterdir()
    assert (run / "report.html").exists()
    for m in ("1x", "2x"):
        for name in ("decisions.jsonl", "nav.csv", "trades.csv", "weekly_log.txt"):
            assert (run / m / name).stat().st_size > 0
    html = (run / "report.html").read_text(encoding="utf-8")
    assert "warm-up (excluded)" in html and "gates (scale)" in html
    with closing(sqlite3.connect(tmp_path / "trials.sqlite")) as con:
        rows = con.execute("SELECT data_version, cost_multiplier FROM trials").fetchall()
    assert [m for _, m in rows] == [1.0, 2.0]
    assert all(v.startswith("fixtures:") for v, _ in rows)


def test_weekly_log_names_reasons(tmp_path: Path) -> None:
    args = ["backtest", "--demo", "--costs", "1x", "--state-dir", str(tmp_path)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "orders   buy 64 VUN [band; stress x0.949]" in result.output


def test_a_real_run_needs_the_commission_schedule(tmp_path: Path) -> None:
    result = runner.invoke(app, ["backtest", "--state-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert "never assume zero" in result.output
