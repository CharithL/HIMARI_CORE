"""Persistent safety states (§8.6-§8.8): kill switch, venue fault and drawdown state.

Every change is a record in an append-only SQLite table (UPDATE and DELETE are refused by
triggers), chained by SHA-256 so any edit to the history is detectable. A state is the latest
record for its key; nothing expires, and SUSPENDED and KILL clear only by a manual command
with a reason (rule 9).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from himari.core.types import DrawdownState

if TYPE_CHECKING:
    from himari.core.config import DrawdownConfig

KILL = "KILL"
VENUE_FAULT = "VENUE_FAULT"
DRAWDOWN = "DRAWDOWN"
GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS safety_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at_utc TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    reason TEXT NOT NULL, actor TEXT NOT NULL, detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nav_marks (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at_utc TEXT NOT NULL, nav REAL NOT NULL, hwm REAL NOT NULL, drawdown REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS safety_events_no_update BEFORE UPDATE ON safety_events
BEGIN SELECT RAISE(ABORT, 'safety_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS safety_events_no_delete BEFORE DELETE ON safety_events
BEGIN SELECT RAISE(ABORT, 'safety_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS nav_marks_no_update BEFORE UPDATE ON nav_marks
BEGIN SELECT RAISE(ABORT, 'nav_marks is append-only'); END;
CREATE TRIGGER IF NOT EXISTS nav_marks_no_delete BEFORE DELETE ON nav_marks
BEGIN SELECT RAISE(ABORT, 'nav_marks is append-only'); END;
"""


@dataclass(frozen=True)
class SafetyEvent:
    seq: int
    at_utc: str
    key: str
    value: str
    reason: str
    actor: str
    detail: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class NavMark:
    at_utc: str
    nav: float
    hwm: float
    drawdown: float


def _digest(prev_hash: str, fields: dict[str, Any]) -> str:
    payload = prev_hash + json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_reason(reason: str) -> str:
    if not reason.strip():
        raise ValueError("a reason is required")
    return reason.strip()


class SafetyStore:
    def __init__(self, db_path: Path, kill_file: Path | None = None) -> None:
        self.db_path = db_path
        self.kill_file = kill_file
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con, con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def record(
        self,
        key: str,
        value: str,
        reason: str,
        actor: str,
        at_utc: datetime,
        detail: dict[str, Any] | None = None,
    ) -> SafetyEvent:
        fields: dict[str, Any] = {
            "at_utc": at_utc.isoformat(),
            "key": key,
            "value": value,
            "reason": reason,
            "actor": actor,
            "detail": detail or {},
        }
        with closing(self._connect()) as con, con:
            row = con.execute("SELECT hash FROM safety_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS
            digest = _digest(prev, fields)
            cur = con.execute(
                "INSERT INTO safety_events (at_utc, key, value, reason, actor, detail, prev_hash,"
                " hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fields["at_utc"],
                    key,
                    value,
                    reason,
                    actor,
                    json.dumps(fields["detail"], sort_keys=True),
                    prev,
                    digest,
                ),
            )
            seq = int(cur.lastrowid or 0)
        return SafetyEvent(
            seq, fields["at_utc"], key, value, reason, actor, fields["detail"], prev, digest
        )

    def events(self, key: str | None = None) -> list[SafetyEvent]:
        sql = (
            "SELECT seq, at_utc, key, value, reason, actor, detail, prev_hash, hash"
            " FROM safety_events"
        )
        args: tuple[str, ...] = ()
        if key is not None:
            sql += " WHERE key = ?"
            args = (key,)
        with closing(self._connect()) as con:
            rows = con.execute(sql + " ORDER BY seq", args).fetchall()
        return [
            SafetyEvent(r[0], r[1], r[2], r[3], r[4], r[5], json.loads(r[6]), r[7], r[8])
            for r in rows
        ]

    def latest(self, key: str) -> SafetyEvent | None:
        found = self.events(key)
        return found[-1] if found else None

    def verify(self) -> bool:
        """True when every record's hash matches its content and the previous record."""
        prev = GENESIS
        for e in self.events():
            fields = {
                "at_utc": e.at_utc,
                "key": e.key,
                "value": e.value,
                "reason": e.reason,
                "actor": e.actor,
                "detail": e.detail,
            }
            if e.prev_hash != prev or e.hash != _digest(prev, fields):
                return False
            prev = e.hash
        return True

    # kill switch: `himari kill` or a KILL file in state/
    def kill_active(self) -> bool:
        latest = self.latest(KILL)
        file_present = self.kill_file is not None and self.kill_file.exists()
        return file_present or (latest is not None and latest.value == "ON")

    def kill(self, reason: str, actor: str, at_utc: datetime) -> SafetyEvent:
        return self.record(KILL, "ON", _require_reason(reason), actor, at_utc)

    def unkill(self, reason: str, actor: str, at_utc: datetime) -> SafetyEvent:
        reason = _require_reason(reason)
        removed = self.kill_file is not None and self.kill_file.exists()
        if removed and self.kill_file is not None:
            self.kill_file.unlink()
        return self.record(KILL, "OFF", reason, actor, at_utc, {"kill_file_removed": removed})

    # venue fault: set by reconciliation or broker problems, cleared when reconciliation passes
    def venue_fault_active(self) -> bool:
        latest = self.latest(VENUE_FAULT)
        return latest is not None and latest.value == "ON"

    def set_venue_fault(self, reason: str, actor: str, at_utc: datetime) -> SafetyEvent:
        return self.record(VENUE_FAULT, "ON", _require_reason(reason), actor, at_utc)

    def clear_venue_fault(self, reason: str, actor: str, at_utc: datetime) -> SafetyEvent:
        return self.record(VENUE_FAULT, "OFF", _require_reason(reason), actor, at_utc)

    # NAV marks for the drawdown state
    def add_nav_mark(self, mark: NavMark) -> None:
        with closing(self._connect()) as con, con:
            con.execute(
                "INSERT INTO nav_marks (at_utc, nav, hwm, drawdown) VALUES (?, ?, ?, ?)",
                (mark.at_utc, mark.nav, mark.hwm, mark.drawdown),
            )

    def last_nav_mark(self) -> NavMark | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT at_utc, nav, hwm, drawdown FROM nav_marks ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return None if row is None else NavMark(row[0], row[1], row[2], row[3])


