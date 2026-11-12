"""Tests for core types."""

from himari.core.types import (
    AssetClass,
    Bar,
    Currency,
    DrawdownState,
    Exchange,
    GateLog,
    Instrument,
    SafetyState,
)


class TestInstrument:
    def test_create_cad_equity(self) -> None:
        inst = Instrument(
            symbol="VCN",
            exchange=Exchange.TSX,
            currency=Currency.CAD,
            **{"class": AssetClass.EQUITY},
        )
        assert inst.symbol == "VCN"
        assert inst.asset_class == AssetClass.EQUITY

    def test_create_usd_commodity(self) -> None:
        inst = Instrument(
            symbol="PDBC",
            exchange=Exchange.NASDAQ,
            currency=Currency.USD,
            **{"class": AssetClass.COMMODITY},
        )
        assert inst.currency == Currency.USD


class TestBar:
    def test_ohlc_valid(self) -> None:
        from datetime import date

        bar = Bar(
            symbol="VCN",
            session_date=date(2025, 1, 2),
            open=40.0,
            high=41.0,
            low=39.5,
            close=40.5,
            volume=100000,
            source="vendor",
        )
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= max(bar.open, bar.close)

    def test_quality_flags_default_empty(self) -> None:
        from datetime import date

        bar = Bar(
            symbol="VCN",
            session_date=date(2025, 1, 2),
            open=40.0,
            high=41.0,
            low=39.5,
            close=40.5,
            volume=100000,
            source="vendor",
        )
        assert bar.quality_flags == []


class TestSafetyStates:
    def test_all_states_defined(self) -> None:
        states = set(SafetyState)
        assert SafetyState.NORMAL in states
        assert SafetyState.SUSPENDED in states
        assert SafetyState.KILL in states

    def test_drawdown_states(self) -> None:
        assert DrawdownState.NORMAL.value == "NORMAL"
        assert DrawdownState.HALVED.value == "HALVED"
        assert DrawdownState.SUSPENDED.value == "SUSPENDED"


class TestGateLog:
    def test_cut_only_invariant(self) -> None:
        """Rule 1: gates only cut — |w_after| <= |w_before|, sign kept or zero."""
        log = GateLog(
            gate_name="test",
            weights_in={"VCN": 0.10, "VUN": 0.05},
            weights_out={"VCN": 0.08, "VUN": 0.05},
            bound=True,
            scale_applied=0.8,
        )
        for sym in log.weights_in:
            w_in = log.weights_in[sym]
            w_out = log.weights_out.get(sym, 0.0)
            assert abs(w_out) <= abs(w_in) + 1e-12
            if w_out != 0:
                assert (w_out > 0) == (w_in > 0)
