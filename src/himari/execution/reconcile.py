"""Reconciliation (§9.5): broker positions and cash against the ledger after every session with
activity. Tolerance: 0 shares, and CAD 1 of cash per currency (USD at the valuation rate).
Any mismatch sets VENUE_FAULT and raises an alert; that is the caller's job.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

CASH_TOLERANCE_CAD = 1.0


@dataclass(frozen=True)
class Reconciliation:
    ok: bool
    share_diffs: dict[str, int]  # broker minus ledger, only where they differ
    cash_diffs_cad: dict[str, float]  # broker minus ledger, in CAD, only beyond tolerance

    def describe(self) -> str:
        parts = [f"{s} shares {d:+d}" for s, d in sorted(self.share_diffs.items())]
        parts += [f"{c} cash {d:+.2f} CAD" for c, d in sorted(self.cash_diffs_cad.items())]
        return "reconciled" if self.ok else "mismatch: " + "; ".join(parts)


def reconcile(
    broker_positions: Mapping[str, int],
    broker_cash: Mapping[str, Decimal],
    ledger_positions: Mapping[str, int],
    ledger_cash: Mapping[str, Decimal],
    usd_to_cad: float,
    tolerance_cad: float = CASH_TOLERANCE_CAD,
) -> Reconciliation:
    shares = {
        s: broker_positions.get(s, 0) - ledger_positions.get(s, 0)
        for s in sorted(set(broker_positions) | set(ledger_positions))
    }
    shares = {s: d for s, d in shares.items() if d != 0}
    to_cad = {"CAD": 1.0, "USD": usd_to_cad}
    cash: dict[str, float] = {}
    for c in sorted(set(broker_cash) | set(ledger_cash)):
        diff = float(broker_cash.get(c, Decimal(0)) - ledger_cash.get(c, Decimal(0))) * to_cad[c]
        if abs(diff) > tolerance_cad:
            cash[c] = diff
    return Reconciliation(not shares and not cash, shares, cash)
