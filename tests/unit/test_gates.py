"""Volatility ceiling, daily safety check, event gate, drawdown and fault gates, pipeline order."""

import math
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from himari.core.config import load_config
from himari.core.types import DrawdownState
from himari.gates.ceiling import apply_vol_ceiling, daily_safety_check, portfolio_vol
from himari.gates.events import (
    Event,
    apply_event_gate,
    event_window,
    events_in_window,
    load_events,
    missing_coverage,
)
from himari.gates.pipeline import GateContext, run_gates
from himari.gates.states import apply_drawdown_gate, apply_fault_gate
from himari.quality.gate import InstrumentQuality, QualityReport

CFG = load_config(Path(__file__).parents[2] / "config" / "frozen_v1.yaml")
UTC = timezone.utc
SYMS = ("A", "B")
COV = np.diag([0.2**2, 0.1**2])  # sigma 20% and 10%, uncorrelated


class TestVolCeiling:
    def test_scales_down_to_the_ceiling(self) -> None:
        w = {"A": 0.6, "B": 0.0}  # sigma_p = 12%
        out, log = apply_vol_ceiling(w, SYMS, COV, 0.08)
        assert portfolio_vol(out, SYMS, COV) == pytest.approx(0.08, abs=1e-15)
        assert log.scale_applied == pytest.approx(0.08 / 0.12)

    def test_below_ceiling_untouched(self) -> None:
        w = {"A": 0.2, "B": 0.2}
        out, log = apply_vol_ceiling(w, SYMS, COV, 0.08)
        assert out == w and not log.bound

    def test_unknown_symbol_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="covariance"):
            portfolio_vol({"Z": 0.1}, SYMS, COV)


class TestDailySafetyCheck:
    def test_triggers_above_1_25_times_the_ceiling(self) -> None:
        check = daily_safety_check({"A": 0.51, "B": 0.0}, SYMS, COV, 0.08, 1.25)  # 10.2%
        assert check.triggered
        assert portfolio_vol(check.targets, SYMS, COV) == pytest.approx(0.08, abs=1e-15)

    def test_quiet_at_or_below_the_trigger(self) -> None:
        check = daily_safety_check({"A": 0.50, "B": 0.0}, SYMS, COV, 0.08, 1.25)  # 10.0%
        assert not check.triggered and check.targets == {"A": 0.50, "B": 0.0}


def event(kind: str, when: datetime) -> Event:
    return Event(kind, when, "https://example.org/source", datetime(2026, 1, 1, tzinfo=UTC))


