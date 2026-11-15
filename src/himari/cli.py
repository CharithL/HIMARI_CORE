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
        confirmed_actions=ConfirmationStore(state_dir / "confirmations.jsonl").confirmed("Q10"),
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
    day: str = typer.Argument(..., metavar="DATE", help="Session or ex-date YYYY-MM-DD"),
    reason: str = typer.Option(..., help="Why this anomaly, outage or split is accepted"),
    check: str = typer.Option(
        "Q4", help="Q4 price anomaly, Q7 FX outage, Q10 December cash/reinvested split"
    ),
    state_dir: Path = typer.Option(Path("state"), help="State root"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
) -> None:
    """Record that a person checked a data issue: a price anomaly (Q4), an FX outage (Q7), or a
    December distribution's cash/reinvested split against the issuer's announcement (Q10)."""
    from himari.core.config import load_config
    from himari.quality.confirmations import CHECKS, ConfirmationStore

    check = check.upper()
    if symbol == load_config(config).sources.fx_series:
        check = "Q7"
    if check not in CHECKS:
        raise typer.BadParameter(f"--check must be one of {', '.join(CHECKS)}")
    store = ConfirmationStore(state_dir / "confirmations.jsonl")
    store.add(symbol, date.fromisoformat(day), reason, _utc_now(), check)
    typer.echo(f"confirmed {symbol} {day} ({check}): {reason}")


_COSTS = {"1x": (1.0,), "2x": (2.0,), "both": (1.0, 2.0)}


