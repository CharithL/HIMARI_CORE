"""HIMARI CLI — the single entry point for all commands."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from himari.risk.state_machine import SafetyStore

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
    holidays: Path = typer.Option(_DEFAULT_HOLIDAYS, help="Forward Bank of Canada holiday file"),
    data_dir: Path = typer.Option(Path("data"), help="Data root (raw/ and clean/)"),
    state_dir: Path = typer.Option(Path("state"), help="State root (logs/)"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
) -> None:
    """Pull data from a source into versioned storage."""
    from himari.core.config import load_config
    from himari.data.calendars import load_boc_holidays
    from himari.data.fx import splice_overlap, write_overlap_log
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
    forward = load_boc_holidays(holidays)
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
        results["series"] = {
            "all": ingest_series_source(src, store, names, forward, start, end, observed, eod)
        }
    else:
        boc = BocValetSource(cfg.sources.boc_valet_base)
        raw = RawStore(data_dir / "raw")
        results["series"] = {
            "all": ingest_boc(boc, raw, store, names, forward, start, end, observed, eod)
        }
    for table, by_key in results.items():
        for key, r in by_key.items():
            typer.echo(f"{table:8} {key:10} +{r.added} new, {r.corrected} corrected")
    overlap = splice_overlap(store.series(names.fx_legacy), store.series(names.fx))
    if overlap:
        log = state_dir / "logs" / "fx_splice_overlap.csv"
        write_overlap_log(overlap, log)
        worst = max(overlap, key=lambda r: abs(r.rel_diff))
        mean = sum(r.rel_diff for r in overlap) / len(overlap)
        typer.echo(
            f"FX splice overlap: {len(overlap)} days {overlap[0].obs_date} to "
            f"{overlap[-1].obs_date}; mean {mean:+.4%}, largest {worst.rel_diff:+.4%} "
            f"on {worst.obs_date}; log {log}"
        )


@data_app.command("check")
def data_check(
    as_of: str = typer.Option(None, help="Decision date YYYY-MM-DD (default today)"),
    primary: str = typer.Option("csv", help="Primary source name"),
    crosscheck: str = typer.Option(None, help="Cross-check source name"),
    holidays: Path = typer.Option(_DEFAULT_HOLIDAYS, help="Forward Bank of Canada holiday file"),
    data_dir: Path = typer.Option(Path("data"), help="Data root"),
    state_dir: Path = typer.Option(Path("state"), help="State root"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
) -> None:
    """Run the data-quality gate as of a decision time and print faults."""
    from himari.core.config import load_config
    from himari.data.calendars import last_completed_session, load_boc_holidays, release_utc
    from himari.data.fx import splice_fx
    from himari.data.ingest import SeriesNames, boc_calendar
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
    names = SeriesNames(cfg.sources.fx_series, cfg.sources.cash_rate_series)
    fx = splice_fx(store.series(names.fx_legacy, as_of_utc), store.series(names.fx, as_of_utc))
    inputs = QualityInputs(
        instruments=instruments,
        bars=store.bars(primary, as_of_utc),
        actions=store.actions(primary, as_of_utc),
        crosscheck=store.bars(crosscheck, as_of_utc) if crosscheck else {},
        fx=fx,
        corra=store.series(cfg.sources.cash_rate_series, as_of_utc),
        last_sessions={
            i.symbol: last_completed_session(codes[i.exchange.value], as_of_utc)
            for i in instruments
        },
        master_session=last_completed_session(cfg.calendars.master, as_of_utc),
        as_of_date=day,
        boc_days=boc_calendar(store, names, load_boc_holidays(holidays)),
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


@data_app.command("boc-calendar")
def data_boc_calendar(
    from_date: str = typer.Option(None, "--from", help="Start date YYYY-MM-DD"),
    to_date: str = typer.Option(None, "--to", help="End date YYYY-MM-DD"),
    holidays: Path = typer.Option(_DEFAULT_HOLIDAYS, help="Forward Bank of Canada holiday file"),
    data_dir: Path = typer.Option(Path("data"), help="Data root"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
) -> None:
    """List publication-record disagreements for manual review (v1.3)."""
    from himari.core.config import load_config
    from himari.data.calendars import load_boc_holidays, review_record
    from himari.data.ingest import SeriesNames
    from himari.data.storage import VersionedStore

    cfg = load_config(config)
    names = SeriesNames(cfg.sources.fx_series, cfg.sources.cash_rate_series)
    store = VersionedStore(data_dir / "clean")
    fx = [o.obs_date for s in (names.fx_legacy, names.fx) for o in store.series(s)]
    corra = [o.obs_date for o in store.series(names.cash_rate)]
    if not fx and not corra:
        typer.echo("No Bank of Canada series stored yet; run: himari data update --source boc")
        raise typer.Exit(1)
    start = _parse_date(from_date, min(fx + corra))
    end = _parse_date(to_date, max(fx + corra))
    review = review_record(fx, corra, load_boc_holidays(holidays), start, end)
    typer.echo(f"Bank of Canada publication record {start} to {end}")
    for label, days in (
        ("FX published, no CORRA", review.fx_only),
        ("CORRA published, no FX", review.corra_only),
        ("holiday file says closed, Bank published", review.file_closed_but_published),
        ("holiday file says open, nothing published", review.file_open_but_unpublished),
    ):
        listed = ", ".join(d.isoformat() for d in days) if days else "none"
        typer.echo(f"  {label} ({len(days)}): {listed}")


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


def _safety(state_dir: Path, check_chain: bool = True) -> SafetyStore:
    from himari.risk.state_machine import SafetyStore

    return SafetyStore(
        state_dir / "himari.sqlite", kill_file=state_dir / "KILL", check_chain=check_chain
    )


def _actor() -> str:
    import getpass

    return getpass.getuser()


@app.command("kill")
def kill(
    reason: str = typer.Option(..., help="Why the kill switch is activated"),
    state_dir: Path = typer.Option(Path("state"), help="State root"),
) -> None:
    """Activate the kill switch: no new orders, open orders cancelled; nothing is liquidated."""
    try:
        _safety(state_dir).kill(reason, _actor(), _utc_now())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"KILL active: {reason.strip()}")


@app.command("unkill")
def unkill(
    reason: str = typer.Option(..., help="Why the kill switch is being cleared"),
    state_dir: Path = typer.Option(Path("state"), help="State root"),
) -> None:
    """Clear the kill switch (also removes a KILL file in the state folder)."""
    try:
        event = _safety(state_dir).unkill(reason, _actor(), _utc_now())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    removed = " (KILL file removed)" if event.detail.get("kill_file_removed") else ""
    typer.echo(f"KILL cleared{removed}: {reason.strip()}")


@app.command("resume")
def resume(
    reason: str = typer.Option(..., help="Why the suspension is being cleared"),
    state_dir: Path = typer.Option(Path("state"), help="State root"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
) -> None:
    """Clear a drawdown suspension; requires a reason and writes a signed record."""
    from himari.core.config import load_config
    from himari.risk.state_machine import DrawdownTracker, ResumeRefusedError

    tracker = DrawdownTracker(_safety(state_dir), load_config(config).drawdown)
    try:
        status = tracker.resume(
            reason, _actor(), _utc_now(), load_config(config).account.loss_floor_cad
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except ResumeRefusedError as exc:
        typer.echo(f"Resume refused: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Drawdown state now {status.state.value}; reference reset to NAV {status.nav:,.2f}")


@app.command("status")
def status(
    state_dir: Path = typer.Option(Path("state"), help="State root"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
) -> None:
    """Show the persisted safety states and whether their record chain is intact."""
    from himari.core.config import load_config
    from himari.risk.state_machine import DrawdownTracker

    store = _safety(state_dir, check_chain=False)  # report a broken chain instead of failing
    dd = DrawdownTracker(store, load_config(config).drawdown).status()
    typer.echo(f"KILL         {'ON' if store.kill_active() else 'off'}")
    typer.echo(f"VENUE_FAULT  {'ON' if store.venue_fault_active() else 'off'}")
    typer.echo(f"DRAWDOWN     {dd.state.value} (drawdown {dd.drawdown:.2%})")
    typer.echo(f"record chain {'intact' if store.verify() else 'BROKEN'}")


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
