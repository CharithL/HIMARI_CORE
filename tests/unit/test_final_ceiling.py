"""The final ceiling re-check and the stress-monotonicity guard (amendment v1.7)."""

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from himari.core.config import BondLoading, load_config
from himari.core.types import DrawdownState
from himari.gates.ceiling import portfolio_vol
from himari.gates.events import Event
from himari.gates.pipeline import GateContext, run_gates
from himari.quality.gate import InstrumentQuality, QualityReport
from himari.risk.stress import gains_in_loss_scenarios

CFG = load_config(Path(__file__).parents[2] / "config" / "frozen_v1.yaml")
UNIVERSE = [i.symbol for i in CFG.stage1.instruments()]


def test_no_fund_gains_in_a_loss_scenario() -> None:
    """With today's loadings a cut can never raise a scenario loss, so the stress gate needs
    no second pass. If a future config breaks this, this test fails."""
    assert gains_in_loss_scenarios(CFG.stress, UNIVERSE) == {}


def test_the_guard_catches_a_mixed_scenario() -> None:
    """A fund that gains when the Canadian dollar rises (while the foreign funds lose)."""
    loadings = {**CFG.stress.loadings, "VCN": {"EQUITY": 1.0, "CAD_UP": 1.0}}
    broken = CFG.stress.model_copy(update={"loadings": loadings})
    assert gains_in_loss_scenarios(broken, UNIVERSE) == {
        "CAD_UP+": ["VCN"],  # VCN gains while the foreign funds lose
        "CAD_UP-": ["CGL.C", "DLR", "PDBC", "VEE", "VIU", "VUN"],  # and the reverse
    }
    bonds = {**CFG.stress.loadings, "VCN": {"CA_YIELD": BondLoading(duration=-2, convexity=0)}}
    assert "CA_YIELD+" in gains_in_loss_scenarios(
        CFG.stress.model_copy(update={"loadings": bonds}), UNIVERSE
    )


def test_capping_a_hedge_is_brought_back_to_the_ceiling() -> None:
    """A (20% vol) and B (10% vol) at correlation -0.5: the ceiling scales the pair to 8%;
    the event gate then caps B at its holding of 0, leaving A alone at 9.2%. The final ceiling
    cuts A to 8%."""
    syms = ("A", "B")
    cov = np.array([[0.04, -0.5 * 0.2 * 0.1], [-0.5 * 0.2 * 0.1, 0.01]])
    release = datetime(2025, 3, 17, 13, 30, tzinfo=timezone.utc)
    ctx = GateContext(
        quality=QualityReport(
            instruments={s: InstrumentQuality(symbol=s, status="OK", results=[]) for s in syms},
            portfolio=[],
        ),
        symbols=syms,
        covariance=cov,
        vol_ceiling=0.08,
        pre_event={"B": [Event("US_CPI", release, "u", release)]},
        stress=CFG.stress,
        drawdown_state=DrawdownState.NORMAL,
        halve_multiplier=0.5,
        venue_fault=False,
        kill=False,
    )
    run = run_gates({"A": 0.5, "B": 0.6}, {"A": 0.0, "B": 0.0}, ctx)
    logs = {log.gate_name: log for log in run.logs}
    assert portfolio_vol(logs["event"].weights_out, syms, cov) == pytest.approx(0.0918, abs=1e-4)
    assert logs["final_ceiling"].bound
    assert portfolio_vol(run.weights, syms, cov) == pytest.approx(0.08)
    assert run.weights == {"A": pytest.approx(0.4), "B": 0.0}
