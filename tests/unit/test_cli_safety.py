"""`himari kill | unkill | resume | status`: states persist between separate commands."""

from pathlib import Path

from typer.testing import CliRunner

from himari.cli import app

ROOT = Path(__file__).parents[2]
runner = CliRunner()


def run(*args: str, state: Path) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--state-dir", str(state)])
    return result.exit_code, result.output


def test_kill_persists_between_commands(tmp_path: Path) -> None:
    assert run("kill", "--reason", "drill", state=tmp_path)[0] == 0
    code, out = run("status", "--config", str(ROOT / "config" / "frozen_v1.yaml"), state=tmp_path)
    assert code == 0 and "KILL         ON" in out and "record chain intact" in out
    assert run("unkill", "--reason", "drill over", state=tmp_path)[0] == 0
    _, out = run("status", "--config", str(ROOT / "config" / "frozen_v1.yaml"), state=tmp_path)
    assert "KILL         off" in out


def test_reasons_are_required(tmp_path: Path) -> None:
    assert run("kill", "--reason", "  ", state=tmp_path)[0] != 0
    assert run("unkill", "--reason", "", state=tmp_path)[0] != 0


def test_resume_refused_when_not_suspended(tmp_path: Path) -> None:
    config = str(ROOT / "config" / "frozen_v1.yaml")
    code, out = run("resume", "--reason", "x", "--config", config, state=tmp_path)
    assert code == 1 and "not SUSPENDED" in out
