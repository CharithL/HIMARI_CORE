"""The gates in their fixed order (§8.1), with rule 1 checked after every gate."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

from himari.gates.ceiling import apply_vol_ceiling
from himari.gates.events import Event, apply_event_gate
from himari.gates.invariants import check_cut_only
from himari.gates.states import apply_drawdown_gate, apply_fault_gate
from himari.gates.stress import apply_stress_gate
from himari.quality.gate import QualityReport, apply_data_gate

if TYPE_CHECKING:
    from himari.core.config import StressConfig
    from himari.core.types import DrawdownState, GateLog

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class GateContext:
    quality: QualityReport
    symbols: tuple[str, ...]  # order of the covariance matrix
    covariance: FloatArray
    vol_ceiling: float
    pre_event: Mapping[str, Sequence[Event]]
    stress: StressConfig
    drawdown_state: DrawdownState
    halve_multiplier: float
    venue_fault: bool
    kill: bool


@dataclass(frozen=True)
class GateRun:
    weights: dict[str, float]
    logs: list[GateLog]
    orders_blocked: bool  # KILL: the order stage sends nothing, cuts included


def run_gates(
    weights_in: Mapping[str, float], holdings: Mapping[str, float], ctx: GateContext
) -> GateRun:
    """data -> volatility ceiling -> event -> stress -> drawdown -> faults and kill switch."""
    w = dict(weights_in)
    logs: list[GateLog] = []
    steps: list[Callable[[dict[str, float]], tuple[dict[str, float], GateLog]]] = [
        lambda x: apply_data_gate(x, holdings, ctx.quality),
        lambda x: apply_vol_ceiling(x, ctx.symbols, ctx.covariance, ctx.vol_ceiling),
        lambda x: apply_event_gate(x, holdings, ctx.pre_event),
        lambda x: apply_stress_gate(x, ctx.stress),
        lambda x: apply_drawdown_gate(x, holdings, ctx.drawdown_state, ctx.halve_multiplier),
        lambda x: apply_fault_gate(x, holdings, ctx.venue_fault, ctx.kill),
    ]
    for step in steps:
        out, log = step(w)
        check_cut_only(log.gate_name, w, out)
        logs.append(log)
        w = out
    return GateRun(w, logs, ctx.kill)
