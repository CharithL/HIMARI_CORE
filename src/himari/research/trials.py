"""Trial ledger (§12.6, `state/trials.sqlite`).

Every run is appended, failures and ad-hoc explorations included; rows are never updated or
deleted (SQLite triggers refuse it). Each row records the data version it ran on, and its daily
excess returns go in a second append-only table, `trial_returns`, for PBO.

v1.8: the DSR and PBO trial counts take only rows run on real market data. Fixture and demo
runs (data version `fixtures:...`) stay in the ledger but are not counted.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from himari.research.stats import PBOResult, pbo

SYNTHETIC_DATA_PREFIX = "fixtures:"  # the committed synthetic fixture set: demo and test runs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id TEXT NOT NULL UNIQUE,
    hypothesis_id TEXT NOT NULL,
    utc TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    data_version TEXT NOT NULL,
    cost_multiplier REAL NOT NULL,
    "window" TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    p_value REAL,
    result TEXT NOT NULL,
    notes TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trials_no_update BEFORE UPDATE ON trials
BEGIN SELECT RAISE(ABORT, 'trials is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trials_no_delete BEFORE DELETE ON trials
BEGIN SELECT RAISE(ABORT, 'trials is append-only'); END;
CREATE TABLE IF NOT EXISTS trial_returns (
    trial_id TEXT NOT NULL REFERENCES trials (trial_id),
    day TEXT NOT NULL,
    r_ex REAL NOT NULL,
    PRIMARY KEY (trial_id, day)
);
CREATE TRIGGER IF NOT EXISTS trial_returns_no_update BEFORE UPDATE ON trial_returns
BEGIN SELECT RAISE(ABORT, 'trial_returns is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trial_returns_no_delete BEFORE DELETE ON trial_returns
BEGIN SELECT RAISE(ABORT, 'trial_returns is append-only'); END;
"""


def is_real_data(data_version: str) -> bool:
    """v1.8: a trial counts for DSR and PBO only when it ran on real market data."""
    return not data_version.startswith(SYNTHETIC_DATA_PREFIX)


_COLUMNS = (
    "trial_id",
    "hypothesis_id",
    "utc",
    "git_commit",
    "code_hash",
    "config_hash",
    "data_version",
    "cost_multiplier",
    "window",
    "metrics_json",
    "p_value",
    "result",
    "notes",
)


@dataclass(frozen=True)
class Trial:
    trial_id: str
    hypothesis_id: str  # a register id (H1, ...), "baseline" or "adhoc"
    utc: str
    git_commit: str
    code_hash: str
    config_hash: str
    data_version: str
    cost_multiplier: float
    window: str  # "YYYY-MM-DD..YYYY-MM-DD"
    metrics_json: str
    p_value: float | None
    result: str
    notes: str


class TrialLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as con, con:
            con.executescript(_SCHEMA)

    def append(self, trial: Trial, returns: Sequence[tuple[date, float]] = ()) -> None:
        """One trial, and its daily excess returns (session, r_ex) when it has any, in one
        transaction."""
        row = asdict(trial)
        json.loads(trial.metrics_json)  # must be valid JSON
        cols = ", ".join(f'"{c}"' for c in _COLUMNS)
        marks = ", ".join("?" for _ in _COLUMNS)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute(f"INSERT INTO trials ({cols}) VALUES ({marks})", [row[c] for c in _COLUMNS])
            con.executemany(
                "INSERT INTO trial_returns (trial_id, day, r_ex) VALUES (?, ?, ?)",
                [(trial.trial_id, d.isoformat(), float(r)) for d, r in returns],
            )

    def rows(self) -> list[dict[str, Any]]:
        cols = ", ".join(f'"{c}"' for c in _COLUMNS)
        with closing(sqlite3.connect(self.path)) as con:
            found = con.execute(f"SELECT {cols} FROM trials ORDER BY seq").fetchall()
        return [dict(zip(_COLUMNS, r, strict=True)) for r in found]

    def real_data_rows(self) -> list[dict[str, Any]]:
        return [r for r in self.rows() if is_real_data(r["data_version"])]

    def trial_count(self) -> int:
        """The ledger's part of the DSR trial count N (§12.4, v1.8): real-data rows only. The
        old alpha factory's trials (`research.legacy_trials_estimate`) are added separately."""
        return len(self.real_data_rows())

    def returns(self, trial_id: str) -> list[tuple[date, float]]:
        with closing(sqlite3.connect(self.path)) as con:
            found = con.execute(
                "SELECT day, r_ex FROM trial_returns WHERE trial_id = ? ORDER BY day", (trial_id,)
            ).fetchall()
        return [(date.fromisoformat(d), float(r)) for d, r in found]


@dataclass(frozen=True)
class LedgerPBO:
    trials: list[str]  # the trial ids in the matrix
    sessions: int  # common sessions
    result: PBOResult | None
    note: str


def ledger_pbo(
    ledger: TrialLedger,
    cost_multiplier: float = 2.0,
    window: str | None = None,
    partitions: int = 16,
) -> LedgerPBO:
    """PBO (§12.4, diagnostic) over the ledger's real-data trials at one cost multiplier (and
    window, if given), on the sessions they all share."""
    rows = [
        r
        for r in ledger.real_data_rows()
        if r["cost_multiplier"] == cost_multiplier and (window is None or r["window"] == window)
    ]
    series = {r["trial_id"]: dict(ledger.returns(r["trial_id"])) for r in rows}
    series = {t: s for t, s in series.items() if s}
    if len(series) < 2:
        return LedgerPBO(list(series), 0, None, "needs at least 2 real-data trials with returns")
    common = sorted(set.intersection(*(set(s) for s in series.values())))
    if len(common) < 2 * partitions:
        return LedgerPBO(list(series), len(common), None, "too few common sessions")
    ids = sorted(series)
    matrix = np.array([[series[t][d] for t in ids] for d in common], dtype=np.float64)
    return LedgerPBO(ids, len(common), pbo(matrix, partitions), "")
