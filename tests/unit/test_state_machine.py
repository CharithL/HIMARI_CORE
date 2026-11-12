"""Persistence of safety states (rule 9, §8.6-§8.8): restarts, no expiry, manual clears only."""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from himari.core.config import DrawdownConfig
from himari.core.types import DrawdownState
from himari.risk.state_machine import (
    ChainBrokenError,
    DrawdownTracker,
    ResumeRefusedError,
    SafetyStore,
)

T0 = datetime(2026, 1, 5, 23, 30, tzinfo=timezone.utc)
CFG = DrawdownConfig()


def store(tmp_path: Path) -> SafetyStore:
    return SafetyStore(tmp_path / "himari.sqlite", kill_file=tmp_path / "KILL")


class TestKillSwitch:
    def test_survives_restart(self, tmp_path: Path) -> None:
        store(tmp_path).kill("drill", "tester", T0)
        assert store(tmp_path).kill_active()  # a new instance reads the same file

    def test_never_expires(self, tmp_path: Path) -> None:
        store(tmp_path).kill("set long ago", "tester", datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert store(tmp_path).kill_active()

    def test_clear_needs_a_reason(self, tmp_path: Path) -> None:
        s = store(tmp_path)
        s.kill("drill", "tester", T0)
        with pytest.raises(ValueError, match="reason"):
            s.unkill("   ", "tester", T0)
        assert s.kill_active()
        s.unkill("drill over", "tester", T0)
        assert not s.kill_active()

    def test_kill_file(self, tmp_path: Path) -> None:
        s = store(tmp_path)
        (tmp_path / "KILL").write_text("", encoding="utf-8")
        assert s.kill_active()
        event = s.unkill("file removed after review", "tester", T0)
        assert event.detail["kill_file_removed"] is True
        assert not (tmp_path / "KILL").exists() and not s.kill_active()


class TestVenueFault:
    def test_persists_until_cleared(self, tmp_path: Path) -> None:
        store(tmp_path).set_venue_fault("reconciliation mismatch", "system", T0)
        assert store(tmp_path).venue_fault_active()
        store(tmp_path).clear_venue_fault("reconciliation passed", "system", T0)
        assert not store(tmp_path).venue_fault_active()


class TestDrawdownStates:
    def run(self, tmp_path: Path, path: list[tuple[float, bool]]) -> list[DrawdownState]:
        tracker = DrawdownTracker(store(tmp_path), CFG)
        return [
            tracker.update(nav, T0 + timedelta(days=i), weekly).state
            for i, (nav, weekly) in enumerate(path)
        ]

    def test_halved_returns_to_normal_only_at_a_weekly_decision(self, tmp_path: Path) -> None:
        states = self.run(tmp_path, [(100, True), (89.9, False), (92, False), (92, True)])
        N, H = DrawdownState.NORMAL, DrawdownState.HALVED
        assert states == [N, H, H, N]

    def test_boundaries(self, tmp_path: Path) -> None:
        states = self.run(tmp_path, [(100, True), (90, False), (85, False)])
        assert states[1] == DrawdownState.HALVED  # DD = -10% exactly
        assert states[2] == DrawdownState.SUSPENDED  # DD = -15% exactly

    def test_suspended_never_clears_by_itself(self, tmp_path: Path) -> None:
        states = self.run(tmp_path, [(100, True), (84, False), (99, True), (120, True)])
        assert states[1:] == [DrawdownState.SUSPENDED] * 3
        assert DrawdownTracker(store(tmp_path), CFG).state() == DrawdownState.SUSPENDED

    def test_resume_needs_a_reason_and_records_who(self, tmp_path: Path) -> None:
        self.run(tmp_path, [(100, True), (84, False), (95, True)])
        tracker = DrawdownTracker(store(tmp_path), CFG)
        with pytest.raises(ValueError, match="reason"):
            tracker.resume("", "tester", T0)
        status = tracker.resume("losses reviewed", "tester", T0)
        assert status.state == DrawdownState.NORMAL  # DD -5% at resume
        last = store(tmp_path).latest("DRAWDOWN")
        assert last is not None and (last.actor, last.reason) == ("tester", "losses reviewed")

    def test_resume_resets_the_reference_to_current_nav(self, tmp_path: Path) -> None:
        """v1.5: resuming at -16% works and measures later drawdowns from the resume NAV."""
        self.run(tmp_path, [(100, True), (84, False)])
        tracker = DrawdownTracker(store(tmp_path), CFG)
        status = tracker.resume("losses reviewed", "tester", T0)
        assert (status.state, status.hwm, status.drawdown) == (DrawdownState.NORMAL, 84, 0.0)
        record = store(tmp_path).latest("DRAWDOWN")
        assert record is not None
        assert record.detail["old_hwm"] == 100 and record.detail["new_reference"] == 84
        assert record.detail["old_drawdown"] == pytest.approx(-0.16)
        later = tracker.update(80, T0 + timedelta(days=1), weekly_decision=False)
        assert later.state == DrawdownState.NORMAL
        assert later.drawdown == pytest.approx(80 / 84 - 1)

    def test_resume_works_from_an_all_cash_suspended_account(self, tmp_path: Path) -> None:
        """No deadlock: a suspended account that went to cash and never recovers can resume."""
        self.run(tmp_path, [(100, True), (84, False), (84, True), (84, True)])
        status = DrawdownTracker(store(tmp_path), CFG).resume("reviewed", "tester", T0)
        assert status.state == DrawdownState.NORMAL

    def test_resume_refused_below_the_loss_floor(self, tmp_path: Path) -> None:
        self.run(tmp_path, [(100, True), (84, False)])
        tracker = DrawdownTracker(store(tmp_path), CFG)
        with pytest.raises(ResumeRefusedError, match="loss floor"):
            tracker.resume("try", "tester", T0, loss_floor_cad=90.0)
        assert tracker.state() == DrawdownState.SUSPENDED
        assert (
            tracker.resume("try", "tester", T0, loss_floor_cad=80.0).state == DrawdownState.NORMAL
        )


class TestRecordIntegrity:
    def test_append_only(self, tmp_path: Path) -> None:
        s = store(tmp_path)
        s.kill("drill", "tester", T0)
        for sql in ("DELETE FROM safety_events", "UPDATE safety_events SET value = 'OFF'"):
            with (
                closing(sqlite3.connect(s.db_path)) as con,
                pytest.raises(sqlite3.DatabaseError, match="append-only"),
            ):
                con.execute(sql)
        assert s.kill_active()

    def test_chain_detects_an_edit(self, tmp_path: Path) -> None:
        s = store(tmp_path)
        s.kill("drill", "tester", T0)
        s.unkill("drill over", "tester", T0)
        assert s.verify()
        with closing(sqlite3.connect(s.db_path)) as con, con:  # bypass the guard on purpose
            con.execute("DROP TRIGGER safety_events_no_update")
            con.execute("UPDATE safety_events SET reason = 'edited' WHERE seq = 1")
        assert not s.verify()
        with pytest.raises(ChainBrokenError, match="broken"):  # verified at startup
            store(tmp_path)
        assert not SafetyStore(s.db_path, check_chain=False).verify()  # status can still report
