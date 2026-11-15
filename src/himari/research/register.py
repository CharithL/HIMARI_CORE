"""The research register (§12.5, Appendix D): at most 10 tests, run one at a time in order,
each a single change against the frozen baseline; Holm over the whole family once every test
has reached its endpoint, and p <= early_decision_p for any decision before then.

This module loads and checks `research/register.yaml` and reports each test's status from the
trial ledger's real-data rows (v1.8). Running a test is M8.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from himari.research.stats import holm

if TYPE_CHECKING:
    from himari.research.trials import TrialLedger

MAX_TESTS = 10


class RegisterTest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    stage: Literal[1, 2]
    change: str | None = None
    primary: str
    guards: list[str] = Field(default_factory=list)
    window: str
    depends_on: str | None = None


class Register(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family_alpha: float
    correction: Literal["holm"]
    tests: list[RegisterTest]

    @model_validator(mode="after")
    def _rules(self) -> Register:
        ids = [t.id for t in self.tests]
        if not 1 <= len(ids) <= MAX_TESTS:
            raise ValueError(f"the register holds 1 to {MAX_TESTS} tests, not {len(ids)}")
        if len(set(ids)) != len(ids):
            raise ValueError("register test ids must be unique")
        for i, t in enumerate(self.tests):
            if t.depends_on is not None and t.depends_on not in ids[:i]:
                raise ValueError(f"{t.id} depends on {t.depends_on}, which is not an earlier test")
        return self


def load_register(path: Path, holm_alpha: float | None = None) -> Register:
    reg = Register.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if holm_alpha is not None and reg.family_alpha != holm_alpha:
        raise ValueError(
            f"family_alpha {reg.family_alpha} differs from research.holm_alpha {holm_alpha}"
        )
    return reg


@dataclass(frozen=True)
class EntryStatus:
    test: RegisterTest
    trials: int  # real-data rows for this test
    p_value: float | None  # the first p-value logged: read once, at the frozen endpoint
    holm_reject: bool | None  # None until every test has reached its endpoint


@dataclass(frozen=True)
class RegisterStatus:
    tests: list[EntryStatus]
    endpoints_reached: int
    holm_applied: bool


def register_status(reg: Register, ledger: TrialLedger) -> RegisterStatus:
    rows = ledger.real_data_rows()
    firsts: list[tuple[RegisterTest, int, float | None]] = []
    for t in reg.tests:
        mine = [r for r in rows if r["hypothesis_id"] == t.id]
        ps = [r["p_value"] for r in mine if r["p_value"] is not None]
        firsts.append((t, len(mine), float(ps[0]) if ps else None))
    reached = sum(1 for _, _, p in firsts if p is not None)
    complete = reached == len(reg.tests)
    flags: list[bool | None] = (
        list(holm([p for _, _, p in firsts if p is not None], reg.family_alpha))
        if complete
        else [None] * len(firsts)
    )
    return RegisterStatus(
        [EntryStatus(t, n, p, f) for (t, n, p), f in zip(firsts, flags, strict=True)],
        reached,
        complete,
    )
