"""Backtest outputs: the run log, the weekly log and a one-page HTML report.

The run log (JSON lines) is the determinism golden: it holds no wall-clock time or run id.
The HTML report has no external dependencies: inline SVG charts, a small script for the hover
layer (assets/report.js), light and dark themes (assets/report.css) and a table view for every
chart.
"""

from __future__ import annotations

import bisect
import csv
import html
import io
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from himari.sim.engine import BacktestResult

# Categorical slots in universe order, so a fund keeps its colour in every chart. Validated
# light and dark with the dataviz palette validator; three light slots are under 3:1 against
# the surface, so the weights chart ships with a table view.
SERIES_LIGHT = [
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"
]  # fmt: skip
SERIES_DARK = [
    "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"
]  # fmt: skip
EDGE_CAVEAT = (
    "A short run checks operations; it cannot prove a slow strategy's edge. At a Sharpe ratio "
    "of 0.5, about 11 years of data are needed for 95% confidence."
)
_ASSETS = Path(__file__).parent / "assets"


# ---- summary ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Summary:
    """Metrics over the measured period (§12.2, v1.6): from the later of the window start and
    the first weekly decision with at least one valid signal; the warm-up is excluded."""

    start: date  # the run window
    end: date
    metric_start: date
    warmup_sessions: int  # sessions before metric_start
    cost_multiplier: float
    initial_nav: float
    base_nav: float  # NAV at metric_start
    final_nav: float
    total_return: float
    annual_return: float
    annual_vol: float
    max_drawdown: float
    cash_return: float
    excess_return: float  # compounded, over the CORRA cash index
    commissions_cad: float
    spread_impact_cad: float
    fx_conversion_cad: float
    interest_cad: float  # posted and accrued in the measured period
    turnover: float  # one-way traded value / average NAV, per year
    time_in_market: float
    avg_gross_exposure: float
    trades: int
    unfilled: int
    gate_binds: dict[str, tuple[int, int]]  # gate -> (decisions where it bound, decisions)
    receivables_without_pay_date_cad: float
    alerts: int

    @property
    def costs_cad(self) -> float:
        return self.commissions_cad + self.spread_impact_cad + self.fx_conversion_cad


def metric_start(res: BacktestResult) -> date:
    """The later of the window start and the first weekly decision where at least one fund has
    a valid signal; the run's last day when there is none."""
    for r in res.records:
        if r["kind"] == "decision" and any(f["momentum"] is not None for f in r["funds"].values()):
            return max(res.settings.start, _d(r["date"]))
    return res.nav[-1].day


def _fill_costs(res: BacktestResult, since: date) -> tuple[float, float, float]:
    """Commissions, spread and impact, and traded value from `since`, in CAD."""
    rate = {p.day: p.usd_to_cad for p in res.nav}
    commissions = spread = traded = 0.0
    for day, det in res.fills:
        if day < since:
            continue
        fx = 1.0 if det.currency == "CAD" else rate[day]
        f = det.fill
        commissions += float(f.commission) * fx
        spread += abs(float(f.price) - det.market_price) * f.shares * fx
        traded += float(f.price) * f.shares * fx
    return commissions, spread, traded


