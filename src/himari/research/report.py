"""The M5 research report (§12): the §12.2 metrics, the §12.3 benchmarks and robustness pack,
the §12.4 statistics and the §12.7 research gate for one research set (the baseline, its
cash-at-the-broker sensitivity, benchmarks 2 and 3 and the delay variant). One self-contained
HTML page in the backtest report's style.

Implementation choices, to confirm (M5):
- DSR's V is the variance of the per-period Sharpe ratios of the ledger's real-data trials when
  it holds at least 2 with returns; otherwise 1 / (T - 1), the variance of an unskilled trial's
  Sharpe estimate;
- the research gate's "common tradable history" is measured sessions / 252, and its "annual
  out-of-sample blocks" are calendar years with at least 240 measured sessions;
- "DSR >= 0.95" passes when it holds at every reported trial count and fails when it holds at
  none; in between it depends on N and is not judged;
- the baseline is compared with each benchmark by the one-sided paired bootstrap of the Sharpe
  difference (§12.4).
"""

from __future__ import annotations

import html
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import numpy as np

from himari.research.attribution import leave_one_instrument_out
from himari.research.metrics import (
    GateStats,
    ReturnMetrics,
    excess_returns,
    gate_statistics,
    leave_one_year_out,
    measured_series,
    return_metrics,
)
from himari.research.stats import (
    SharpeMoments,
    dsr,
    min_trl,
    paired_bootstrap,
    psr,
    sharpe_moments,
    sharpe_rows,
)
from himari.research.trials import LedgerPBO, TrialLedger, ledger_pbo
from himari.sim.report import Summary, page_css, summarize, table_html

if TYPE_CHECKING:
    from himari.core.config import FrozenConfig
    from himari.sim.engine import BacktestResult
    from himari.sim.run import ResearchRuns

MINTRL_ALPHA = 0.05  # one-sided 95%, as in Appendix F
FULL_YEAR_SESSIONS = 240  # a calendar year counts as an annual block with this many sessions
CAVEAT = (
    "A 90-day run checks operations; it cannot prove a slow strategy's edge. At a Sharpe ratio "
    "of 0.5, about 11 years of data are needed for 95% confidence (MinTRL 10.83 years)."
)


@dataclass(frozen=True)
class RunMetrics:
    name: str
    multiplier: float
    metrics: ReturnMetrics
    summary: Summary


@dataclass(frozen=True)
class Comparison:
    against: str
    multiplier: float
    other: RunMetrics | None  # None for CAD cash (r_ex = 0)
    sharpe_diff: float  # the baseline's Sharpe minus the other's
    p_value: float  # one-sided: the baseline improves on the other


@dataclass(frozen=True)
class Inference:
    multiplier: float
    moments: SharpeMoments
    psr0: float
    dsr: dict[int, float]  # trial count N -> DSR
    min_trl_years: float


@dataclass(frozen=True)
class GateCheck:
    criterion: str
    value: str
    status: str  # PASS | FAIL | n/a


@dataclass(frozen=True)
class ResearchReport:
    title: str
    data_label: str
    notices: list[str]
    config_hash: str
    data_version: str
    start: date
    end: date
    metric_start: date
    cash_return: float  # the cash index over the measured sessions
    baseline: list[RunMetrics]
    broker_cash: list[RunMetrics]
    benchmarks: dict[str, list[RunMetrics]]
    delayed: list[RunMetrics]
    comparisons: list[Comparison]
    inference: list[Inference]
    dsr_variance: float
    dsr_variance_source: str
    trial_count: int
    synthetic_rows: int
    legacy_trials: int | None
    pbo: LedgerPBO
    loo_years: dict[int, float]
    loo_funds: dict[str, float]
    gates: dict[str, GateStats]
    research_gate: list[GateCheck]
    verdict: str


def _series(res: BacktestResult) -> dict[date, float]:
    days, nav, cash = measured_series(res)
    return dict(zip(days[1:], excess_returns(nav, cash).tolist(), strict=True))


