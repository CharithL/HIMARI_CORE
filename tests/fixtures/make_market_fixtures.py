"""Generate the committed market fixtures under tests/fixtures/market/.

SYNTHETIC DATA: prices, distributions, the split, FX, CORRA and the Bank of Canada
holiday calendar are invented for tests. They are not market data and not the Bank's
real holiday list. Regenerate with:

    python tests/fixtures/make_market_fixtures.py
"""

from __future__ import annotations

import csv
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

# Synthetic Bank of Canada holidays (chosen to exercise Easter bridges, Remembrance Day, etc.)
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


def main() -> None:
    rng = np.random.default_rng(SEED)
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
        actions = [
            [d.isoformat(), "", "cash", f"{x:.4f}", "USD" if cal == "XNAS" else "CAD"]
            for d, x in zip(days, cash, strict=True)
            if x > 0
        ]
        actions += [
            [d.isoformat(), "", "split", f"{s:g}", ""]
            for d, s in zip(days, splits, strict=True)
            if s != 1.0
        ]
        _write(
            ROOT / "primary" / "actions" / f"{sym}.csv",
            ["ex_date", "pay_date", "type", "amount", "currency"],
            sorted(actions),
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

    lines = [
        "# SYNTHETIC test calendar for tests/fixtures/market (not the Bank's real holidays).",
        "covered_years: [2024, 2025]",
        "holidays:",
    ]
    for y, mds in BOC_HOLIDAYS.items():
        lines += [f"  - {{date: {y}-{md}, name: synthetic, scope: national}}" for md in mds]
    (ROOT / "boc_holidays.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