def summarize(res: BacktestResult) -> Summary:
    m0 = metric_start(res)
    days = [p.day for p in res.nav]
    i0 = bisect.bisect_left(days, m0)
    points = res.nav[i0:]
    nav = [p.nav for p in points]
    base, final = nav[0], nav[-1]
    years = max((points[-1].day - points[0].day).days / 365.25, 1e-9)
    rets = [b / a - 1.0 for a, b in zip(nav[:-1], nav[1:], strict=True)]
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    peak, mdd = -math.inf, 0.0
    for v in nav:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    levels = [x for x in res.cash_level[i0:] if math.isfinite(x)]
    cash_ret = levels[-1] / levels[0] - 1.0 if len(levels) > 1 else 0.0
    commissions, spread, traded = _fill_costs(res, m0)
    rate = {p.day: p.usd_to_cad for p in res.nav}
    interest = sum(
        float(r.amount) * (1.0 if r.currency == "CAD" else rate.get(r.day, 1.0))
        for r in res.ledger.interest
        if r.day > m0
    ) + (points[-1].accrued_interest_cad - points[0].accrued_interest_cad)
    binds: dict[str, list[int]] = {}
    for r in res.records:
        if r["kind"] != "decision" or _d(r["date"]) < m0:
            continue
        for g in r["gates"]:
            b = binds.setdefault(g["gate"], [0, 0])
            b[0] += int(bool(g["bound"]))
            b[1] += 1
    last_rate = res.nav[-1].usd_to_cad
    recv = sum(
        float(r.net) * (1.0 if r.currency == "CAD" else last_rate)
        for r in res.ledger.receivables()
        if r.pay_date is None
    )
    return Summary(
        start=days[0],
        end=days[-1],
        metric_start=points[0].day,
        warmup_sessions=i0,
        cost_multiplier=res.settings.cost_multiplier,
        initial_nav=float(res.settings.initial_cad),
        base_nav=base,
        final_nav=final,
        total_return=final / base - 1.0,
        annual_return=(final / base) ** (1.0 / years) - 1.0,
        annual_vol=math.sqrt(var * 252.0),
        max_drawdown=mdd,
        cash_return=cash_ret,
        excess_return=(final / base) / (1.0 + cash_ret) - 1.0,
        commissions_cad=commissions,
        spread_impact_cad=spread,
        fx_conversion_cad=sum(float(c.cost_cad) for c in res.ledger.conversions if c.day >= m0),
        interest_cad=interest,
        turnover=traded / 2.0 / (sum(nav) / len(nav)) / years,
        time_in_market=sum(1 for p in points if p.weights) / len(points),
        avg_gross_exposure=sum(sum(p.weights.values()) for p in points) / len(points),
        trades=sum(1 for t in res.ledger.trades if t.day >= m0),
        unfilled=len(res.unfilled),
        gate_binds={g: (b[0], b[1]) for g, b in binds.items()},
        receivables_without_pay_date_cad=recv,
        alerts=len(res.alerts),
    )


# ---- text --------------------------------------------------------------------------------
def _pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def _cad(x: float) -> str:
    return f"{x:,.2f}"


def _gate_reason(g: Mapping[str, Any]) -> str:
    details = g.get("details") or {}
    name = str(g["gate"])
    if name == "vol_ceiling":
        vol, cap = details.get("portfolio_vol", 0.0), details.get("ceiling", 0.0)
        return f"vol_ceiling x{g['scale']:.3f} (portfolio vol {_pct(vol, 2)} > {_pct(cap, 2)})"
    if name == "stress":
        single = _pct(details.get("worst_single", 0.0), 2)
        joint = _pct(details.get("joint", 0.0), 2)
        worst = details.get("worst_single_scenario", "")
        return f"stress x{g['scale']:.3f} (worst single {single} {worst}, joint {joint})"
    if isinstance(details, Mapping) and details:
        parts = [f"{k}: {v}" for k, v in sorted(details.items()) if isinstance(v, str)]
        return f"{name} ({'; '.join(parts)})" if parts else name
    return name


