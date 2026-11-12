"""Anomalies a person has accepted with `himari data confirm` (Q4). Append-only, on disk."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path


class ConfirmationStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, symbol: str, session_date: date, reason: str, confirmed_at_utc: datetime) -> None:
        if not reason.strip():
            raise ValueError("a reason is required to confirm an anomaly")
        record = {
            "symbol": symbol,
            "session_date": session_date.isoformat(),
            "reason": reason.strip(),
            "confirmed_at_utc": confirmed_at_utc.isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    def confirmed(self) -> dict[str, set[date]]:
        out: dict[str, set[date]] = {}
        if not self.path.exists():
            return out
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    out.setdefault(rec["symbol"], set()).add(
                        date.fromisoformat(rec["session_date"])
                    )
        return out
