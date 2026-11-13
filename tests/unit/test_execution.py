"""Execution: costs (§11), the band (§9.2, v1.6), orders (§9.3), reconciliation (§9.5) and the
post-trade limit check (v1.6)."""

from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from himari.core.config import load_config
from himari.core.types import GateLog
from himari.execution.band import apply_band, forced_cuts, in_band
from himari.execution.costs import CommissionSchedule, load_cost_model
from himari.execution.orders import build_orders, funding_need, usd_surplus
from himari.execution.post_trade import RiskLimits, check_risk, plan_trades
from himari.execution.reconcile import reconcile

ROOT = Path(__file__).parents[2]
CFG = load_config(ROOT / "config" / "frozen_v1.yaml")
COSTS = ROOT / "config" / "costs_v1.yaml"
DEMO = ROOT / "tests" / "fixtures" / "commissions_demo.yaml"
LIMITS = RiskLimits.from_config(CFG)


class TestCosts:
    def test_floor_applies_below_ten_bp(self) -> None:
        c1, c2 = load_cost_model(COSTS, 1.0, DEMO), load_cost_model(COSTS, 2.0, DEMO)
        assert c1.side_bps("VCN") == 10.0  # half spread 3 + impact 1 is under the 10 bp floor
        assert c2.side_bps("VCN") == 20.0

    def test_stress_multiplies_spreads(self) -> None:
        c = load_cost_model(COSTS, 1.0, DEMO)
        assert c.side_bps("VIU", stressed=True) == 26.0  # 5 x 5 + 1
        assert load_cost_model(COSTS, 2.0, DEMO).side_bps("VIU", stressed=True) == 52.0

    def test_pay_up_buying_receive_less_selling(self) -> None:
        c = load_cost_model(COSTS, 1.0, DEMO)
        assert c.fill_price(100.0, "buy", "VCN") == pytest.approx(100.10)
        assert c.fill_price(100.0, "sell", "VCN") == pytest.approx(99.90)

    def test_commission_minimum_and_cap(self) -> None:
        s = CommissionSchedule(per_share=0.01, minimum=1.0, max_fraction=0.005)
        assert s.charge(50, 20.0) == 1.0  # 0.50 per share total, raised to the minimum
        assert s.charge(1000, 1.0) == 5.0  # 10.00 per share total, capped at 0.5% of 1,000
        assert s.charge(0, 20.0) == 0.0

    def test_no_schedule_is_refused(self) -> None:
        with pytest.raises(ValueError, match="never assume zero"):
            load_cost_model(COSTS, 1.0, None)

    def test_reads_the_v16_blocks(self) -> None:
        c = load_cost_model(COSTS, 2.0, DEMO)
        assert (c.participation_cap, c.carry_forward_sessions, c.us_withholding) == (
            0.005,
            3,
            0.15,
        )
        assert c.interest is not None and c.settlement is not None
        assert c.interest.currencies["CAD"].zero_first == 13000
        assert c.interest.currencies["USD"].benchmark_proxy is None


