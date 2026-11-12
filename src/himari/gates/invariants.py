"""Rule 1: a gate only cuts. |w_after| <= |w_before| and the sign is kept or becomes zero."""

from __future__ import annotations

from collections.abc import Mapping

TOLERANCE = 1e-12


class CutOnlyError(RuntimeError):
    pass


def check_cut_only(gate: str, before: Mapping[str, float], after: Mapping[str, float]) -> None:
    """Raise CutOnlyError if the gate opened, added to or flipped any position."""
    for symbol in set(before) | set(after):
        w_in = before.get(symbol, 0.0)
        w_out = after.get(symbol, 0.0)
        if abs(w_out) > abs(w_in) + TOLERANCE:
            raise CutOnlyError(f"{gate}: {symbol} grew from {w_in} to {w_out}")
        if w_out != 0.0 and (w_out > 0) != (w_in > 0):
            raise CutOnlyError(f"{gate}: {symbol} flipped from {w_in} to {w_out}")
