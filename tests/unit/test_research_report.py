"""DSR's V (v1.9): max(the cross-trial variance of the real-data trials' per-period Sharpe
ratios, 1 / (T - 1)); with fewer than 2 real-data trials it is 1 / (T - 1)."""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from himari.research.report import dsr_variance
from himari.research.stats import sharpe_moments
from himari.research.trials import Trial, TrialLedger

T = 300
DAYS = [date(2020, 1, 1) + timedelta(days=i) for i in range(T)]


def _trial(tid: str, version: str = "vendor:abc") -> Trial:
    return Trial(tid, "adhoc", "2026-11-16T00:00:00+00:00", "abc", "code", "cfg", version, 2.0,
                 "2015-01-02..2025-12-31", "{}", None, "completed", "")  # fmt: skip


def _append(ledger: TrialLedger, tid: str, r: np.ndarray, version: str = "vendor:abc") -> None:
    ledger.append(_trial(tid, version), list(zip(DAYS, r.tolist(), strict=True)))


def test_no_or_one_real_trial_gives_one_over_t(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "t.sqlite")
    rng = np.random.default_rng(1)
    _append(ledger, "T-fix", rng.normal(0, 0.01, T), version="fixtures:x")  # not counted
    assert dsr_variance(ledger, T)[0] == pytest.approx(1 / (T - 1))
    _append(ledger, "T-1", rng.normal(0, 0.01, T))
    assert dsr_variance(ledger, T)[0] == pytest.approx(1 / (T - 1))


def test_similar_trials_keep_the_floor(tmp_path: Path) -> None:
    """Nearly identical trials have almost no cross-trial variance: V stays at 1 / (T - 1)."""
    ledger = TrialLedger(tmp_path / "t.sqlite")
    r = np.random.default_rng(2).normal(0.0005, 0.01, T)
    _append(ledger, "T-1", r)
    _append(ledger, "T-2", r * 1.001)
    v, note = dsr_variance(ledger, T)
    assert v == pytest.approx(1 / (T - 1)) and "1 / (T - 1)" in note


def test_dispersed_trials_set_v(tmp_path: Path) -> None:
    ledger = TrialLedger(tmp_path / "t.sqlite")
    base = np.random.default_rng(3).normal(0.0, 0.01, T)
    series = [base + 0.002, base - 0.002, base]  # per-period Sharpe about +0.2, -0.2 and 0
    for i, r in enumerate(series):
        _append(ledger, f"T-{i}", r)
    expected = float(np.var([sharpe_moments(r).sr for r in series], ddof=1))
    v, note = dsr_variance(ledger, T)
    assert expected > 1 / (T - 1) and v == pytest.approx(expected) and "3 real-data" in note
