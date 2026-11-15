"""The trial ledger (§12.6): every run appended; rows never updated or deleted (rule 6)."""

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from himari.research.trials import Trial, TrialLedger, is_real_data, ledger_pbo


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


def real(tid: str, version: str = "vendor:abc", m: float = 2.0) -> Trial:
    return Trial(tid, "adhoc", "2026-11-15T00:00:00+00:00", "abc", "code", "cfg", version, m,
                 "2015-01-02..2025-12-31", "{}", None, "completed", "")  # fmt: skip


def test_the_trial_count_takes_real_data_rows_only(tmp_path: Path) -> None:
    """v1.8: fixture and demo runs stay in the ledger, but DSR and PBO do not count them."""
    ledger = TrialLedger(tmp_path / "trials.sqlite")
    for i in range(3):
        ledger.append(trial(f"T-f{i}"))  # data version fixtures:...
    ledger.append(real("T-r1"))
    ledger.append(real("T-r2", "csv:def"))
    assert len(ledger.rows()) == 5 and ledger.trial_count() == 2
    assert [r["trial_id"] for r in ledger.real_data_rows()] == ["T-r1", "T-r2"]
    assert not is_real_data("fixtures:abc") and is_real_data("eodhd:abc")


def test_returns_are_stored_with_the_trial_and_never_changed(tmp_path: Path) -> None:
    path = tmp_path / "trials.sqlite"
    ledger = TrialLedger(path)
    ledger.append(real("T-1"), [(date(2025, 1, 2), 0.001), (date(2025, 1, 3), -0.002)])
    assert ledger.returns("T-1") == [(date(2025, 1, 2), 0.001), (date(2025, 1, 3), -0.002)]
    for sql in ("UPDATE trial_returns SET r_ex = 0", "DELETE FROM trial_returns"):
        with closing(sqlite3.connect(path)) as con, pytest.raises(sqlite3.DatabaseError):
            con.execute(sql)


def test_ledger_pbo_uses_real_data_trials_on_their_common_sessions(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "trials.sqlite")
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(640)]
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 0.01, 640).tolist()
    ledger.append(trial("T-fix"), list(zip(days, noise, strict=True)))
    assert ledger_pbo(ledger).result is None  # the fixture trial does not count
    for k in range(4):
        r = rng.normal(0.002 if k == 0 else 0.0, 0.01, 640).tolist()
        ledger.append(real(f"T-{k}"), list(zip(days, r, strict=True)))
    one_x = rng.normal(0, 0.01, 640).tolist()
    ledger.append(real("T-1x", m=1.0), list(zip(days, one_x, strict=True)))
    out = ledger_pbo(ledger, 2.0)
    assert out.result is not None and out.trials == ["T-0", "T-1", "T-2", "T-3"]
    assert out.sessions == 640 and out.result.pbo < 0.2  # T-0 has a real edge
