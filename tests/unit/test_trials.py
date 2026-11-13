"""The trial ledger (§12.6): every run appended; rows never updated or deleted (rule 6)."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from himari.research.trials import Trial, TrialLedger


def trial(tid: str, metrics: str = '{"total_return": 0.05}') -> Trial:
    return Trial(tid, "adhoc", "2026-11-13T00:00:00+00:00", "abc123", "code", "cfg",
                 "fixtures:xyz", 2.0, "2024-01-02..2025-12-31", metrics, None, "completed",
                 "demo")  # fmt: skip


def test_rows_round_trip_in_order(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "trials.sqlite")
    ledger.append(trial("T-1"))
    ledger.append(trial("T-2"))
    rows = ledger.rows()
    assert [r["trial_id"] for r in rows] == ["T-1", "T-2"]
    assert rows[0]["window"] == "2024-01-02..2025-12-31" and rows[0]["cost_multiplier"] == 2.0


@pytest.mark.parametrize("sql", ["UPDATE trials SET result = 'x'", "DELETE FROM trials"])
def test_rows_are_never_changed(tmp_path: Path, sql: str) -> None:
    path = tmp_path / "trials.sqlite"
    TrialLedger(path).append(trial("T-1"))
    with closing(sqlite3.connect(path)) as con, pytest.raises(sqlite3.DatabaseError):
        con.execute(sql)


def test_metrics_must_be_json(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TrialLedger(tmp_path / "trials.sqlite").append(trial("T-1", metrics="not json"))


def test_trial_ids_are_unique(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "trials.sqlite")
    ledger.append(trial("T-1"))
    with pytest.raises(sqlite3.IntegrityError):
        ledger.append(trial("T-1"))
