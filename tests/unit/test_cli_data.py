"""`himari data update | check | confirm` on the fixture set."""

import json
from pathlib import Path

from typer.testing import CliRunner

from himari.cli import app

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "market"
HOLIDAYS = str(ROOT / "config" / "calendars" / "boc_holidays.yaml")
runner = CliRunner()


def update(data_dir: Path, folder: str, name: str) -> str:
    result = runner.invoke(
        app,
        [
            "data", "update", "--source", "csv", "--dir", str(FIXTURES / folder), "--name", name,
            "--from", "2024-01-01", "--to", "2025-12-31", "--holidays", HOLIDAYS,
            "--data-dir", str(data_dir), "--config", str(ROOT / "config" / "frozen_v1.yaml"),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    return result.output


def test_update_then_check(tmp_path: Path) -> None:
    out = update(tmp_path, "primary", "csv")
    assert "bars     VCN        +503 new" in out
    update(tmp_path, "crosscheck", "xcheck")
    result = runner.invoke(
        app,
        [
            "data", "check", "--as-of", "2025-12-31", "--primary", "csv", "--crosscheck", "xcheck",
            "--holidays", HOLIDAYS, "--data-dir", str(tmp_path),
            "--state-dir", str(tmp_path / "state"),
            "--config", str(ROOT / "config" / "frozen_v1.yaml"),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "VGV    OK    faults=0 warnings=1" in result.output
    assert "FAULT" not in result.output


def test_confirm_appends_a_record(tmp_path: Path) -> None:
    args = ["data", "confirm", "FXUSDCAD", "2025-04-22", "--reason", "Easter closure"]
    result = runner.invoke(app, [*args, "--state-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    (line,) = (tmp_path / "confirmations.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["symbol"] == "FXUSDCAD" and record["session_date"] == "2025-04-22"
    assert record["reason"] == "Easter closure"


def test_confirm_requires_a_reason(tmp_path: Path) -> None:
    args = ["data", "confirm", "VCN", "2025-04-22", "--reason", "  "]
    result = runner.invoke(app, [*args, "--state-dir", str(tmp_path)])
    assert result.exit_code != 0


def test_vendor_source_is_not_chosen_yet() -> None:
    result = runner.invoke(app, ["data", "update", "--source", "vendor"])
    assert result.exit_code == 1
