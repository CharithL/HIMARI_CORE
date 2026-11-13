"""Identifier generation for decisions, orders, and trials."""

from __future__ import annotations

import uuid
from datetime import date


def decision_id(decision_date: date, sequence: int = 0) -> str:
    return f"D-{decision_date.isoformat()}-{sequence:03d}"


def safety_id(check_date: date, sequence: int = 0) -> str:
    """Daily safety reductions (§8.3) get their own ids, so their keys never collide with a
    weekly decision's."""
    return f"S-{check_date.isoformat()}-{sequence:03d}"


def order_idempotency_key(decision_id: str, symbol: str) -> str:
    return f"{decision_id}:{symbol}"


def trial_id() -> str:
    return f"T-{uuid.uuid4().hex[:12]}"


def run_id() -> str:
    return f"R-{uuid.uuid4().hex[:12]}"
