"""Stress gate (§8.5): scale every risky weight so stress losses stay within the limits."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from himari.core.types import GateLog
from himari.risk.stress import stress_test

if TYPE_CHECKING:
    from himari.core.config import StressConfig


def apply_stress_gate(
    weights_in: Mapping[str, float], cfg: StressConfig
) -> tuple[dict[str, float], GateLog]:
    """scale = min(1, single limit / worst single loss, joint limit / joint loss).

    The all-adverse scenario including CAD +5% is logged as a diagnostic, never applied.
    """
    result = stress_test(weights_in, cfg)
    out = {s: w * result.scale for s, w in weights_in.items()}
    log = GateLog(
        gate_name="stress",
        weights_in=dict(weights_in),
        weights_out=out,
        bound=result.scale < 1.0,
        scale_applied=result.scale,
        details={
            "worst_single": result.worst_single,
            "worst_single_scenario": result.worst_single_name,
            "joint": result.joint,
            "diagnostic_all_adverse_including_cad_up": result.all_adverse_including_cad_up,
        },
    )
    return out, log
