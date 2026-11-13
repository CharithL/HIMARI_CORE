"""`himari data update | check | confirm` on the fixture set."""

import json
import shutil
from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from himari.cli import app
from himari.quality.confirmations import ConfirmationStore

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
    state = tmp_path / "state"
    state.mkdir()
    # v1.6: Q10 needs every December split confirmed; the fixture set ships its confirmations
    shutil.copy(FIXTURES / "confirmations.jsonl", state / "confirmations.jsonl")
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


def test_confirm_records_its_check(tmp_path: Path) -> None:
    """v1.6: a confirmation names the check it answers; FXUSDCAD is always Q7."""
    for args in (
        ["VCN", "2024-12-27", "--reason", "issuer notice: all cash", "--check", "Q10"],
        ["VCN", "2025-03-04", "--reason", "real move"],
        ["FXUSDCAD", "2025-04-22", "--reason", "Easter closure", "--check", "Q10"],
    ):
        result = runner.invoke(app, ["data", "confirm", *args, "--state-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
    lines = (tmp_path / "confirmations.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["check"] for line in lines] == ["Q10", "Q4", "Q7"]
    store = ConfirmationStore(tmp_path / "confirmations.jsonl")
    assert store.confirmed("Q10") == {"VCN": {date(2024, 12, 27)}}
    assert store.confirmed() == {"VCN": {date(2025, 3, 4)}, "FXUSDCAD": {date(2025, 4, 22)}}


def test_confirm_rejects_an_unknown_check(tmp_path: Path) -> None:
    args = ["data", "confirm", "VCN", "2024-12-27", "--reason", "x", "--check", "Q99"]
    result = runner.invoke(app, [*args, "--state-dir", str(tmp_path)])
    assert result.exit_code != 0


def test_confirm_requires_a_reason(tmp_path: Path) -> None:
    args = ["data", "confirm", "VCN", "2025-04-22", "--reason", "  "]
    result = runner.invoke(app, [*args, "--state-dir", str(tmp_path)])
    assert result.exit_code != 0


def test_vendor_source_is_not_chosen_yet() -> None:
    result = runner.invoke(app, ["data", "update", "--source", "vendor"])
    assert result.exit_code == 1
