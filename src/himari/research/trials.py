"""Trial ledger (§12.6, `state/trials.sqlite`).

Every run is appended, failures and ad-hoc explorations included; rows are never updated or
deleted (SQLite triggers refuse it). The DSR trial count (M5) comes from this table, so each
row records the data version it ran on: runs on the synthetic fixtures can be told apart.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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
"""

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

    def append(self, trial: Trial) -> None:
        row = asdict(trial)
        json.loads(trial.metrics_json)  # must be valid JSON
        cols = ", ".join(f'"{c}"' for c in _COLUMNS)
        marks = ", ".join("?" for _ in _COLUMNS)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute(f"INSERT INTO trials ({cols}) VALUES ({marks})", [row[c] for c in _COLUMNS])

    def rows(self) -> list[dict[str, Any]]:
        cols = ", ".join(f'"{c}"' for c in _COLUMNS)
        with closing(sqlite3.connect(self.path)) as con:
            found = con.execute(f"SELECT {cols} FROM trials ORDER BY seq").fetchall()
        return [dict(zip(_COLUMNS, r, strict=True)) for r in found]