THRESHOLD_TOLERANCE = 1e-12


def _at_or_below(drawdown: float, threshold: float) -> bool:
    """A drawdown within 1e-12 of a threshold counts as reaching it: 90/100 - 1 is
    -0.09999999999999998 in floating point, and must still count as -10%."""
    return drawdown <= threshold + THRESHOLD_TOLERANCE


class ResumeRefusedError(RuntimeError):
    pass


@dataclass(frozen=True)
class DrawdownStatus:
    state: DrawdownState
    nav: float | None
    hwm: float | None
    drawdown: float


class DrawdownTracker:
    """NORMAL / HALVED / SUSPENDED from NAV against its high-water mark (§8.6)."""

    def __init__(self, store: SafetyStore, cfg: DrawdownConfig) -> None:
        self.store = store
        self.cfg = cfg

    def state(self) -> DrawdownState:
        latest = self.store.latest(DRAWDOWN)
        return DrawdownState.NORMAL if latest is None else DrawdownState(latest.value)

    def status(self) -> DrawdownStatus:
        mark = self.store.last_nav_mark()
        if mark is None:
            return DrawdownStatus(self.state(), None, None, 0.0)
        return DrawdownStatus(self.state(), mark.nav, mark.hwm, mark.drawdown)

    def update(self, nav: float, at_utc: datetime, weekly_decision: bool) -> DrawdownStatus:
        """Record NAV (CAD, after costs) and move between states.

        HALVED returns to NORMAL only at a weekly decision; SUSPENDED never clears here.
        """
        if not nav > 0:
            raise ValueError("NAV must be positive")
        last = self.store.last_nav_mark()
        hwm = nav if last is None else max(last.hwm, nav)
        dd = nav / hwm - 1.0
        state = self.state()
        if state == DrawdownState.SUSPENDED or _at_or_below(dd, self.cfg.suspend_at):
            new = DrawdownState.SUSPENDED
        elif _at_or_below(dd, self.cfg.halve_at) or (
            state == DrawdownState.HALVED and not weekly_decision
        ):
            new = DrawdownState.HALVED
        else:
            new = DrawdownState.NORMAL
        self.store.add_nav_mark(NavMark(at_utc.isoformat(), nav, hwm, dd))
        if new != state:
            detail = {"nav": nav, "hwm": hwm, "drawdown": dd, "from": state.value}
            self.store.record(DRAWDOWN, new.value, f"drawdown {dd:.2%}", "system", at_utc, detail)
        return DrawdownStatus(new, nav, hwm, dd)

    def resume(self, reason: str, actor: str, at_utc: datetime) -> DrawdownStatus:
        """Leave SUSPENDED (manual, with a reason). Refused while the drawdown is still at or
        below the suspension level, because the next update would suspend again."""
        reason = _require_reason(reason)
        if self.state() != DrawdownState.SUSPENDED:
            raise ResumeRefusedError("the drawdown state is not SUSPENDED")
        status = self.status()
        if _at_or_below(status.drawdown, self.cfg.suspend_at):
            raise ResumeRefusedError(
                f"drawdown is {status.drawdown:.2%}, at or below {self.cfg.suspend_at:.0%}; "
                "resuming now would suspend again at the next update"
            )
        halved = _at_or_below(status.drawdown, self.cfg.halve_at)
        new = DrawdownState.HALVED if halved else DrawdownState.NORMAL
        detail = {"nav": status.nav, "hwm": status.hwm, "drawdown": status.drawdown}
        self.store.record(DRAWDOWN, new.value, reason, actor, at_utc, detail)
        return DrawdownStatus(new, status.nav, status.hwm, status.drawdown)
