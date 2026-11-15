"""The final ceiling re-check (§8.1, §8.2, amendment v1.7).

After the last gate, sigma_p <= 0.08 and every stress loss is within its limit, for random
targets, holdings, covariances (correlations of either sign), drawdown states and funds capped
by the data, event or fault gates.
"""

from pathlib import Path

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from himari.core.config import load_config
from himari.core.types import DrawdownState
from himari.gates.ceiling import portfolio_vol
from himari.gates.pipeline import GateContext, run_gates
from himari.quality.gate import InstrumentQuality, QualityReport
from himari.risk.stress import stress_test
from tests.property.test_gates_cut_only import FOMC, SYMBOLS, covariance, flags, weights

CFG = load_config(Path(__file__).parents[2] / "config" / "frozen_v1.yaml")
EPS = 1e-12


@settings(max_examples=400, deadline=None)
@given(
    target=weights,
    held=weights,
    cov=covariance(),
    faulted=flags,
    pre_event=flags,
    dd_state=st.sampled_from(list(DrawdownState)),
    venue=st.booleans(),
    kill=st.booleans(),
)
def test_after_the_last_gate_risk_is_within_every_limit(
    target: list[float],
    held: list[float],
    cov: np.ndarray,
    faulted: list[bool],
    pre_event: list[bool],
    dd_state: DrawdownState,
    venue: bool,
    kill: bool,
) -> None:
    ctx = GateContext(
        quality=QualityReport(
            instruments={
                s: InstrumentQuality(symbol=s, status="FAULT" if f else "OK", results=[])
                for s, f in zip(SYMBOLS, faulted, strict=True)
            },
            portfolio=[],
        ),
        symbols=SYMBOLS,
        covariance=cov,
        vol_ceiling=CFG.risk.portfolio_vol_ceiling,
        pre_event={s: [FOMC] for s, p in zip(SYMBOLS, pre_event, strict=True) if p},
        stress=CFG.stress,
        drawdown_state=dd_state,
        halve_multiplier=CFG.drawdown.halve_multiplier,
        venue_fault=venue,
        kill=kill,
    )
    run = run_gates(
        dict(zip(SYMBOLS, target, strict=True)), dict(zip(SYMBOLS, held, strict=True)), ctx
    )
    assert run.logs[-1].gate_name == "final_ceiling"
    assert portfolio_vol(run.weights, SYMBOLS, cov) <= CFG.risk.portfolio_vol_ceiling + EPS
    result = stress_test(run.weights, CFG.stress)
    assert result.worst_single <= CFG.stress.single_factor_limit + EPS
    assert result.joint <= CFG.stress.joint_limit + EPS