def _run_metrics(res: BacktestResult, name: str, cfg: FrozenConfig) -> RunMetrics:
    _, nav, cash = measured_series(res)
    m = return_metrics(
        nav, cash, cfg.risk.annualization, cfg.research.risk_aversion, cfg.research.es_level
    )
    return RunMetrics(f"{name} {res.settings.cost_multiplier:g}x", res.settings.cost_multiplier, m,
                      summarize(res))  # fmt: skip


def _paired(a: Mapping[date, float], b: Mapping[date, float]) -> tuple[list[float], list[float]]:
    common = sorted(set(a) & set(b))
    return [a[d] for d in common], [b[d] for d in common]


def _trial_variance(ledger: TrialLedger, t: int) -> tuple[float, str]:
    srs = []
    for row in ledger.real_data_rows():
        r = [x for _, x in ledger.returns(row["trial_id"])]
        if len(r) > 2 and float(np.std(r, ddof=1)) > 0:
            srs.append(sharpe_moments(r).sr)
    if len(srs) >= 2:
        return float(np.var(srs, ddof=1)), f"variance of {len(srs)} real-data trials' Sharpe ratios"
    return 1.0 / (t - 1), "fewer than 2 real-data trials: 1 / (T - 1), an unskilled trial's"


def build_research_report(
    runs: ResearchRuns,
    cfg: FrozenConfig,
    ledger: TrialLedger,
    title: str,
    data_label: str,
    config_hash: str,
    data_version: str,
    notices: Sequence[str] = (),
) -> ResearchReport:
    research = cfg.research
    boot = research.bootstrap
    base = [_run_metrics(r, "baseline", cfg) for r in runs.baseline]
    broker = [_run_metrics(r, "cash at the broker", cfg) for r in runs.broker_cash]
    benches = {
        label: [_run_metrics(r, label, cfg) for r in rs] for label, rs in runs.benchmarks.items()
    }
    delayed = [_run_metrics(r, "fills one session later", cfg) for r in runs.delayed]
    by_m = {r.settings.cost_multiplier: r for r in runs.baseline}
    comparisons: list[Comparison] = []
    inference: list[Inference] = []
    first = runs.baseline[0]
    t_sessions = len(_series(first))
    variance, variance_source = _trial_variance(ledger, t_sessions)
    for m, res in sorted(by_m.items()):
        b = _series(res)
        others: list[tuple[str, RunMetrics | None, dict[date, float]]] = [
            ("CAD cash", None, dict.fromkeys(b, 0.0))
        ]
        for label, rs in runs.benchmarks.items():
            for r, rm in zip(rs, benches[label], strict=True):
                if r.settings.cost_multiplier == m:
                    others.append((label, rm, _series(r)))
        for r, rm in zip(runs.broker_cash, broker, strict=True):
            if r.settings.cost_multiplier == m:
                others.append(("cash at the broker", rm, _series(r)))
        for against, other, s in others:
            x, y = _paired(s, b)
            test = paired_bootstrap(x, y, sharpe_rows, boot.mean_block, boot.resamples,
                                    research.seed)  # fmt: skip
            comparisons.append(Comparison(against, m, other, test.observed, test.p_value))
        mo = sharpe_moments(list(b.values()))
        inference.append(
            Inference(
                m,
                mo,
                psr(mo.sr, 0.0, mo.t, mo.skew, mo.kurt),
                {
                    n: dsr(mo.sr, mo.t, mo.skew, mo.kurt, n, variance)
                    for n in research.dsr_sensitivity_n
                },
                min_trl(mo.sr, 0.0, mo.skew, mo.kurt, MINTRL_ALPHA) / cfg.risk.annualization,
            )
        )
    gate_m = max(by_m)  # pass or fail at 2x (rule 7)
    judged = by_m[gate_m]
    days = sorted(_series(judged))
    r_ex = [_series(judged)[d] for d in days]
    loo_years = leave_one_year_out(days, r_ex, cfg.risk.annualization)
    loo_funds = leave_one_instrument_out(judged, cfg.risk.annualization)
    gates = gate_statistics(judged)
    pbo = ledger_pbo(ledger, gate_m)
    rows = ledger.rows()
    count = ledger.trial_count()
    judged_metrics = next(r for r in base if r.multiplier == gate_m)
    judged_inf = next(i for i in inference if i.multiplier == gate_m)
    checks = _research_gate(judged_metrics, judged_inf, days, loo_years, loo_funds)
    statuses = {c.status for c in checks}
    verdict = "FAIL" if "FAIL" in statuses else ("PASS" if statuses <= {"PASS"} else "UNDECIDED")
    _, _, cash = measured_series(first)
    return ResearchReport(
        title=title,
        data_label=data_label,
        notices=list(notices),
        config_hash=config_hash,
        data_version=data_version,
        start=first.settings.start,
        end=first.settings.end,
        metric_start=base[0].summary.metric_start,
        cash_return=cash[-1] / cash[0] - 1.0,
        baseline=base,
        broker_cash=broker,
        benchmarks=benches,
        delayed=delayed,
        comparisons=comparisons,
        inference=inference,
        dsr_variance=variance,
        dsr_variance_source=variance_source,
        trial_count=count,
        synthetic_rows=len(rows) - count,
        legacy_trials=research.legacy_trials_estimate,
        pbo=pbo,
        loo_years=loo_years,
        loo_funds=loo_funds,
        gates=gates,
        research_gate=checks,
        verdict=verdict,
    )


