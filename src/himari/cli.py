"""HIMARI CLI — the single entry point for all commands."""

from __future__ import annotations

import os
from pathlib import Path

import typer

app = typer.Typer(name="himari", help="HIMARI Core: rules-based multi-asset trend following")

config_app = typer.Typer(help="Configuration commands")
data_app = typer.Typer(help="Data management commands")
register_app = typer.Typer(help="Research register commands")

app.add_typer(config_app, name="config")
app.add_typer(data_app, name="data")
app.add_typer(register_app, name="register")


def _default_config_path() -> Path:
    return Path("config/frozen_v1.yaml")


@config_app.command("hash")
def config_hash_cmd(
    config: Path = typer.Option(_default_config_path(), help="Path to config YAML"),
) -> None:
    """Print the SHA-256 hash of the config file."""
    from himari.core.config import config_hash

    h = config_hash(config)
    typer.echo(f"config_hash: {h}")
    typer.echo(f"file: {config}")


@data_app.command("update")
def data_update(
    source: str = typer.Option(..., help="Source: vendor|ibkr|boc|csv"),
    from_date: str = typer.Option(None, "--from", help="Start date YYYY-MM-DD"),
) -> None:
    """Pull fresh data from a source."""
    typer.echo(f"[M1] data update from {source} (not yet implemented)")


@data_app.command("check")
def data_check() -> None:
    """Run the data quality gate and print faults."""
    typer.echo("[M1] data check (not yet implemented)")


@data_app.command("confirm")
def data_confirm(
    symbol: str = typer.Argument(...),
    date: str = typer.Argument(...),
    reason: str = typer.Option(..., help="Why this anomaly is accepted"),
) -> None:
    """Manually confirm a data anomaly."""
    typer.echo(f"[M1] confirming {symbol} {date}: {reason} (not yet implemented)")


@app.command()
def backtest(
    config: Path = typer.Option(_default_config_path(), help="Config file"),
    costs: str = typer.Option("2x", help="Cost multiplier: 1x or 2x"),
    from_date: str = typer.Option("2015-01-01", "--from", help="Start date"),
    to_date: str = typer.Option("2025-12-31", "--to", help="End date"),
) -> None:
    """Run a historical backtest."""
    typer.echo(f"[M4] backtest {from_date} to {to_date} at {costs} costs (not yet implemented)")


@register_app.command("run")
def register_run(
    hypothesis: str = typer.Argument(..., help="Hypothesis ID, e.g. H1"),
) -> None:
    """Run a single registered test."""
    typer.echo(f"[M8] running {hypothesis} (not yet implemented)")


@register_app.command("status")
def register_status() -> None:
    """Show the status of all registered tests."""
    typer.echo("[M8] register status (not yet implemented)")


@app.command("shadow")
def shadow(
    broker: str = typer.Option("quote_paper", help="Broker: quote_paper|ibkr_paper"),
    crypto: bool = typer.Option(False, help="Include crypto comparator"),
) -> None:
    """Start or resume the shadow run."""
    typer.echo(f"[M7] shadow run with {broker} (not yet implemented)")


@app.command("report")
def report(
    report_type: str = typer.Argument(..., help="weekly|baseline|register"),
) -> None:
    """Generate a report."""
    typer.echo(f"[M5] generating {report_type} report (not yet implemented)")


@app.command("dashboard")
def dashboard() -> None:
    """Launch the Streamlit dashboard."""
    typer.echo("[M7] dashboard (not yet implemented)")


@app.command("kill")
def kill(
    reason: str = typer.Option(..., help="Why the kill switch is activated"),
) -> None:
    """Activate the kill switch — cancels open orders, blocks new ones."""
    typer.echo(f"[M3] KILL activated: {reason} (not yet implemented)")


@app.command("unkill")
def unkill(
    reason: str = typer.Option(..., help="Why the kill switch is being cleared"),
) -> None:
    """Clear the kill switch."""
    typer.echo(f"[M3] KILL cleared: {reason} (not yet implemented)")


@app.command("resume")
def resume(
    reason: str = typer.Option(..., help="Why the suspension is being cleared"),
) -> None:
    """Clear a drawdown suspension — requires a reason."""
    typer.echo(f"[M3] SUSPENDED cleared: {reason} (not yet implemented)")


@app.command("promote")
def promote(
    stage: int = typer.Option(..., help="Stage number"),
    reason: str = typer.Option(..., help="Promotion rationale"),
) -> None:
    """Write a promotion record (user only)."""
    if os.environ.get("LIVE_TRADING_ENABLED", "false").lower() == "true":
        typer.echo("ERROR: LIVE_TRADING_ENABLED must be false during development", err=True)
        raise typer.Exit(1)
    typer.echo(f"[M7] promoting stage {stage}: {reason} (not yet implemented)")


if __name__ == "__main__":
    app()
