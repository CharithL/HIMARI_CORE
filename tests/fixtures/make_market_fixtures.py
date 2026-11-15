"""Generate the committed market fixtures under tests/fixtures/market/.

SYNTHETIC DATA: prices, distributions and their pay dates, the reinvested year-end
distributions, the split, FX, CORRA, the Bank of Canada holiday calendar and the Q10
confirmations are invented for tests. They are not market data and not the Bank's real
holiday list. Regenerate with:

    python tests/fixtures/make_market_fixtures.py
"""

from __future__ import annotations

import bisect
import csv
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from himari.data.calendars import sessions  # noqa: E402

SEED = 20261111
START, END = date(2024, 1, 2), date(2025, 12, 31)
ROOT = Path(__file__).resolve().parent / "market"

# symbol: (calendar, start price, annual vol, annual drift, payout schedule, yield per payout)
UNIVERSE: dict[str, tuple[str, float, float, float, str | None, float]] = {
    "VCN": ("XTSE", 40.00, 0.13, 0.07, "quarterly", 0.0060),
    "VUN": ("XTSE", 90.00, 0.15, 0.09, "quarterly", 0.0030),
    "VIU": ("XTSE", 35.00, 0.14, 0.05, "quarterly", 0.0070),
    "VEE": ("XTSE", 38.00, 0.16, 0.04, "quarterly", 0.0060),
    "VGV": ("XTSE", 27.00, 0.06, 0.02, "monthly", 0.0025),
    "CGL.C": ("XTSE", 22.00, 0.15, 0.08, None, 0.0),
    "PDBC": ("XNAS", 13.50, 0.18, 0.00, "annual", 0.0400),
    "DLR": ("XTSE", 13.70, 0.06, 0.00, None, 0.0),
}
SPLITS = {("VIU", date(2025, 6, 16)): 2.0}
ZERO_VOLUME = {("VGV", date(2025, 8, 14))}
# v1.6: year-end distributions the fund reinvests and consolidates (no cash, no price effect),
# on the last session of December; amounts per unit
REINVESTED = {("VCN", 2024): 0.0450, ("VCN", 2025): 0.0480}
PAY_LAG_SESSIONS = 5  # synthetic: a cash distribution is paid 5 sessions after its ex-date
CONFIRMED_AT = "2026-01-05T00:00:00+00:00"
# v1.7: a synthetic T-bill ETF for the cash sleeve, from its own inception mid-2024. Built
# without the random stream (so every other fixture series is unchanged): the price accrues
# CORRA less a 0.10% fee and pays it out at each month's last session.
SLEEVE, SLEEVE_INCEPTION, SLEEVE_PRICE, SLEEVE_FEE = "TBILL.SYN", date(2024, 7, 2), 50.0, 0.10

# Synthetic Bank of Canada closures: the synthetic FX/CORRA publication record skips these
# weekdays (chosen to exercise Easter bridges, Remembrance Day, etc.)
BOC_HOLIDAYS = {
    2024: [
        "01-01",
        "02-19",
        "03-29",
        "04-01",
        "05-20",
        "07-01",
        "08-05",
        "09-02",
        "09-30",
        "10-14",
        "11-11",
        "12-25",
        "12-26",
    ],
    2025: [
        "01-01",
        "02-17",
        "04-18",
        "04-21",
        "05-19",
        "07-01",
        "08-04",
        "09-01",
        "09-30",
        "10-13",
        "11-11",
        "12-25",
        "12-26",
    ],
}


def _first_on_or_after(days: list[date], target: date) -> date | None:
    return next((d for d in days if d >= target), None)


def _ex_dates(days: list[date], schedule: str | None) -> set[date]:
    if schedule is None:
        return set()
    months = {"quarterly": (3, 6, 9, 12), "monthly": tuple(range(1, 13)), "annual": (12,)}
    day_of_month = 20 if schedule == "annual" else 25
    out = set()
    for year in (2024, 2025):
        for month in months[schedule]:
            d = _first_on_or_after(days, date(year, month, day_of_month))
            if d is not None and d.month == month:
                out.add(d)
    return out