class TestEventCalendar:
    def test_load_and_validate(self, tmp_path: Path) -> None:
        (tmp_path / "events_2025.yaml").write_text(
            "events:\n"
            "  - {type: FOMC, release_utc: '2025-01-29T19:00:00Z',"
            " source_url: 'https://www.federalreserve.gov',"
            " retrieved_utc: '2026-11-12T00:00:00Z'}\n",
            encoding="utf-8",
        )
        (got,) = load_events(tmp_path, CFG.events.map)
        assert got.type == "FOMC" and got.release_utc == datetime(2025, 1, 29, 19, tzinfo=UTC)

    def test_unknown_type_and_naive_times_are_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "events_2025.yaml"
        f.write_text(
            "events:\n  - {type: XYZ, release_utc: '2025-01-29T19:00:00Z',"
            " source_url: u, retrieved_utc: '2026-11-12T00:00:00Z'}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="unknown event type"):
            load_events(tmp_path, CFG.events.map)
        f.write_text(
            "events:\n  - {type: FOMC, release_utc: '2025-01-29T19:00:00',"
            " source_url: u, retrieved_utc: '2026-11-12T00:00:00Z'}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="timezone"):
            load_events(tmp_path, CFG.events.map)

    def test_missing_coverage_names_types_and_years(self) -> None:
        events = [event("FOMC", datetime(2025, 1, 29, 19, tzinfo=UTC))]
        gaps = missing_coverage(events, ["FOMC", "BOC"], [2024, 2025])
        assert gaps == {"FOMC": [2024], "BOC": [2024, 2025]}

    def test_window_from_decision_close_to_execution_close(self) -> None:
        start, ends = event_window(date(2025, 4, 17), "XTSE", {"VCN": "XTSE", "PDBC": "XNAS"})
        assert start == datetime(2025, 4, 17, 20, 0, tzinfo=UTC)  # Thursday close, 16:00 ET
        assert ends["VCN"] == datetime(2025, 4, 21, 20, 0, tzinfo=UTC)  # after Good Friday
        assert ends["PDBC"] == datetime(2025, 4, 21, 20, 0, tzinfo=UTC)

    def test_only_mapped_instruments_inside_the_window(self) -> None:
        start = datetime(2025, 3, 7, 21, tzinfo=UTC)
        close = datetime(2025, 3, 10, 20, tzinfo=UTC)
        ends = {"VUN": close, "VIU": close}
        events = [
            event("US_NFP", datetime(2025, 3, 7, 13, 30, tzinfo=UTC)),  # before the window
            event("US_CPI", datetime(2025, 3, 10, 12, 30, tzinfo=UTC)),  # inside, maps to VUN
            event("ECB", datetime(2025, 3, 11, 13, 15, tzinfo=UTC)),  # after VIU's window
        ]
        hit = events_in_window(events, CFG.events.map, start, ends)
        assert list(hit) == ["VUN"] and hit["VUN"][0].type == "US_CPI"


class TestEventGate:
    def test_no_additions_for_affected_instruments_only(self) -> None:
        pre = {"VUN": [event("FOMC", datetime(2025, 1, 29, 19, tzinfo=UTC))]}
        out, log = apply_event_gate({"VUN": 0.08, "VCN": 0.08}, {"VUN": 0.05, "VCN": 0.0}, pre)
        assert out == {"VUN": 0.05, "VCN": 0.08}
        assert "FOMC" in log.details["VUN"]

    def test_cuts_pass(self) -> None:
        pre = {"VUN": [event("FOMC", datetime(2025, 1, 29, 19, tzinfo=UTC))]}
        out, _ = apply_event_gate({"VUN": 0.02}, {"VUN": 0.05}, pre)
        assert out == {"VUN": 0.02}


class TestStateGates:
    def test_halved(self) -> None:
        out, _ = apply_drawdown_gate({"A": 0.2}, {"A": 0.0}, DrawdownState.HALVED, 0.5)
        assert out == {"A": 0.1}

    def test_suspended_adds_nothing_but_cuts(self) -> None:
        out, _ = apply_drawdown_gate(
            {"A": 0.2, "B": 0.05}, {"A": 0.1, "B": 0.1}, DrawdownState.SUSPENDED, 0.5
        )
        assert out == {"A": 0.1, "B": 0.05}

    def test_venue_fault_blocks_additions(self) -> None:
        out, log = apply_fault_gate({"A": 0.2}, {"A": 0.1}, venue_fault=True, kill=False)
        assert out == {"A": 0.1} and log.details["orders_blocked"] is False

    def test_kill_blocks_orders(self) -> None:
        out, log = apply_fault_gate({"A": 0.2}, {"A": 0.1}, venue_fault=False, kill=True)
        assert out == {"A": 0.1} and log.details["orders_blocked"] is True


def all_ok(symbols: tuple[str, ...]) -> QualityReport:
    return QualityReport(
        instruments={s: InstrumentQuality(symbol=s, status="OK", results=[]) for s in symbols},
        portfolio=[],
    )


class TestPipeline:
    def test_fixed_order_and_kill_flag(self) -> None:
        syms = ("VCN", "VGV")
        ctx = GateContext(
            quality=all_ok(syms),
            symbols=syms,
            covariance=np.diag([0.15**2, 0.06**2]),
            vol_ceiling=0.08,
            pre_event={},
            stress=CFG.stress,
            drawdown_state=DrawdownState.NORMAL,
            halve_multiplier=0.5,
            venue_fault=False,
            kill=True,
        )
        run = run_gates({"VCN": 0.1, "VGV": 0.2}, {"VCN": 0.05, "VGV": 0.25}, ctx)
        names = [log.gate_name for log in run.logs]
        assert names == ["data", "vol_ceiling", "event", "stress", "drawdown", "faults"]
        assert run.orders_blocked
        assert run.weights["VCN"] == 0.05  # no addition under KILL
        assert math.isclose(run.weights["VGV"], 0.2)  # a cut passes through the weights
