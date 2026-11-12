"""HIMARI CLI — the single entry point for all commands."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timezone
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


_DEFAULT_HOLIDAYS = Path("config/calendars/boc_holidays.yaml")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: str | None, default: date) -> date:
    return date.fromisoformat(value) if value else default


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
    source: str = typer.Option(..., help="Source: csv|boc|vendor|ibkr"),
    from_date: str = typer.Option(None, "--from", help="Start date YYYY-MM-DD"),
    to_date: str = typer.Option(None, "--to", help="End date YYYY-MM-DD (default today)"),
    csv_dir: Path = typer.Option(None, "--dir", help="CSV root (bars/, actions/, series/)"),
    name: str = typer.Option("csv", help="Source name stored with CSV rows"),
    holidays: Path = typer.Option(_DEFAULT_HOLIDAYS, help="Bank of Canada holiday file"),
    data_dir: Path = typer.Option(Path("data"), help="Data root (raw/ and clean/)"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
) -> None:
    """Pull data from a source into versioned storage."""
    from himari.core.config import load_config
    from himari.data.calendars import load_boc_holidays
    from himari.data.ingest import (
        SeriesNames,
        ingest_actions,
        ingest_bars,
        ingest_boc,
        ingest_series_source,
    )
    from himari.data.sources import BocValetSource, CsvSource
    from himari.data.storage import AppendResult, RawStore, VersionedStore

    if source == "vendor":
        typer.echo("No end-of-day vendor chosen yet (build guide §20).", err=True)
        raise typer.Exit(1)
    if source == "ibkr":
        typer.echo("IBKR data arrives in M7.", err=True)
        raise typer.Exit(1)
    if source not in ("csv", "boc"):
        raise typer.BadParameter(f"unknown source {source!r}")

    cfg = load_config(config)
    start = _parse_date(from_date, date(2000, 1, 1))
    end = _parse_date(to_date, _utc_now().date())
    store = VersionedStore(data_dir / "clean")
    names = SeriesNames(cfg.sources.fx_series, cfg.sources.cash_rate_series)
    boc_days = load_boc_holidays(holidays)
    observed = _utc_now()
    eod = time.fromisoformat(cfg.schedule.daily_job_after_et)
    results: dict[str, dict[str, AppendResult]] = {}
    if source == "csv":
        if csv_dir is None:
            raise typer.BadParameter("--dir is required for --source csv")
        src = CsvSource(csv_dir, name=name)
        symbols = [i.symbol for i in cfg.stage1.instruments()]
        results["bars"] = ingest_bars(src, store, symbols, start, end, observed, eod)
        results["actions"] = ingest_actions(src, store, symbols, start, end, observed, eod)
        results["series"] = ingest_series_source(src, store, names, boc_days, start, end, observed)
    else:
        boc = BocValetSource(cfg.sources.boc_valet_base)
        raw = RawStore(data_dir / "raw")
        results["series"] = ingest_boc(boc, raw, store, names, boc_days, start, end, observed)
    for table, by_key in results.items():
        for key, r in by_key.items():
            typer.echo(f"{table:8} {key:10} +{r.added} new, {r.corrected} corrected")


@data_app.command("check")
def data_check(
    as_of: str = typer.Option(None, help="Decision date YYYY-MM-DD (default today)"),
    primary: str = typer.Option("csv", help="Primary source name"),
    crosscheck: str = typer.Option(None, help="Cross-check source name"),
    holidays: Path = typer.Option(_DEFAULT_HOLIDAYS, help="Bank of Canada holiday file"),
    data_dir: Path = typer.Option(Path("data"), help="Data root"),
    state_dir: Path = typer.Option(Path("state"), help="State root"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
) -> None:
    """Run the data-quality gate as of a decision time and print faults."""
    from himari.core.config import load_config
    from himari.data.calendars import last_completed_session, load_boc_holidays, release_utc
    from himari.data.storage import VersionedStore
    from himari.quality.checks import Severity
    from himari.quality.confirmations import ConfirmationStore
    from himari.quality.gate import QualityInputs, run_quality_gate

    cfg = load_config(config)
    day = _parse_date(as_of, _utc_now().date())
    as_of_utc = release_utc(day, time.fromisoformat(cfg.schedule.daily_job_after_et))
    store = VersionedStore(data_dir / "clean")
    instruments = cfg.stage1.instruments()
    codes = cfg.calendars.by_exchange
    inputs = QualityInputs(
        instruments=instruments,
        bars=store.bars(primary, as_of_utc),
        actions=store.actions(primary, as_of_utc),
        crosscheck=store.bars(crosscheck, as_of_utc) if crosscheck else {},
        fx=store.series(cfg.sources.fx_series, as_of_utc),
        corra=store.series(cfg.sources.cash_rate_series, as_of_utc),
        last_sessions={
            i.symbol: last_completed_session(codes[i.exchange.value], as_of_utc)
            for i in instruments
        },
        master_session=last_completed_session(cfg.calendars.master, as_of_utc),
        as_of_date=day,
        boc_days=load_boc_holidays(holidays),
        fx_series=cfg.sources.fx_series,
        confirmed=ConfirmationStore(state_dir / "confirmations.jsonl").confirmed(),
    )
    report = run_quality_gate(inputs, cfg.data_quality)
    typer.echo(f"Data-quality gate as of {as_of_utc.isoformat()}")
    for sym, q in report.instruments.items():
        warnings = sum(1 for r in q.results if r.severity == Severity.WARN)
        typer.echo(f"  {sym:6} {q.status:5} faults={len(q.faults)} warnings={warnings}")
        for r in q.faults:
            flag = r.flag.value if r.flag else ""
            typer.echo(f"      {r.check} {flag} {r.session_date or ''} {r.detail}")
    for r in report.portfolio:
        if r.severity != Severity.PASS:
            typer.echo(f"  portfolio {r.check} {r.severity.value} {r.detail}")


@data_app.command("confirm")
def data_confirm(
    symbol: str = typer.Argument(..., help="Instrument, or FXUSDCAD for an FX outage"),
    day: str = typer.Argument(..., metavar="DATE", help="Session date YYYY-MM-DD"),
    reason: str = typer.Option(..., help="Why this anomaly or outage is accepted"),
    state_dir: Path = typer.Option(Path("state"), help="State root"),
) -> None:
    """Record that a person accepted a data anomaly (Q4) or an FX outage (Q7)."""
    from himari.quality.confirmations import ConfirmationStore

    store = ConfirmationStore(state_dir / "confirmations.jsonl")
    store.add(symbol, date.fromisoformat(day), reason, _utc_now())
    typer.echo(f"confirmed {symbol} {day}: {reason}")


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