class TestBand:
    def test_boundary_is_inside(self) -> None:
        assert in_band(0.045, 0.040, 0.005, 0.10)  # exactly the 0.005 band: no trade
        assert not in_band(0.0451, 0.040, 0.005, 0.10)

    def test_relative_band_for_large_holdings(self) -> None:
        assert in_band(0.219, 0.20, 0.005, 0.10)  # max(0.005, 0.02)
        assert not in_band(0.221, 0.20, 0.005, 0.10)

    def test_gate_cuts_follow_the_band(self) -> None:
        """v1.6: a 2.5% gate trim of a 20% holding is inside the band, so it does not trade."""
        out = apply_band({"VCN": 0.195}, {"VCN": 0.20}, 0.005, 0.10)
        assert not out["VCN"].trade and out["VCN"].reason == "inside_band"

    def test_exceptions_always_trade(self) -> None:
        held = {"VCN": 0.003, "VUN": 0.20, "VIU": 0.20}
        targets = {"VCN": 0.0, "VUN": 0.195, "VIU": 0.195}
        forced = {"VUN": "halved", "VIU": "safety_cut"}
        out = apply_band(targets, held, 0.005, 0.10, forced)
        assert (out["VCN"].trade, out["VCN"].reason) == (True, "full_exit")
        assert (out["VUN"].trade, out["VUN"].reason) == (True, "halved")
        assert (out["VIU"].trade, out["VIU"].reason) == (True, "safety_cut")

    def test_forcing_never_turns_an_addition_into_a_trade(self) -> None:
        out = apply_band({"VCN": 0.202}, {"VCN": 0.20}, 0.005, 0.10, {"VCN": "halved"})
        assert not out["VCN"].trade

    def test_unknown_forced_reason(self) -> None:
        with pytest.raises(ValueError, match="unknown forced reason"):
            apply_band({"VCN": 0.1}, {"VCN": 0.2}, 0.005, 0.10, {"VCN": "stress"})

    def test_forced_cuts_by_gate(self) -> None:
        logs = [
            GateLog(gate_name="stress", weights_in={"A": 0.2}, weights_out={"A": 0.19}),
            GateLog(gate_name="drawdown", weights_in={"A": 0.19, "B": 0.2},
                    weights_out={"A": 0.095, "B": 0.1}),
        ]  # fmt: skip
        final, held = {"A": 0.095, "B": 0.1}, {"A": 0.2, "B": 0.05}
        assert forced_cuts(logs, final, held) == {"A"}  # B's target is above its holding
        assert forced_cuts(logs[:1], final, held, only=("drawdown",)) == set()


class TestOrders:
    def test_whole_shares_toward_zero(self) -> None:
        orders, skipped = build_orders(
            "D-1", {"VCN": 0.10, "VUN": 0.05}, {"VCN": 100, "VUN": 300},
            {"VCN": 33.33, "VUN": 33.33}, {"CAD": 1.0}, {"VCN": "CAD", "VUN": "CAD"},
            100_000.0, 100.0, reasons={"VCN": "band", "VUN": "band"},
        )  # fmt: skip
        assert [(o.symbol, o.side, o.shares) for o in orders] == [
            ("VCN", "buy", 200),  # trunc(10,000 / 33.33) = 300
            ("VUN", "sell", 150),  # trunc(5,000 / 33.33) = 150
        ]
        assert not skipped

    def test_small_orders_are_skipped_and_logged(self) -> None:
        orders, skipped = build_orders(
            "D-1", {"VCN": 0.101}, {"VCN": 300}, {"VCN": 33.33}, {"CAD": 1.0}, {"VCN": "CAD"},
            100_000.0, 100.0, reasons={"VCN": "band"},
        )  # fmt: skip
        assert not orders
        assert skipped[0].shares == 3 and skipped[0].reason == "below 100 CAD"

    def test_usd_funds_price_in_cad_and_carry_reasons_and_gates(self) -> None:
        orders, _ = build_orders(
            "D-1", {"PDBC": 0.19}, {}, {"PDBC": 14.0}, {"CAD": 1.0, "USD": 1.35},
            {"PDBC": "USD"}, 100_000.0, 100.0, reasons={"PDBC": "band"},
            gates={"PDBC": [("stress", 0.95), ("post_trade_buys", 0.5)]},
        )  # fmt: skip
        (o,) = orders
        assert o.shares == 1005 and o.reference_price_cad == pytest.approx(18.9)  # 1005.29
        assert o.reason == "band" and o.gates_text() == "stress x0.950; post_trade_buys x0.500"
        assert o.idempotency_key == "D-1:PDBC"

    def test_funding_and_surplus(self) -> None:
        assert funding_need(10_000.0, 2_000.0, 0.01) == pytest.approx(8_100.0)
        assert funding_need(1_000.0, 2_000.0, 0.01) == 0.0
        assert usd_surplus(2_000.0, 0.0, 1.35, 100_000.0, 0.02) == 2_000.0  # 2,700 CAD > 2,000
        assert usd_surplus(1_400.0, 0.0, 1.35, 100_000.0, 0.02) == 0.0  # 1,890 CAD


