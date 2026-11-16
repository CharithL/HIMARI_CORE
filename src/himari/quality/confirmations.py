"""Data issues a person has accepted with `himari data confirm`. Append-only, on disk.

Each record names the check it answers: Q4 (a price anomaly), Q7 (an FX outage, keyed by the
FX series name) or Q10 (a December distribution's cash/reinvested split, v1.6). Records written
before v1.6 have no check and count as Q4/Q7. A confirmation only clears its own check.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

CHECKS = ("Q4", "Q7", "Q10")
_LEGACY = ("Q4", "Q7")  # what a record without a check field answers


class ConfirmationStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def add(
        self,
        symbol: str,
        session_date: date,
        reason: str,
        confirmed_at_utc: datetime,
        check: str = "Q4",
    ) -> None:
        self.add_many([(symbol, session_date, reason)], confirmed_at_utc, check)

    def add_many(
        self,
        items: Sequence[tuple[str, date, str]],
        confirmed_at_utc: datetime,
        check: str,
    ) -> None:
        """(symbol, date, reason) confirmations of one check, all validated first and then
        appended in a single write (v1.11, `issuer-check --confirm-matches`)."""
        if check not in CHECKS:
            raise ValueError(f"check must be one of {', '.join(CHECKS)}")
        lines = []
        for symbol, session_date, reason in items:
            if not reason.strip():
                raise ValueError("a reason is required to confirm an anomaly")
            record = {
                "symbol": symbol,
                "session_date": session_date.isoformat(),
                "reason": reason.strip(),
                "confirmed_at_utc": confirmed_at_utc.isoformat(),
                "check": check,
            }
            lines.append(json.dumps(record, sort_keys=True) + "\n")
        if not lines:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("".join(lines))

    def confirmed(self, *checks: str) -> dict[str, set[date]]:
        """Symbol -> confirmed dates for the given checks (default: Q4 and Q7)."""
        wanted = set(checks or _LEGACY)
        out: dict[str, set[date]] = {}
        if not self.path.exists():
            return out
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                answers = {rec["check"]} if "check" in rec else set(_LEGACY)
                if answers & wanted:
                    out.setdefault(rec["symbol"], set()).add(
                        date.fromisoformat(rec["session_date"])
                    )
        return out