def weekly_log(res: BacktestResult) -> list[str]:
    """One block per weekly decision: signals, targets, gates that bound and why, the band,
    orders, the fills and costs that followed, and NAV. Safety cuts and alerts in between."""
    rate = {p.day: p.usd_to_cad for p in res.nav}
    fills_by_decision: dict[str, list[tuple[date, Any]]] = defaultdict(list)
    for day, det in res.fills:
        fills_by_decision[det.decision_id].append((day, det))
    lines: list[str] = []
    for r in res.records:
        kind = r["kind"]
        if kind == "alert":
            lines.append(f"!! {r['date']}  ALERT  {r['message']}")
            continue
        if kind == "safety":
            cuts = ", ".join(f"{s} -> {_pct(w)}" for s, w in sorted(r["targets"].items()))
            lines.append(f"-- {r['date']}  daily safety: holdings vol {_pct(r['vol'], 2)}; {cuts}")
            if r.get("blocked"):
                lines.append(f"   blocked: {r['blocked']}")
            continue
        if kind != "decision":
            continue
        d = r["drawdown"]
        lines.append(
            f"{r['date']}  {r['decision_id']}  NAV {_cad(r['nav'])} CAD  "
            f"drawdown {_pct(d['drawdown'], 2)} {d['state']}"
        )
        funds = r["funds"]
        on = [f"{s} {f['momentum'] * 100:+.1f}%" for s, f in funds.items() if f["on"]]
        off = [
            f"{s} {f['momentum'] * 100:+.1f}%" if f["momentum"] is not None else f"{s} n/a"
            for s, f in funds.items()
            if not f["on"]
        ]
        lines.append(f"   signals  on: {', '.join(on) or '-'} | off: {', '.join(off) or '-'}")
        tgt = sorted(
            ((s, f["after_gates"]) for s, f in funds.items() if f["after_gates"] > 0),
            key=lambda x: -x[1],
        )
        shown = "  ".join(f"{s} {_pct(w)}" for s, w in tgt) or "all cash"
        bound = ", ".join(r["sizing"]["bound"]) or "none"
        total, k = sum(w for _, w in tgt), r["sizing"]["scale"]
        lines.append(f"   targets  {shown}  (sum {_pct(total)}, k {k:.3f}; sizing bound: {bound})")
        gates = [_gate_reason(g) for g in r["gates"] if g["bound"]]
        lines.append(f"   gates    {'; '.join(gates) if gates else 'none bound'}")
        if r.get("blocked"):
            lines.append(f"   BLOCKED  {r['blocked']}")
            continue
        trade = [
            f"{s} ({f['band']})"
            for s, f in funds.items()
            if f.get("band") not in (None, "inside_band", "no_change")
        ]
        hold = [s for s, f in funds.items() if f.get("band") == "inside_band"]
        lines.append(
            f"   band     trade: {', '.join(trade) or '-'} | inside: {', '.join(hold) or '-'}"
        )
        pt = r.get("post_trade")
        if pt and (pt["buy_scale"] < 1.0 or pt["fallback"]):
            what = (
                "every fund traded to its gated target"
                if pt["fallback"]
                else f"buys scaled x{pt['buy_scale']:.3f}"
            )
            lines.append(
                f"   post     {pt['banded'].describe()} over the limit + "
                f"{_pct(0.1, 0)}: {what} -> {pt['final'].describe()}"
            )
        orders = [
            f"{o['order'].side} {o['order'].shares} {o['order'].symbol} [{o['order'].reason}"
            + (f"; {o['order'].gates_text()}" if o["order"].gates else "")
            + "]"
            for o in r["orders"]
        ]
        skipped = [f"{s.symbol} ({s.reason})" for s in r.get("skipped", [])]
        tail = f" | skipped: {', '.join(skipped)}" if skipped else ""
        lines.append(f"   orders   {', '.join(orders) or 'none'}{tail}")
        comm = spread = 0.0
        fills = fills_by_decision.get(r["decision_id"], [])
        for day, det in fills:
            fx = 1.0 if det.currency == "CAD" else rate[day]
            f = det.fill
            comm += float(f.commission) * fx
            spread += abs(float(f.price) - det.market_price) * f.shares * fx
            flags = "".join(
                flag
                for flag, on_ in (
                    (" stressed", det.stressed),
                    (" capped", det.capped),
                    (" cash-limited", det.cash_limited),
                )
                if on_
            )
            lines.append(
                f"   fill     {day} {f.side} {f.shares} {f.symbol} @ {f.price} {det.currency} "
                f"({det.side_bps:.1f} bp, commission {f.commission}){flags}"
            )
        if fills:
            lines.append(
                f"   costs    commissions {_cad(comm)} CAD, spread+impact {_cad(spread)} CAD"
            )
        if r.get("fx"):
            usd = _cad(r["fx"]["usd"])
            lines.append(f"   fx       {r['fx']['purpose']}: {usd} USD to CAD next session")
    return lines