def _research_gate(
    base: RunMetrics,
    inf: Inference,
    days: Sequence[date],
    loo_years: Mapping[int, float],
    loo_funds: Mapping[str, float],
) -> list[GateCheck]:
    m = base.metrics
    years = m.sessions / 252.0
    blocks = sum(
        1
        for y in {d.year for d in days}
        if sum(1 for d in days if d.year == y) >= FULL_YEAR_SESSIONS
    )
    rows = {f"{y} left out": v for y, v in loo_years.items()}
    rows |= {f"{f} left out": v for f, v in loo_funds.items()}
    computed = {k: v for k, v in rows.items() if not math.isnan(v)}
    label = "No dependence on a single crisis or instrument (every leave-one-out row positive)"
    worst = min(computed, key=lambda k: computed[k]) if computed else ""
    if computed and computed[worst] <= 0:  # a negative row fails, whatever else is missing
        loo = GateCheck(label, f"{worst}: {computed[worst]:+.2%}", "FAIL")
    elif len(computed) < len(rows):
        missing = ", ".join(k for k in rows if k not in computed)
        loo = GateCheck(label, f"cannot be computed: {missing}", "n/a")
    else:
        loo = GateCheck(label, f"lowest row, {worst}: {computed[worst]:+.2%}", "PASS")
    dsr_values = list(inf.dsr.values())
    dsr_ok = [v >= 0.95 for v in dsr_values]
    dsr_status = "PASS" if all(dsr_ok) else ("FAIL" if not any(dsr_ok) else "n/a")
    return [
        GateCheck("Positive net excess return", f"{m.annual_excess_return:+.2%} a year",
                  "PASS" if m.annual_excess_return > 0 else "FAIL"),
        GateCheck("Positive CER", f"{m.cer:+.2%} a year", "PASS" if m.cer > 0 else "FAIL"),
        GateCheck("At least 5 years of common tradable history", f"{years:.1f} years",
                  "PASS" if years >= 5 else "FAIL"),
        GateCheck("At least 3 annual out-of-sample blocks", f"{blocks}",
                  "PASS" if blocks >= 3 else "FAIL"),
        loo,
        GateCheck("Holm pass (challengers only)", "the baseline is not a challenger", "n/a"),
        GateCheck(
            "DSR >= 0.95 (trial-count caveat)",
            ", ".join(f"N={n:,}: {v:.3f}" for n, v in inf.dsr.items()),
            dsr_status,
        ),
        GateCheck("MinTRL reported", f"{inf.min_trl_years:.1f} years", "PASS"),
    ]  # fmt: skip


# ---- HTML --------------------------------------------------------------------------------
def _pct(x: float | None, d: int = 2) -> str:
    return "-" if x is None or not math.isfinite(x) else f"{x * 100:.{d}f}%"


def _num(x: float | None, d: int = 2) -> str:
    return "-" if x is None or not math.isfinite(x) else f"{x:,.{d}f}"


