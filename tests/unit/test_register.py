"""The research register (§12.5, Appendix D, M5): the loader's rules and the status view, which
counts real-data trials only (v1.8) and applies Holm once every endpoint is reached."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from himari.research.register import Register, load_register, register_status
from himari.research.trials import Trial, TrialLedger

ROOT = Path(__file__).parents[2]


def _t(i: int, **kw: Any) -> dict[str, Any]:
    return {"id": f"H{i}", "title": "t", "stage": 1, "primary": "p", "window": "evaluation", **kw}


def _reg(tests: list[dict[str, Any]], alpha: float = 0.05) -> dict[str, Any]:
    return {"family_alpha": alpha, "correction": "holm", "tests": tests}


def _trial(tid: str, hyp: str, p: float | None, version: str = "vendor:abc") -> Trial:
    return Trial(tid, hyp, "2026-11-15T00:00:00+00:00", "abc", "code", "cfg", version, 2.0,
                 "2015-01-02..2025-12-31", "{}", p, "completed", "")  # fmt: skip


def test_the_committed_register_loads() -> None:
    reg = load_register(ROOT / "research" / "register.yaml", 0.05)
    assert [t.id for t in reg.tests] == [f"H{i}" for i in range(1, 11)]
    h4 = reg.tests[3]
    assert h4.depends_on == "H2" and h4.change is None  # H4 has no change line in Appendix D


@pytest.mark.parametrize(
    "tests",
    [
        [_t(i) for i in range(1, 12)],  # 11 tests
        [_t(1), _t(1)],  # a duplicate id
        [_t(1, depends_on="H2"), _t(2)],  # depends on a later test
        [_t(1, stage=3)],  # no such stage
        [_t(1, extra="x")],  # an unknown field
    ],
)
def test_the_register_rules(tests: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        Register.model_validate(_reg(tests))


def test_family_alpha_must_match_the_config(tmp_path: Path) -> None:
    path = tmp_path / "register.yaml"
    path.write_text(yaml.safe_dump(_reg([_t(1)], alpha=0.10)), encoding="utf-8")
    with pytest.raises(ValueError):
        load_register(path, 0.05)


def test_status_counts_real_data_trials_and_applies_holm_when_complete(tmp_path: Path) -> None:
    reg = Register.model_validate(_reg([_t(1), _t(2)]))
    ledger = TrialLedger(tmp_path / "trials.sqlite")
    ledger.append(_trial("T-f", "H1", 0.001, version="fixtures:x"))  # not counted
    st = register_status(reg, ledger)
    assert st.endpoints_reached == 0 and not st.holm_applied
    ledger.append(_trial("T-1", "H1", 0.01))
    st = register_status(reg, ledger)
    assert st.endpoints_reached == 1 and st.tests[0].holm_reject is None
    ledger.append(_trial("T-2", "H2", 0.06))
    st = register_status(reg, ledger)
    assert st.holm_applied and [e.holm_reject for e in st.tests] == [True, False]
