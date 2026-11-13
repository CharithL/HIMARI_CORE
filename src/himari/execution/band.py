"""Rebalance band (§9.2, amendment v1.6).

Trade a fund only when |target - current| > max(band_abs, band_rel * |current|). The exceptions
that always trade are full exits, daily safety cuts, and the cut on entering HALVED. Cuts by the
other gates follow the band (v1.6); the post-trade check (execution/post_trade.py) then keeps
the portfolio inside its limits. The band decides whether a fund trades at all; a fund it holds
is left out of the order builder, so rounding can never turn "keep the current weight" into an
order.

Reason codes: band, full_exit, halved, safety_cut (trades); inside_band, no_change (no trade).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from himari.core.types import GateLog

FORCED_REASONS = ("halved", "safety_cut")


def in_band(target: float, current: float, band_abs: float, band_rel: float) -> bool:
    """True when the change is too small to trade."""
    return abs(target - current) <= max(band_abs, band_rel * abs(current))


@dataclass(frozen=True)
class BandDecision:
    symbol: str
    current: float
    target: float
    trade: bool
    reason: str


def forced_cuts(
    logs: Sequence[GateLog],
    final: Mapping[str, float],
    current: Mapping[str, float],
    only: Collection[str] | None = None,
) -> set[str]:
    """Funds a gate (from `only`, or any) cut whose final target is below the holding."""
    cut_by_gate = {
        s
        for log in logs
        if only is None or log.gate_name in only
        for s, w in log.weights_out.items()
        if w < log.weights_in.get(s, w)
    }
    return {s for s in cut_by_gate if final.get(s, 0.0) < current.get(s, 0.0)}


def apply_band(
    targets: Mapping[str, float],
    current: Mapping[str, float],
    band_abs: float,
    band_rel: float,
    forced: Mapping[str, str] | None = None,
) -> dict[str, BandDecision]:
    """`forced` maps a fund to halved or safety_cut; such a cut trades inside the band."""
    forced = forced or {}
    for reason in forced.values():
        if reason not in FORCED_REASONS:
            raise ValueError(f"unknown forced reason {reason!r}")
    out: dict[str, BandDecision] = {}
    for sym in sorted(set(targets) | set(current)):
        target, held = targets.get(sym, 0.0), current.get(sym, 0.0)
        if target == held:
            out[sym] = BandDecision(sym, held, target, False, "no_change")
        elif target == 0.0:
            out[sym] = BandDecision(sym, held, target, True, "full_exit")
        elif sym in forced and target < held:
            out[sym] = BandDecision(sym, held, target, True, forced[sym])
        elif in_band(target, held, band_abs, band_rel):
            out[sym] = BandDecision(sym, held, target, False, "inside_band")
        else:
            out[sym] = BandDecision(sym, held, target, True, "band")
    return out