# ---- files -------------------------------------------------------------------------------
def nav_csv(res: BacktestResult) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    symbols = list(res.universe)
    head = ["date", "nav_cad", "cash_cad", "cash_usd", "receivables_cad", "usd_to_cad"]
    w.writerow([*head, "drawdown", "state", "cash_index", *symbols])
    for p, level in zip(res.nav, res.cash_level, strict=True):
        w.writerow(
            [
                p.day.isoformat(),
                f"{p.nav:.6f}",
                f"{p.cash.get('CAD', 0.0):.2f}",
                f"{p.cash.get('USD', 0.0):.2f}",
                f"{p.receivables_cad:.2f}",
                f"{p.usd_to_cad:.6f}",
                f"{p.drawdown:.10f}",
                p.state,
                f"{level:.10f}" if math.isfinite(level) else "",
                *(f"{p.weights.get(s, 0.0):.10f}" for s in symbols),
            ]
        )
    return out.getvalue()


def trades_csv(res: BacktestResult) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["date", "decision_id", "order_id", "symbol", "side", "shares", "price"]
               + ["currency", "gross", "commission", "reason", "gates"])  # fmt: skip
    for t in res.ledger.trades:
        w.writerow([t.day.isoformat(), t.decision_id, t.order_id, t.symbol, t.side, t.shares]
                   + [t.price, t.currency, t.gross, t.commission, t.reason, t.gates])  # fmt: skip
    return out.getvalue()