@app.command()
def backtest(
    demo: bool = typer.Option(False, "--demo", help="Run the committed synthetic fixture data"),
    costs: str = typer.Option("both", help="Cost multiplier: 1x, 2x or both"),
    from_date: str = typer.Option(None, "--from", help="Start date (default 2015-01-01)"),
    to_date: str = typer.Option(None, "--to", help="End date (default 2025-12-31)"),
    initial_cad: str = typer.Option(
        None, help="Starting cash in CAD (default: account.initial_nav_cad in the config)"
    ),
    commissions: Path = typer.Option(
        None, help="Broker commission schedule YAML (required unless --demo)"
    ),
    primary: str = typer.Option("csv", help="Primary source name"),
    crosscheck: str = typer.Option(None, help="Cross-check source name"),
    fill_at: str = typer.Option("close", help="close, or open for the sensitivity variant"),
    label: str = typer.Option("adhoc", help="Trial ledger hypothesis id: adhoc or baseline"),
    notes: str = typer.Option("", help="Trial ledger notes"),
    holidays: Path = typer.Option(_DEFAULT_HOLIDAYS, help="Forward Bank of Canada holiday file"),
    data_dir: Path = typer.Option(Path("data"), help="Data root"),
    state_dir: Path = typer.Option(Path("state"), help="State root (runs/, trials.sqlite)"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
    quiet: bool = typer.Option(False, help="Do not print the weekly log"),
) -> None:
    """Run a historical backtest; print the weekly log and write an HTML report."""
    import tempfile
    from decimal import Decimal, InvalidOperation

    from himari.core.config import load_config, run_hashes
    from himari.core.ids import run_id
    from himari.data.calendars import load_boc_holidays
    from himari.data.storage import VersionedStore
    from himari.quality.confirmations import ConfirmationStore
    from himari.research.trials import TrialLedger
    from himari.sim import run as runner
    from himari.sim.report import summarize, weekly_log

    def positive_cad(value: str) -> Decimal:
        try:
            amount = Decimal(value)
        except InvalidOperation:
            amount = Decimal(0)
        if not amount.is_finite() or amount <= 0:
            raise typer.BadParameter("--initial-cad must be a positive amount")
        return amount

    if costs not in _COSTS:
        raise typer.BadParameter("--costs must be 1x, 2x or both")
    cfg = load_config(config)
    root = Path.cwd()
    rid = run_id()
    out_dir = state_dir / "runs" / rid
    try:
        cash = runner.initial_nav(cfg, positive_cad(initial_cad) if initial_cad else None)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    if demo:
        start = _parse_date(from_date, runner.DEMO_START)
        end = _parse_date(to_date, runner.DEMO_END)
        schedule = commissions or root / "tests" / "fixtures" / "commissions_demo.yaml"
        primary, crosscheck = "csv", crosscheck or "xcheck"  # the fixture set's two sources
        cfg = runner.demo_config(cfg)  # the synthetic cash-sleeve fund (v1.7)
    else:
        if commissions is None:
            typer.echo(
                "A real run needs --commissions: the broker's current schedule (costs_v1.yaml "
                "says never assume zero)."
            )
            raise typer.Exit(2)
        start = _parse_date(from_date, date(2015, 1, 1))
        end = _parse_date(to_date, date(2025, 12, 31))
        schedule = commissions
    plan = runner.RunPlan(
        start, end, cash, _COSTS[costs], schedule, primary, crosscheck, fill_at,
        runner.DEMO_SLEEVE_HALF_SPREAD_BPS if demo else None,
    )  # fmt: skip
    with tempfile.TemporaryDirectory() as tmp:
        if demo:
            store = runner.ingest_demo_fixtures(cfg, root, Path(tmp) / "clean")
            forward, confirmations = None, runner.demo_confirmations(root)
            data_label = "SYNTHETIC fixture data (not market data)"
        else:
            store = VersionedStore(data_dir / "clean")
            forward = load_boc_holidays(holidays)
            confirmations = ConfirmationStore(state_dir / "confirmations.jsonl")
            data_label = f"stored data, source {primary}"
        results = runner.run_backtests(cfg, root, store, plan, out_dir, forward, confirmations)
        version = ("fixtures:" if demo else f"{primary}:") + runner.data_version(store)
    commission_label = schedule.name + (" (SYNTHETIC)" if demo and commissions is None else "")
    notices = (
        [
            "Synthetic fixture data, a synthetic commission schedule and a synthetic cash-sleeve "
            f"fund ({runner.DEMO_SLEEVE}): the numbers show how the system works, not how the "
            "strategy performs."
        ]
        if demo
        else []
    )
    report = runner.write_outputs(
        results,
        out_dir,
        "HIMARI backtest" + (" (demo)" if demo else ""),
        data_label,
        commission_label,
        notices,
    )
    if not quiet:
        for line in weekly_log(results[0]):
            typer.echo(line)
    hashes = run_hashes(root)
    note = notes or ("demo on synthetic fixture data" if demo else "")
    note += f" | costs {hashes['costs'][:12]} | commissions {schedule.name} | run {rid}"
    ids = runner.log_trials(
        TrialLedger(state_dir / "trials.sqlite"),
        results,
        root,
        hashes,
        version,
        label,
        note,
        _utc_now(),
    )
    typer.echo("")
    for r, tid in zip(results, ids, strict=True):
        s = summarize(r)
        name = f"{s.cost_multiplier:g}x costs" + (f", {s.label}" if s.label else "")
        typer.echo(
            f"{name}: NAV {s.final_nav:,.2f} CAD, return "
            f"{s.total_return:+.2%} (cash {s.cash_return:+.2%}), max drawdown "
            f"{s.max_drawdown:.2%}, costs {s.costs_cad:,.2f} CAD, trades {s.trades} [{tid}]"
        )
        if s.sleeve:  # v1.7: the cash sleeve's own line, apart from the strategy's figures
            typer.echo(
                f"  sleeve {s.sleeve}: average weight {s.sleeve_avg_weight:.1%}, turnover "
                f"{s.sleeve_turnover:.2f}/yr, costs {s.sleeve_costs_cad:,.2f} CAD, income "
                f"{s.sleeve_income_cad:,.2f} CAD, trades {s.sleeve_trades}"
            )
        for a in r.alerts:
            typer.echo(f"  ALERT {a}")
    typer.echo(f"Report: {report}")
    typer.echo(f"Run files: {out_dir}")


@register_app.command("run")
def register_run(
    hypothesis: str = typer.Argument(..., help="Hypothesis ID, e.g. H1"),
) -> None:
    """Run a single registered test."""
    typer.echo(f"[M8] running {hypothesis} (not yet implemented)")


@register_app.command("status")
def register_status(
    state_dir: Path = typer.Option(Path("state"), help="State root (trials.sqlite)"),
    register: Path = typer.Option(Path("research") / "register.yaml", help="Register file"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
) -> None:
    """Show the status of all registered tests, from the ledger's real-data trials (v1.8)."""
    _echo_register_status(state_dir, register, config)


def _echo_register_status(state_dir: Path, register: Path, config: Path) -> None:
    from himari.core.config import load_config
    from himari.research.register import load_register
    from himari.research.register import register_status as status_of
    from himari.research.trials import TrialLedger

    cfg = load_config(config)
    reg = load_register(register, cfg.research.holm_alpha)
    st = status_of(reg, TrialLedger(state_dir / "trials.sqlite"))
    for e in st.tests:
        if e.p_value is None:
            state = f"not at its endpoint ({e.trials} real-data trials)"
        else:
            holm = (
                ""
                if e.holm_reject is None
                else (", Holm: reject" if e.holm_reject else ", Holm: keep")
            )
            state = f"p = {e.p_value:.4f}{holm}"
        typer.echo(f"{e.test.id:<4} stage {e.test.stage}  {state}  | {e.test.title}")
    typer.echo(
        f"Endpoints reached: {st.endpoints_reached} of {len(st.tests)}. Holm "
        f"{'applied' if st.holm_applied else 'waits for every endpoint'} (alpha "
        f"{reg.family_alpha}); a decision before then needs p <= {cfg.research.early_decision_p}."
    )


@app.command("shadow")
def shadow(
    broker: str = typer.Option("quote_paper", help="Broker: quote_paper|ibkr_paper"),
    crypto: bool = typer.Option(False, help="Include crypto comparator"),
) -> None:
    """Start or resume the shadow run."""
    typer.echo(f"[M7] shadow run with {broker} (not yet implemented)")


@app.command("report")
def report(
    report_type: str = typer.Argument(..., help="baseline|register|weekly"),
    demo: bool = typer.Option(False, "--demo", help="The baseline report on the fixture data"),
    label: str = typer.Option("baseline", help="Trial ledger id for the baseline runs"),
    state_dir: Path = typer.Option(Path("state"), help="State root (runs/, trials.sqlite)"),
    config: Path = typer.Option(_default_config_path(), help="Config file"),
) -> None:
    """baseline: the M5 research report (metrics, benchmarks, statistics, robustness pack,
    research gate); register: the register's status; weekly: M7."""
    if report_type == "register":
        _echo_register_status(state_dir, Path("research") / "register.yaml", config)
        return
    if report_type == "weekly":
        typer.echo("[M7] weekly report (not yet implemented)")
        raise typer.Exit(2)
    if report_type != "baseline":
        raise typer.BadParameter("report type must be baseline, register or weekly")
    if not demo:
        typer.echo(
            "The real-data baseline report is part of M6: it needs the vendor data, the IBKR "
            "commission schedule and the synthetic pre-inception sleeve sensitivity. Use --demo "
            "for the fixture data."
        )
        raise typer.Exit(2)
    _demo_research_report(label, state_dir, config)


def _demo_research_report(label: str, state_dir: Path, config: Path) -> None:
    import tempfile

    from himari.core.config import load_config, run_hashes
    from himari.core.ids import run_id
    from himari.research.report import build_research_report, research_html
    from himari.research.trials import TrialLedger
    from himari.sim import run as runner
    from himari.sim.report import write_run

    cfg = runner.demo_config(load_config(config))  # the synthetic cash-sleeve fund
    root = Path.cwd()
    rid = run_id()
    out_dir = state_dir / "runs" / rid
    schedule = root / "tests" / "fixtures" / "commissions_demo.yaml"
    plan = runner.RunPlan(
        runner.DEMO_START, runner.DEMO_END, runner.initial_nav(cfg), (1.0, 2.0), schedule,
        "csv", "xcheck", "close", runner.DEMO_SLEEVE_HALF_SPREAD_BPS,
    )  # fmt: skip
    with tempfile.TemporaryDirectory() as tmp:
        store = runner.ingest_demo_fixtures(cfg, root, Path(tmp) / "clean")
        runs = runner.run_research_pack(
            cfg, root, store, plan, out_dir, None, runner.demo_confirmations(root)
        )
        version = "fixtures:" + runner.data_version(store)
    data_label = "SYNTHETIC fixture data (not market data)"
    notices = [
        "Synthetic fixture data, a synthetic commission schedule and a synthetic cash-sleeve "
        f"fund ({runner.DEMO_SLEEVE}): the numbers show how the research engine works, not how "
        "the strategy performs. One measured year cannot pass the research gate."
    ]
    hashes = run_hashes(root)
    note = (
        f"research pack on synthetic fixture data | costs {hashes['costs'][:12]} | "
        f"commissions {schedule.name} | run {rid}"
    )
    ledger = TrialLedger(state_dir / "trials.sqlite")
    others = [*runs.broker_cash, *(r for rs in runs.benchmarks.values() for r in rs), *runs.delayed]
    runner.log_trials(ledger, runs.baseline, root, hashes, version, label, note, _utc_now())
    runner.log_trials(ledger, others, root, hashes, version, "adhoc", note, _utc_now())
    runner.write_outputs(
        [*runs.baseline, *runs.broker_cash], out_dir, "HIMARI backtest (demo)", data_label,
        f"{schedule.name} (SYNTHETIC)", notices,
    )  # fmt: skip
    for r in others[len(runs.broker_cash) :]:
        write_run(r, out_dir / r.settings.run_name)
    rep = build_research_report(
        runs, cfg, ledger, "HIMARI research report (demo)", data_label, hashes["config"],
        version, notices,
    )  # fmt: skip
    path = out_dir / "research.html"
    path.write_text(research_html(rep), encoding="utf-8", newline="\n")
    typer.echo(f"Research gate at 2x: {rep.verdict}")
    for c in rep.research_gate:
        typer.echo(f"  {c.status:<4} {c.criterion}: {c.value}")
    for m in [*rep.baseline, *rep.broker_cash, *(x for v in rep.benchmarks.values() for x in v),
              *rep.delayed]:  # fmt: skip
        typer.echo(
            f"{m.name}: excess {m.metrics.annual_excess_return:+.2%}, Sharpe "
            f"{m.metrics.sharpe:.2f}, CER {m.metrics.cer:+.2%}, ES {m.metrics.es:.2%}, max "
            f"drawdown {m.metrics.max_drawdown:.2%}"
        )
    typer.echo(f"Research report: {path}")
    typer.echo(f"Run files: {out_dir}")


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