def _metrics_rows(cols: Sequence[RunMetrics]) -> list[list[str]]:
    def row(label: str, f: Callable[[RunMetrics], str]) -> list[str]:
        return [label, *(f(c) for c in cols)]

    return [
        row("Annual return", lambda c: _pct(c.metrics.annual_return)),
        row("Annual excess return", lambda c: _pct(c.metrics.annual_excess_return)),
        row("Volatility (excess)", lambda c: _pct(c.metrics.annual_vol)),
        row("Sharpe ratio", lambda c: _num(c.metrics.sharpe)),
        row("CER (risk aversion 3)", lambda c: _pct(c.metrics.cer)),
        row("97.5% ES (daily loss)", lambda c: _pct(c.metrics.es)),
        row("Max drawdown", lambda c: _pct(c.metrics.max_drawdown)),
        row("Longest underwater (sessions)", lambda c: str(c.metrics.longest_underwater)),
        row("Turnover (one-way, per year)", lambda c: _pct(c.summary.turnover, 1)),
        row("Time in market", lambda c: _pct(c.summary.time_in_market, 1)),
        row("Average gross exposure", lambda c: _pct(c.summary.avg_gross_exposure, 1)),
        row("Strategy trades", lambda c: str(c.summary.trades)),
        row("Strategy costs (CAD)", lambda c: _num(c.summary.costs_cad)),
        row("Broker interest (CAD)", lambda c: _num(c.summary.interest_cad)),
        row("Cash sleeve fund", lambda c: c.summary.sleeve or "none"),
        row("Sleeve average weight", lambda c: _pct(c.summary.sleeve_avg_weight, 1)),
        row("Sleeve turnover (per year)", lambda c: _pct(c.summary.sleeve_turnover, 1)),
        row("Sleeve costs (CAD)", lambda c: _num(c.summary.sleeve_costs_cad)),
        row("Sleeve income (CAD)", lambda c: _num(c.summary.sleeve_income_cad)),
        row("Sleeve trades", lambda c: str(c.summary.sleeve_trades)),
    ]


