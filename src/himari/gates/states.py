"""Drawdown-state gate (§8.6) and the faults and kill-switch gate (§8.7)."""

from __future__ import annotations

from collections.abc import Mapping

from himari.core.types import DrawdownState, GateLog
from himari.gates.invariants import no_additions


def apply_drawdown_gate(
    weights_in: Mapping[str, float],
    holdings: Mapping[str, float],
    state: DrawdownState,
    halve_multiplier: float,
) -> tuple[dict[str, float], GateLog]:
    """NORMAL passes; HALVED multiplies every target by halve_multiplier; SUSPENDED adds nothing."""
    if state == DrawdownState.HALVED:
        out = {s: w * halve_multiplier for s, w in weights_in.items()}
    elif state == DrawdownState.SUSPENDED:
        out = {s: no_additions(w, holdings.get(s, 0.0)) for s, w in weights_in.items()}
    else:
        out = dict(weights_in)
    log = GateLog(
        gate_name="drawdown",
        weights_in=dict(weights_in),
        weights_out=out,
        bound=out != dict(weights_in),
        scale_applied=halve_multiplier if state == DrawdownState.HALVED else 1.0,
        details={"state": state.value},
    )
    return out, log


def apply_fault_gate(
    weights_in: Mapping[str, float],
    holdings: Mapping[str, float],
    venue_fault: bool,
    kill: bool,
) -> tuple[dict[str, float], GateLog]:
    """VENUE_FAULT or KILL: no additions anywhere.

    KILL also sets orders_blocked: the order stage then sends nothing at all, cuts included,
    and cancels open orders. Positions are never liquidated by this gate.
    """
    if venue_fault or kill:
        out = {s: no_additions(w, holdings.get(s, 0.0)) for s, w in weights_in.items()}
    else:
        out = dict(weights_in)
    log = GateLog(
        gate_name="faults",
        weights_in=dict(weights_in),
        weights_out=out,
        bound=out != dict(weights_in),
        details={"venue_fault": venue_fault, "kill": kill, "orders_blocked": kill},
    )
    return out, log
