"""A shuffled-returns copy of a CSV market (`CsvSource` layout), for diagnostics (M5 review).

Each fund's daily price returns are permuted in time with a seeded generator, which keeps the
returns but removes any day-to-day dependence (momentum, mean reversion) and any information a
decision could take from the order of the path. Split and distribution dates and amounts stay
where they are; open, high, low, adj_close and the cross-check closes are rebuilt to match, so
the quality gate passes as it does on the original. Never used to choose anything.
"""

from __future__ import annotations

import csv
import shutil
from collections.abc import Sequence
from pathlib import Path

import numpy as np


def _read(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def shuffled_market(source: Path, dest: Path, symbols: Sequence[str], seed: int) -> Path:
    """Copy `source` (primary/ and crosscheck/ CSV folders) to `dest` with each symbol's daily
    returns shuffled; returns `dest`."""
    shutil.copytree(source, dest)
    rng = np.random.default_rng(seed)
    for sym in symbols:
        bars_path = dest / "primary" / "bars" / f"{sym}.csv"
        bars = _read(bars_path)
        actions = _read(dest / "primary" / "actions" / f"{sym}.csv")
        split = {a["ex_date"]: float(a["amount"]) for a in actions if a["type"] == "split"}
        cash = {a["ex_date"]: float(a["amount"]) for a in actions if a["type"] == "cash"}
        days = [b["session_date"] for b in bars]
        close = [float(b["close"]) for b in bars]
        # a session's step: close = previous close x (1 + r) / split - distribution
        r = [
            (close[i] + cash.get(days[i], 0.0)) * split.get(days[i], 1.0) / close[i - 1] - 1.0
            for i in range(1, len(days))
        ]
        r = [r[j] for j in rng.permutation(len(r))]
        new = [close[0]]
        for i in range(1, len(days)):
            s, dist = split.get(days[i], 1.0), cash.get(days[i], 0.0)
            new.append(round(new[-1] * (1.0 + r[i - 1]) / s - dist, 2))
        factor, adj = 1.0, [0.0] * len(days)
        for i in range(len(days) - 1, -1, -1):  # split- and distribution-adjusted close
            adj[i] = round(new[i] * factor, 6)
            if i > 0:
                c = new[i]
                factor *= c / (split.get(days[i], 1.0) * (c + cash.get(days[i], 0.0)))
        cross_path = dest / "crosscheck" / "bars" / f"{sym}.csv"
        cross = _read(cross_path)
        for i, (b, xb) in enumerate(zip(bars, cross, strict=True)):
            o = round(new[i - 1] / split.get(days[i], 1.0), 2) if i else new[0]
            c = new[i]
            b.update(
                open=str(o),
                high=str(round(max(o, c) * 1.002, 2)),
                low=str(round(min(o, c) * 0.998, 2)),
                close=str(c),
                adj_close=str(adj[i]),
            )
            xb.update(open=b["open"], high=b["high"], low=b["low"], close=str(c))
        _write(bars_path, bars)
        _write(cross_path, cross)
    return dest