def write_run(res: BacktestResult, out_dir: Path) -> dict[str, Path]:
    """Run log, NAV, trades and weekly log; text files with LF line endings."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "decisions": ("decisions.jsonl", "".join(line + "\n" for line in res.log_lines())),
        "nav": ("nav.csv", nav_csv(res)),
        "trades": ("trades.csv", trades_csv(res)),
        "weekly_log": ("weekly_log.txt", "".join(line + "\n" for line in weekly_log(res))),
    }
    paths: dict[str, Path] = {}
    for key, (name, text) in files.items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        paths[key] = path
    return paths


# ---- HTML --------------------------------------------------------------------------------
def _tag(name: str, attrs: Mapping[str, object], body: str | None = None) -> str:
    """An element; attribute values are escaped, the body is inserted as given."""
    a = "".join(f' {k}="{html.escape(str(v), quote=True)}"' for k, v in attrs.items())
    return f"<{name}{a}/>" if body is None else f"<{name}{a}>{body}</{name}>"


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return [lo]
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step + 1e-9) * step
    last = math.ceil(hi / step - 1e-9) * step  # the axis always covers [lo, hi]
    return [round(first + i * step, 10) for i in range(round((last - first) / step) + 1)]


def _fmt_axis(v: float, step: float) -> str:
    """Full numbers under 10,000; above, thousands with as many decimals as the step needs
    (a 2,500 step gives 102.5K, never a rounded 102K)."""
    if abs(v) < 10_000:
        return f"{v:,.0f}"
    k = step / 1000
    decimals = next((d for d in range(4) if abs(round(k, d) - k) < 1e-9), 3)
    return f"{v / 1000:,.{decimals}f}K"


def _month_ticks(days: Sequence[date]) -> list[int]:
    """Index of the first day of each quarter (each month for runs under ~13 months)."""
    months = 1 if (days[-1] - days[0]).days < 400 else 3
    out: list[int] = []
    seen: set[tuple[int, int]] = set()
    for i, d in enumerate(days):
        key = (d.year, (d.month - 1) // months)
        if key not in seen and (d.month - 1) % months == 0:
            seen.add(key)
            out.append(i)
    return out


W, H, L, R, T, B = 720, 260, 56, 96, 12, 28  # chart box: width, height and margins
PLOT_W, PLOT_H = W - L - R, H - T - B


def _axes(y_ticks: Sequence[tuple[float, str]], x_ticks: Sequence[tuple[float, str]]) -> str:
    parts = []
    for y, text in y_ticks:
        parts.append(_tag("line", {"class": "grid", "x1": L, "x2": W - R, "y1": y, "y2": y}))
        attrs = {"class": "tick", "x": L - 6, "y": f"{y + 4:.1f}", "text-anchor": "end"}
        parts.append(_tag("text", attrs, html.escape(text)))
    for x, text in x_ticks:
        attrs = {"class": "tick", "x": x, "y": H - 8, "text-anchor": "middle"}
        parts.append(_tag("text", attrs, html.escape(text)))
    return "".join(parts)


def _warmup(x0: float, x1: float) -> str:
    """The shaded warm-up band (v1.6): before the first decision with a valid signal."""
    if x1 <= x0:
        return ""
    band = {"class": "warmup", "x": f"{x0:.1f}", "y": T, "width": f"{x1 - x0:.1f}"}
    label = {"class": "wlabel", "x": f"{x0 + 6:.1f}", "y": T + 14}
    return _tag("rect", {**band, "height": PLOT_H}) + _tag("text", label, "warm-up (excluded)")


def _line_chart(
    days: Sequence[date],
    series: Sequence[tuple[str, str, Sequence[float]]],
    warmup_end: int = 0,
) -> str:
    """One CAD axis; each series (name, css var, values) as a 2px line with an end label."""
    vals = [v for _, _, s in series for v in s if math.isfinite(v)]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 1.0
    ticks = _nice_ticks(lo - pad, hi + pad)
    y0, y1 = ticks[0], ticks[-1]
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1.0
    n = len(days)

    def x(i: int) -> str:
        return f"{L + PLOT_W * (i / max(n - 1, 1)):.1f}"

    def y(v: float) -> float:
        return T + PLOT_H * (1 - (v - y0) / (y1 - y0)) if y1 > y0 else T + PLOT_H / 2

    parts = [
        _warmup(float(x(0)), float(x(warmup_end))) if warmup_end > 0 else "",
        _axes(
            [(round(y(t), 1), _fmt_axis(t, step)) for t in ticks],
            [(float(x(i)), f"{days[i]:%b %Y}") for i in _month_ticks(days)],
        ),
    ]
    ends: list[tuple[float, str, str]] = []
    for name, var, s in series:
        pts = " ".join(f"{x(i)},{y(v):.1f}" for i, v in enumerate(s) if math.isfinite(v))
        parts.append(
            _tag("polyline", {"class": "line", "style": f"stroke:var({var})", "points": pts})
        )
        last = next((v for v in reversed(s) if math.isfinite(v)), None)
        if last is not None:
            ends.append((y(last), name, var))
    xe = float(x(n - 1))
    placed: list[float] = []
    for ye, name, var in sorted(ends):
        ly = max(ye, placed[-1] + 14) if placed else ye
        placed.append(ly)
        dot = {"class": "end", "cx": xe, "cy": f"{ye:.1f}", "r": 4, "style": f"fill:var({var})"}
        parts.append(_tag("circle", dot))
        if abs(ly - ye) > 0.5:  # a leader line instead of a label detached from its line
            leader = {"class": "leader", "x1": xe + 5, "y1": f"{ye:.1f}", "x2": xe + 10}
            parts.append(_tag("line", {**leader, "y2": f"{ly:.1f}"}))
        label = {"class": "label", "x": xe + 12, "y": f"{ly + 4:.1f}"}
        parts.append(_tag("text", label, html.escape(name)))
    cross = {"class": "crosshair", "id": "nav-cross", "x1": 0, "x2": 0, "y1": T, "y2": H - B}
    parts.append(_tag("line", {**cross, "visibility": "hidden"}))
    parts.append(_tag("rect", {"class": "hit", "x": L, "y": T, "width": PLOT_W, "height": PLOT_H}))
    svg = {
        "viewBox": f"0 0 {W} {H}",
        "class": "chart",
        "id": "nav-chart",
        "tabindex": 0,
        "role": "img",
        "aria-label": "NAV and the cash benchmark in CAD; arrow keys move the crosshair",
    }
    return _tag("svg", svg, "".join(parts))


def _weights_chart(
    days: Sequence[date],
    symbols: Sequence[str],
    weights: Sequence[Mapping[str, float]],
    warmup_end: int = 0,
) -> str:
    """Stacked columns: each fund's weight at each weekly decision; cash is the rest."""
    n = len(days)
    band = PLOT_W / max(n, 1)
    col = min(24.0, max(band - 2.0, 1.0))

    def y(v: float) -> float:
        return T + PLOT_H * (1 - v)

    parts = [
        _warmup(L, L + band * warmup_end) if warmup_end > 0 else "",
        _axes(
            [(round(y(t), 1), f"{t * 100:.0f}%") for t in (0.0, 0.25, 0.5, 0.75, 1.0)],
            [(round(L + band * (i + 0.5), 1), f"{days[i]:%b %Y}") for i in _month_ticks(days)],
        ),
    ]
    for i, w in enumerate(weights):
        x0 = L + band * i + (band - col) / 2
        acc = 0.0
        for k, s in enumerate(symbols):
            v = w.get(s, 0.0)
            if v <= 0:
                continue
            top, bottom = y(acc + v), y(acc)
            seg = {
                "x": f"{x0:.1f}",
                "y": f"{top + 2.0:.1f}",  # a 2px surface gap between segments
                "width": f"{col:.1f}",
                "height": f"{max(bottom - top - 2.0, 0.5):.1f}",
                "rx": 1,
                "style": f"fill:var(--s{k + 1})",
            }
            parts.append(_tag("rect", seg))
            acc += v
        hit = {"class": "hit col", "data-i": i, "x": f"{L + band * i:.1f}", "y": T}
        parts.append(_tag("rect", {**hit, "width": f"{band:.1f}", "height": PLOT_H}))
    svg = {
        "viewBox": f"0 0 {W} {H}",
        "class": "chart",
        "id": "w-chart",
        "role": "img",
        "aria-label": "Fund weights at each weekly decision; the table view lists them",
    }
    return _tag("svg", svg, "".join(parts))