def _write(path: Path, header: list[str], rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _boc_days() -> list[date]:
    holidays = {date.fromisoformat(f"{y}-{md}") for y, mds in BOC_HOLIDAYS.items() for md in mds}
    out, d = [], date(2024, 1, 1)
    while d <= END:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def _pay_date(calendar: str, ex_date: date) -> date:
    later = sessions(calendar, ex_date + timedelta(days=1), ex_date + timedelta(days=30))
    return later[PAY_LAG_SESSIONS - 1]


def _sleeve(corra: list[list[str]]) -> list[tuple[str, str]]:
    """The synthetic sleeve fund's bars (primary and cross-check) and monthly distributions;
    returns its December ex-dates for Q10."""
    rates = [(date.fromisoformat(d), float(v)) for d, v in corra]
    rate_days = [r for r, _ in rates]
    days = sessions("XTSE", SLEEVE_INCEPTION, END)
    months = {(d.year, d.month) for d in days}
    month_ends = {max(d for d in days if (d.year, d.month) == ym) for ym in months}
    accrued, prev, rows, actions = 0.0, days[0], [], []
    for d in days:
        # the latest CORRA dated before d, as the cash index uses the previous session's rate
        rate = rates[max(bisect.bisect_left(rate_days, d) - 1, 0)][1]
        accrued += SLEEVE_PRICE * (rate - SLEEVE_FEE) / 100.0 * (d - prev).days / 365.0
        prev = d
        dist = round(accrued, 4) if d in month_ends else 0.0
        close = round(SLEEVE_PRICE + accrued - dist, 2)
        rows.append([d, close, dist])
        if dist:
            accrued -= dist
            pay = _pay_date("XTSE", d).isoformat()
            actions.append([d.isoformat(), pay, "cash", f"{dist:.4f}", "CAD"])
    factor, adj = 1.0, [0.0] * len(rows)
    for i in range(len(rows) - 1, -1, -1):
        adj[i] = round(rows[i][1] * factor, 6)
        if i > 0:
            factor *= rows[i][1] / (rows[i][1] + rows[i][2])
    bars, xbars = [], []
    for i, (d, c, _) in enumerate(rows):
        o = rows[i - 1][1] if i else c
        hi, lo = round(max(o, c) + 0.01, 2), round(min(o, c) - 0.01, 2)
        bars.append([d.isoformat(), o, hi, lo, c, 1_000_000, adj[i]])
        xbars.append([d.isoformat(), o, hi, lo, c, 1_000_000])
    head = ["session_date", "open", "high", "low", "close", "volume"]
    _write(ROOT / "primary" / "bars" / f"{SLEEVE}.csv", [*head, "adj_close"], bars)
    _write(ROOT / "crosscheck" / "bars" / f"{SLEEVE}.csv", head, xbars)
    _write(ROOT / "primary" / "actions" / f"{SLEEVE}.csv",
           ["ex_date", "pay_date", "type", "amount", "currency"], actions)  # fmt: skip
    return [(SLEEVE, a[0]) for a in actions if a[0][5:7] == "12"]


def main() -> None:
    rng = np.random.default_rng(SEED)
    december: list[tuple[str, str]] = []  # (symbol, ex_date) of CAD-listed December payouts
    for sym, (cal, p0, vol, drift, schedule, yld) in UNIVERSE.items():
        days = sessions(cal, START, END)
        ex = _ex_dates(days, schedule)
        closes, opens, highs, lows, vols, splits, cash = [], [], [], [], [], [], []
        prev = p0
        for i, d in enumerate(days):
            s = SPLITS.get((sym, d), 1.0)
            dist = round(yld * prev, 4) if d in ex and i > 0 else 0.0
            if i == 0:
                c = p0
            else:
                r = math.exp(rng.normal(drift / 252, vol / math.sqrt(252))) - 1.0
                c = round(prev * (1.0 + r) / s - dist, 2)
            o = round(prev / s * (1.0 + rng.normal(0.0, 0.003)), 2) if i > 0 else p0
            h = round(max(o, c) + abs(rng.normal(0.0, 0.004)) * c + 0.01, 2)
            lo = round(min(o, c) - abs(rng.normal(0.0, 0.004)) * c - 0.01, 2)
            v = 0 if (sym, d) in ZERO_VOLUME else int(rng.integers(5_000, 400_000))
            closes.append(c), opens.append(o), highs.append(h), lows.append(lo), vols.append(v)
            splits.append(s), cash.append(dist)
            prev = c
        factor, adj = 1.0, [0.0] * len(days)
        for i in range(len(days) - 1, -1, -1):
            adj[i] = round(closes[i] * factor, 6)
            if i > 0:
                factor *= closes[i] / (splits[i] * (closes[i] + cash[i]))
        _write(
            ROOT / "primary" / "bars" / f"{sym}.csv",
            ["session_date", "open", "high", "low", "close", "volume", "adj_close"],
            [
                [d.isoformat(), o, h, lo, c, v, a]
                for d, o, h, lo, c, v, a in zip(
                    days, opens, highs, lows, closes, vols, adj, strict=True
                )
            ],
        )
        currency = "USD" if cal == "XNAS" else "CAD"
        actions = [
            [d.isoformat(), _pay_date(cal, d).isoformat(), "cash", f"{x:.4f}", currency]
            for d, x in zip(days, cash, strict=True)
            if x > 0
        ]
        actions += [
            [d.isoformat(), "", "split", f"{s:g}", ""]
            for d, s in zip(days, splits, strict=True)
            if s != 1.0
        ]
        for (rsym, year), amount in REINVESTED.items():
            if rsym == sym:
                last = max(d for d in days if d.year == year and d.month == 12)
                actions.append([last.isoformat(), "", "reinvested", f"{amount:.4f}", currency])
        _write(
            ROOT / "primary" / "actions" / f"{sym}.csv",
            ["ex_date", "pay_date", "type", "amount", "currency"],
            sorted(actions),
        )
        if currency == "CAD":
            december.extend(
                (sym, row[0]) for row in actions if row[2] != "split" and row[0][5:7] == "12"
            )
        xrows = []
        for d, o, h, lo, c, v in zip(days, opens, highs, lows, closes, vols, strict=True):
            xc = round(c * (1.0 + rng.uniform(-0.0015, 0.0015)), 2)
            xrows.append([d.isoformat(), o, max(h, xc), min(lo, xc), xc, v])
        _write(
            ROOT / "crosscheck" / "bars" / f"{sym}.csv",
            ["session_date", "open", "high", "low", "close", "volume"],
            xrows,
        )

    boc = _boc_days()
    fx, x = [], 1.3500
    for d in boc:
        fx.append([d.isoformat(), f"{x:.4f}"])
        x *= math.exp(rng.normal(0.0, 0.06 / math.sqrt(252)))
    _write(ROOT / "primary" / "series" / "FXUSDCAD.csv", ["date", "value"], fx)
    corra = []
    for d in boc:
        base = (
            4.00
            if d < date(2024, 7, 1)
            else 3.50
            if d < date(2025, 1, 1)
            else (3.00 if d < date(2025, 7, 1) else 2.75)
        )
        corra.append([d.isoformat(), f"{base + rng.normal(0.0, 0.01):.2f}"])
    _write(ROOT / "primary" / "series" / "AVG.INTWO.csv", ["date", "value"], corra)
    december.extend(_sleeve(corra))

    # Q10 (v1.6): the December cash/reinvested splits, "confirmed" for the synthetic set
    records = [
        {
            "check": "Q10",
            "confirmed_at_utc": CONFIRMED_AT,
            "reason": "synthetic fixture: split as stored",
            "session_date": ex,
            "symbol": sym,
        }
        for sym, ex in sorted(set(december))
    ]
    with open(ROOT / "confirmations.jsonl", "w", newline="", encoding="utf-8") as f:
        f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in records)


if __name__ == "__main__":
    main()