def research_html(rep: ResearchReport) -> str:
    esc = html.escape
    sub = (
        f"{rep.start} to {rep.end} · {rep.data_label} · measured from {rep.metric_start} (the "
        f"warm-up is excluded) · cash index {_pct(rep.cash_return)} over the measured sessions · "
        f"config {rep.config_hash[:12]} · data {rep.data_version[:24]}"
    )
    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(rep.title)}</title><style>{page_css()}</style></head>",
        '<body><div class="viz-root">',
        f"<h1>{esc(rep.title)}</h1>",
        f'<p class="sub">{esc(sub)}</p>',
    ]
    for n in rep.notices:
        out.append(f'<div class="notice">{esc(n)}</div>')
    out.append(f'<div class="notice">{esc(CAVEAT)}</div>')

    def section(title: str, body: str, note: str = "") -> None:
        out.append(f"<h2>{esc(title)}</h2>")
        if note:
            out.append(f'<p class="sub">{esc(note)}</p>')
        out.append(body)

    gate_m = max(r.multiplier for r in rep.baseline)
    section(
        f"Research gate (§12.7) at {gate_m:g}x costs: {rep.verdict}",
        table_html(
            ["Criterion", "Value", "Status"],
            [[c.criterion, c.value, c.status] for c in rep.research_gate],
            left=3,
        ),
    )
    cols = [*rep.baseline, *rep.broker_cash]
    section(
        "Metrics (§12.2)",
        table_html(["", *(c.name for c in cols)], _metrics_rows(cols)),
        "On daily CAD excess returns over the cash index. Exposure, time in market and "
        "strategy turnover leave out the cash sleeve, which has its own lines.",
    )
    bench_rows = []
    for c in rep.comparisons:
        m = c.other.metrics if c.other else None
        bench_rows.append(
            [
                c.against,
                f"{c.multiplier:g}x",
                _pct(m.annual_excess_return) if m else "0.00%",
                _num(m.sharpe) if m else "-",
                _pct(m.cer) if m else "0.00%",
                _pct(m.max_drawdown) if m else "-",
                _num(c.sharpe_diff),
                f"{c.p_value:.4f}",
            ]
        )
    section(
        "Benchmarks (§12.3)",
        table_html(
            [
                "Benchmark",
                "Costs",
                "Annual excess",
                "Sharpe",
                "CER",
                "Max drawdown",
                "Baseline Sharpe minus",
                "One-sided p (baseline better)",
            ],
            bench_rows,
            left=2,
        ),  # fmt: skip
        "Benchmark 1 is CAD cash; benchmark 2 has every signal on with everything else "
        "unchanged; benchmark 3 is equal weights times the signal with no volatility scaling. "
        "All run through the same ledger and cash handling. p from the paired stationary "
        "bootstrap of the Sharpe difference.",
    )
    inf_rows = []
    for i in rep.inference:
        mo = i.moments
        inf_rows.append(
            [f"{i.multiplier:g}x", str(mo.t), _num(mo.sr, 4), _num(mo.sr * math.sqrt(252)),
             _num(mo.skew), _num(mo.kurt), f"{i.psr0:.3f}",
             *(f"{v:.3f}" for v in i.dsr.values()), _num(i.min_trl_years, 1)]
        )  # fmt: skip
    ns = list(rep.inference[0].dsr) if rep.inference else []
    legacy = "unknown" if rep.legacy_trials is None else f"{rep.legacy_trials:,}"
    pbo_text = (
        f"PBO over {len(rep.pbo.trials)} real-data trials: {rep.pbo.result.pbo:.3f}"
        if rep.pbo.result
        else f"PBO over the trial ledger: not computed ({rep.pbo.note})"
    )
    section(
        "Statistics (§12.4)",
        table_html(
            [
                "Costs",
                "T",
                "SR per session",
                "SR annual",
                "Skew",
                "Kurtosis",
                "PSR(0)",
                *(f"DSR N={n:,}" for n in ns),
                "MinTRL (years)",
            ],
            inf_rows,
        ),  # fmt: skip
        f"Trial count from the ledger: {rep.trial_count} real-data rows ({rep.synthetic_rows} "
        f"fixture or demo rows excluded, v1.8); the old alpha factory's count is {legacy}. "
        f"V for DSR: {rep.dsr_variance:.3g} ({rep.dsr_variance_source}). {pbo_text}.",
    )
    loo_rows = [[str(y), _pct(v)] for y, v in sorted(rep.loo_years.items())]
    fund_rows = [[f, _pct(v)] for f, v in rep.loo_funds.items()]
    judged = next(r for r in rep.baseline if r.multiplier == gate_m)
    delay_rows = []
    for d in rep.delayed:
        b = next(r for r in rep.baseline if r.multiplier == d.multiplier)
        delay_rows.append(
            [d.name, _pct(d.metrics.annual_excess_return),
             _pct(d.metrics.annual_excess_return - b.metrics.annual_excess_return),
             _num(d.metrics.sharpe), _pct(d.metrics.cer), _pct(d.metrics.max_drawdown),
             str(d.summary.trades)]
        )  # fmt: skip
    section(
        "Robustness pack (§12.3)",
        "<h3>Leave one calendar year out</h3>"
        + table_html(["Year left out", f"Annual excess return, {gate_m:g}x"], loo_rows)
        + "<h3>Leave one instrument's P&amp;L out</h3>"
        + table_html(
            ["Fund left out", f"Annual excess return, {gate_m:g}x"],
            [["(none)", _pct(judged.metrics.annual_excess_return)], *fund_rows],
        )
        + "<h3>Every fill one session later</h3>"
        + table_html(
            ["Run", "Annual excess", "vs baseline", "Sharpe", "CER", "Max drawdown", "Trades"],
            delay_rows,
        ),
        "Reported with the baseline, labelled, and never used to choose anything; each run is "
        "logged as adhoc. Every leave-one-out row must stay positive for the research gate.",
    )
    gate_rows = [
        [g, str(s.decisions), str(s.binds), _num(s.avg_scale, 3)] for g, s in rep.gates.items()
    ]
    section(
        f"Gate statistics (baseline, {gate_m:g}x)",
        table_html(["Gate", "Decisions", "Bound", "Average scale when bound"], gate_rows),
        "Average scale = gross weight leaving the gate over gross weight entering it, at the "
        "decisions where it bound.",
    )
    out.append(f'<p class="foot">{esc(CAVEAT)}</p></div></body></html>')
    return "".join(out)