class TestReconcile:
    def test_within_tolerance(self) -> None:
        rec = reconcile({"VCN": 10}, {"CAD": Decimal("100.50")}, {"VCN": 10},
                        {"CAD": Decimal("100.00")}, 1.35)  # fmt: skip
        assert rec.ok and rec.describe() == "reconciled"

    def test_share_and_usd_cash_mismatches(self) -> None:
        rec = reconcile({"VCN": 10}, {"USD": Decimal("10.00")}, {"VCN": 9},
                        {"USD": Decimal("9.00")}, 1.35)  # fmt: skip
        assert not rec.ok
        assert rec.share_diffs == {"VCN": 1}
        assert rec.cash_diffs_cad == {"USD": pytest.approx(1.35)}  # 1 USD is over CAD 1


SYMS = tuple(i.symbol for i in CFG.stage1.instruments())
SIGMA = {"VCN": 0.13, "VUN": 0.15, "VIU": 0.14, "VEE": 0.16, "VGV": 0.06, "CGL.C": 0.15,
         "PDBC": 0.18, "DLR": 0.06}  # fmt: skip
COV = np.diag([SIGMA[s] ** 2 for s in SYMS])


def plan(held: dict[str, float], gated: dict[str, float]):  # type: ignore[no-untyped-def]
    band = apply_band(gated, held, 0.005, 0.10)
    return plan_trades(held, gated, band, SYMS, COV, CFG.stress, LIMITS)


class TestPostTrade:
    """Equity weight E loses 7% in the single EQUITY- scenario: the limit is 3%, so with the
    10% tolerance E may reach 0.4714 after trading."""

    def test_check_uses_the_tolerance(self) -> None:
        assert check_risk({"VCN": 0.47}, SYMS, COV, CFG.stress, LIMITS).fits  # 3.29%
        assert not check_risk({"VCN": 0.48}, SYMS, COV, CFG.stress, LIMITS).fits  # 3.36%

    def test_a_fitting_plan_is_left_alone(self) -> None:
        p = plan({"VCN": 0.30}, {"VCN": 0.30, "VUN": 0.10})
        assert p.weights["VUN"] == 0.10 and p.buy_scale == 1.0 and not p.fallback

    def test_buys_shrink_pro_rata_before_any_cut(self) -> None:
        p = plan({"VCN": 0.40}, {"VCN": 0.40, "VUN": 0.10})  # E would be 0.50: 3.5%
        assert not p.banded.fits and p.final.fits and not p.fallback
        assert p.buy_scale == pytest.approx(0.714, abs=0.001)  # VUN 0.0714: E = 0.4714
        assert p.weights["VCN"] == 0.40  # the holding is not touched
        assert p.reasons == {"VUN": "band"}

    def test_fallback_trades_every_fund_to_its_gated_target(self) -> None:
        """Holding 0.48 equity fails even with no buys; its gated 0.44 is inside the band."""
        p = plan({"VCN": 0.48}, {"VCN": 0.44, "VUN": 0.02})
        assert p.fallback and p.final.fits
        assert p.weights == {"VCN": 0.44, "VUN": 0.02}
        assert p.reasons == {"VCN": "post_trade_limit", "VUN": "band"}

    def test_shrinking_to_zero_drops_the_buy(self) -> None:
        p = plan({"VCN": 0.4714}, {"VCN": 0.4714, "VUN": 0.05})  # the holding is at the limit
        assert p.buy_scale == 0.0 and not p.fallback
        assert p.weights["VUN"] == 0.0 and "VUN" not in p.reasons


class TestReportAxis:
    def test_ticks_cover_the_data(self) -> None:
        from himari.sim.report import _fmt_axis, _nice_ticks

        ticks = _nice_ticks(99_029.0, 113_102.0)
        assert ticks[0] <= 99_029.0 and ticks[-1] >= 113_102.0
        assert ticks == [95_000, 100_000, 105_000, 110_000, 115_000]
        assert _fmt_axis(102_500, 2_500) == "102.5K" and _fmt_axis(105_000, 5_000) == "105K"