def _asset(name: str) -> str:
    return (_ASSETS / name).read_text(encoding="utf-8")


def _table(head: Sequence[str], rows: Sequence[Sequence[str]], left: int = 1) -> str:
    """A table; the first `left` columns are left-aligned text, the rest numbers."""

    def cells(tag: str, values: Sequence[str]) -> str:
        return "".join(
            _tag(tag, {"class": "l"} if i < left else {}, html.escape(v))
            for i, v in enumerate(values)
        )

    body = "".join(f"<tr>{cells('td', r)}</tr>" for r in rows)
    return f'<div class="scroll"><table><tr>{cells("th", head)}</tr>{body}</table></div>'


def html_report(
    results: Sequence[BacktestResult],
    title: str,
    data_label: str,
    commission_label: str,
    notices: Sequence[str] = (),
) -> str:
    """One page: KPIs per cost multiplier, NAV vs cash, weights at each weekly decision,
    gate statistics and the trade list (of the first result)."""
    esc = html.escape
    main = results[0]
    sums = [summarize(r) for r in results]
    days = [p.day for p in main.nav]
    initial = float(main.settings.initial_cad)
    base = next((v for v in main.cash_level if math.isfinite(v)), math.nan)
    cash_nav = [initial * v / base if math.isfinite(v) else math.nan for v in main.cash_level]
    colors = ["--nav1", "--nav2"]
    series: list[tuple[str, str, Sequence[float]]] = [
        (f"NAV {s.cost_multiplier:g}x costs", colors[i % 2], [p.nav for p in r.nav])
        for i, (r, s) in enumerate(zip(results, sums, strict=True))
    ]
    series.append(("CAD cash", "--cash", cash_nav))
    symbols = list(main.universe)
    by_day = {p.day: p for p in main.nav}
    wdays = [_d(r["date"]) for r in main.records if r["kind"] == "decision"]
    wvals = [by_day[d].weights for d in wdays]

    m0 = sums[0].metric_start
    warm_nav, warm_w = bisect.bisect_left(days, m0), bisect.bisect_left(wdays, m0)

    css = _asset("report.css")
    css = css.replace("%LIGHT%", "".join(f"--s{i + 1}:{c};" for i, c in enumerate(SERIES_LIGHT)))
    css = css.replace("%DARK%", "".join(f"--s{i + 1}:{c};" for i, c in enumerate(SERIES_DARK)))
    sub = (
        f"{sums[0].start} to {sums[0].end} · {data_label} · commissions: {commission_label} · "
        f"initial NAV {_cad(initial)} CAD · metrics from {m0} (the warm-up before the first "
        "valid signal is shaded and excluded)"
    )
    out: list[str] = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(title)}</title><style>{css}</style></head>",
        '<body><div class="viz-root">',
        f"<h1>{esc(title)}</h1>",
        f'<p class="sub">{esc(sub)}</p>',
    ]
    for note in notices:
        out.append(f'<div class="notice">{esc(note)}</div>')
    if main.missing_events:
        miss = "; ".join(
            f"{t} ({', '.join(map(str, ys))})" for t, ys in sorted(main.missing_events.items())
        )
        text = f"Event types missing from the archive for this run: {miss}"
        out.append(f'<div class="notice">{esc(text)}</div>')
    for r, s in zip(results, sums, strict=True):
        for a in r.alerts:
            out.append(f'<div class="notice crit">{s.cost_multiplier:g}x: {esc(a)}</div>')
    if sums[0].receivables_without_pay_date_cad:
        recv = _cad(sums[0].receivables_without_pay_date_cad)
        text = (
            "Distributions without a pay date in the data stay receivable (counted in NAV, "
            f"not spendable): {recv} CAD at the end."
        )
        out.append(f'<div class="notice">{esc(text)}</div>')

    def kpi(label: str, values: list[str], note: str) -> str:
        parts = (("k", label), ("v", " / ".join(values)), ("d", note))
        return _tag(
            "div", {"class": "kpi"}, "".join(_tag("div", {"class": c}, esc(t)) for c, t in parts)
        )

    mults = " / ".join(f"{s.cost_multiplier:g}x" for s in sums)
    cash_note = f"cash {_pct(sums[0].cash_return, 2)}"
    tiles = [
        kpi("Final NAV (CAD)", [_cad(s.final_nav) for s in sums], mults),
        kpi("Total return", [_pct(s.total_return, 2) for s in sums], cash_note),
        kpi("Excess over cash", [_pct(s.excess_return, 2) for s in sums], mults),
        kpi("Max drawdown", [_pct(s.max_drawdown, 2) for s in sums], mults),
        kpi("Costs (CAD)", [_cad(s.costs_cad) for s in sums], "commissions, spread, impact, FX"),
        kpi("Volatility", [_pct(s.annual_vol, 2) for s in sums], "annualized, daily NAV"),
        kpi("Avg gross exposure", [_pct(s.avg_gross_exposure, 1) for s in sums], mults),
        kpi("Interest (CAD)", [_cad(s.interest_cad) for s in sums], "on settled cash"),
    ]
    out.append(_tag("div", {"class": "kpis"}, "".join(tiles)))

    out.append("<h2>NAV against CAD cash</h2>")
    keys = "".join(
        f'<span><i class="ln" style="background:var({v})"></i>{esc(n)}</span>' for n, v, _ in series
    )
    legend = _tag("div", {"class": "legend"}, keys)
    out.append(_tag("div", {"class": "card"}, legend + _line_chart(days, series, warm_nav)))

    out.append("<h2>Weights at each weekly decision</h2>")
    keys = "".join(
        f'<span><i style="background:var(--s{k + 1})"></i>{esc(s)}</span>'
        for k, s in enumerate(symbols)
    )
    legend = _tag("div", {"class": "legend"}, keys + "<span>remainder: cash</span>")
    chart = _weights_chart(wdays, symbols, wvals, warm_w)
    out.append(_tag("div", {"class": "card"}, legend + chart))
    rows = [
        [
            str(d),
            *(f"{w.get(s, 0.0) * 100:.1f}%" for s in symbols),
            f"{(1 - sum(w.values())) * 100:.1f}%",
        ]
        for d, w in zip(wdays, wvals, strict=True)
    ]
    table = _table(["date", *symbols, "cash"], rows)
    out.append(f"<details><summary>Table view: weights</summary>{table}</details>")

    out.append("<h2>Summary</h2>")
    srows = [
        ["Measured from (warm-up excluded)", *(str(s.metric_start) for s in sums)],
        ["Warm-up sessions", *(str(s.warmup_sessions) for s in sums)],
        ["NAV at the start of measurement (CAD)", *(_cad(s.base_nav) for s in sums)],
        ["Annual return", *(_pct(s.annual_return, 2) for s in sums)],
        ["Cash index return (CORRA)", *(_pct(s.cash_return, 2) for s in sums)],
        ["Turnover (one-way, per year)", *(_pct(s.turnover, 1) for s in sums)],
        ["Time in market", *(_pct(s.time_in_market, 1) for s in sums)],
        ["Average gross exposure", *(_pct(s.avg_gross_exposure, 1) for s in sums)],
        ["Interest earned (CAD)", *(_cad(s.interest_cad) for s in sums)],
        ["Commissions (CAD)", *(_cad(s.commissions_cad) for s in sums)],
        ["Spread and impact (CAD)", *(_cad(s.spread_impact_cad) for s in sums)],
        ["FX conversion (CAD)", *(_cad(s.fx_conversion_cad) for s in sums)],
        ["Trades", *(str(s.trades) for s in sums)],
        ["Unfilled order remainders", *(str(s.unfilled) for s in sums)],
        ["Alerts", *(str(s.alerts) for s in sums)],
    ]
    for g in sorted({g for s in sums for g in s.gate_binds}):
        counts = [s.gate_binds.get(g, (0, 0)) for s in sums]
        srows.append([f"Gate bound: {g}", *(f"{b} of {n} decisions" for b, n in counts)])
    out.append(_table(["", *(f"{s.cost_multiplier:g}x costs" for s in sums)], srows))

    out.append(f"<h2>Trades ({sums[0].cost_multiplier:g}x costs)</h2>")
    trows = [
        [str(t.day), t.decision_id, t.symbol, t.side, t.currency, t.reason, t.gates or "-"]
        + [f"{t.shares:,}", str(t.price), _cad(float(t.gross)), str(t.commission)]
        for t in main.ledger.trades
    ]
    thead = ["date", "decision", "fund", "side", "ccy", "reason", "gates (scale)"]
    table = _table([*thead, "shares", "price", "gross", "commission"], trows, left=len(thead))
    out.append(f"<details open><summary>Trade list</summary>{table}</details>")
    foot = (
        f"Costs are run at 1x and 2x; pass or fail is decided at 2x. {EDGE_CAVEAT} Fills use the "
        "next session's close moved by the cost model (§10.4); NAV is valued at the Bank of "
        "Canada rate."
    )
    out.append(f'<p class="foot">{esc(foot)}</p>')
    payload = {
        "nav": {
            "days": [d.isoformat() for d in days],
            "series": [
                {
                    "name": n,
                    "var": v,
                    "values": [round(x, 2) if math.isfinite(x) else None for x in s],
                }
                for n, v, s in series
            ],
        },
        "weights": {
            "days": [d.isoformat() for d in wdays],
            "symbols": symbols,
            "values": [[round(w.get(s, 0.0), 6) for s in symbols] for w in wvals],
        },
    }
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    out.append(_tag("script", {"type": "application/json", "id": "report-data"}, data))
    out.append(f"<script>{_asset('report.js')}</script>")
    out.append("</div></body></html>")
    return "\n".join(out)


def _d(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))
