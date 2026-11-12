"""Rule 1 across the whole gate pipeline (§8.1): every gate only cuts, whatever the state."""

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from himari.core.config import load_config
from himari.core.types import DrawdownState
from himari.gates.events import Event
from himari.gates.invariants import check_cut_only
from himari.gates.pipeline import GateContext, run_gates
from himari.quality.gate import InstrumentQuality, QualityReport

CFG = load_config(Path(__file__).parents[2] / "config" / "frozen_v1.yaml")
SYMBOLS = tuple(i.symbol for i in CFG.stage1.instruments())
N = len(SYMBOLS)
FOMC = Event("FOMC", datetime(2025, 1, 29, 19, tzinfo=timezone.utc), "u", datetime(2026, 1, 1))

weights = st.lists(st.floats(0.0, 0.25, allow_nan=False), min_size=N, max_size=N)
flags = st.lists(st.booleans(), min_size=N, max_size=N)


@st.composite
def covariance(draw: st.DrawFn) -> np.ndarray:
    loadings = np.array(
        draw(st.lists(st.floats(-1.0, 1.0, allow_nan=False), min_size=2 * N, max_size=2 * N))
    ).reshape(N, 2)
    corr = loadings @ loadings.T + 0.1 * np.eye(N)
    d = np.sqrt(np.diag(corr))
    corr = corr / np.outer(d, d)
    sig = np.array(draw(st.lists(st.floats(0.02, 0.6), min_size=N, max_size=N)))
    return np.diag(sig) @ corr @ np.diag(sig)


@settings(max_examples=300, deadline=None)
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
def test_every_gate_only_cuts(
    target: list[float],
    held: list[float],
    cov: np.ndarray,
    faulted: list[bool],
    pre_event: list[bool],
    dd_state: DrawdownState,
    venue: bool,
    kill: bool,
) -> None:
    w_in = dict(zip(SYMBOLS, target, strict=True))
    holdings = dict(zip(SYMBOLS, held, strict=True))
    quality = QualityReport(
        instruments={
            s: InstrumentQuality(symbol=s, status="FAULT" if f else "OK", results=[])
            for s, f in zip(SYMBOLS, faulted, strict=True)
        },
        portfolio=[],
    )
    ctx = GateContext(
        quality=quality,
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
    run = run_gates(w_in, holdings, ctx)  # raises CutOnlyError on any violation

    for log in run.logs:
        check_cut_only(log.gate_name, log.weights_in, log.weights_out)
    blocked_everywhere = venue or kill or dd_state == DrawdownState.SUSPENDED
    for i, s in enumerate(SYMBOLS):
        assert 0.0 <= run.weights[s] <= w_in[s] + 1e-15
        if blocked_everywhere or faulted[i] or pre_event[i]:
            assert run.weights[s] <= holdings[s] + 1e-15  # no additions; cuts still allowed
    assert run.orders_blocked == kill
