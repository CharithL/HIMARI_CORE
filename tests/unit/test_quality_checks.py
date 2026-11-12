"""Every data-quality check (§6, amendments v1.1 and v1.2): pass and fail cases."""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from himari.core.types import Bar, CorporateAction, QualityFlag, SeriesObservation
from himari.data.calendars import BocBusinessDays, HolidayFile
from himari.quality.checks import (
    CheckResult,
    Severity,
    check_actions_reconcile,
    check_cash_rate,
    check_cross_source,
    check_fresh,
    check_fx,
    check_fx_bridges,
    check_jumps,
    check_ohlc,
    check_order,
    check_zero_volume,
)

D0 = date(2025, 3, 3)


def bar(
    day: date,
    close: float,
    *,
    o: float | None = None,
    h: float | None = None,
    lo: float | None = None,
    v: int = 1000,
    adj: float | None = None,
) -> Bar:
    o = close if o is None else o
    return Bar(
        symbol="X",
        session_date=day,
        open=o,
        high=max(o, close) if h is None else h,
        low=min(o, close) if lo is None else lo,
        close=close,
        volume=v,
        source="t",
        adj_close=adj,
    )


def series(closes: Sequence[float], adj: Sequence[float] | None = None) -> list[Bar]:
    adj_values = list(adj) if adj is not None else [None] * len(closes)
    return [
        bar(D0 + timedelta(days=i), c, adj=a)
        for i, (c, a) in enumerate(zip(closes, adj_values, strict=True))
    ]


def faults(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.severity == Severity.FAULT]


def only_pass(results: list[CheckResult]) -> bool:
    return all(r.severity == Severity.PASS for r in results)


class TestQ1Fresh:
    def test_latest_is_last_completed_session(self) -> None:
        assert only_pass(check_fresh("X", series([1.0, 1.0]), D0 + timedelta(days=1)))

    def test_stale(self) -> None:
        (r,) = check_fresh("X", series([1.0]), D0 + timedelta(days=1))
        assert r.severity == Severity.FAULT and r.flag == QualityFlag.STALE

    def test_bar_for_unfinished_session(self) -> None:
        (r,) = check_fresh("X", series([1.0, 1.0]), D0)
        assert r.severity == Severity.FAULT and r.flag == QualityFlag.INVALID

    def test_no_bars(self) -> None:
        (r,) = check_fresh("X", [], D0)
        assert r.flag == QualityFlag.STALE


class TestQ2Ohlc:
    def test_sane(self) -> None:
        assert only_pass(check_ohlc("X", [bar(D0, 10.0, o=9.9, h=10.2, lo=9.8)]))

    def test_each_violation_faults(self) -> None:
        bad = [
            bar(D0, 10.0, h=9.9),  # high below close
            bar(D0, 10.0, lo=10.1),  # low above close
            bar(D0, 10.0, lo=0.0),  # non-positive low
            bar(D0, 10.0, v=-1),  # negative volume
            bar(D0, 10.0, o=float("nan"), h=10.0, lo=10.0),  # NaN must not slip through min/max
        ]
        for b in bad:
            (r,) = check_ohlc("X", [b])
            assert r.severity == Severity.FAULT and r.flag == QualityFlag.INVALID, b


class TestQ3Order:
    def test_increasing(self) -> None:
        assert only_pass(check_order("X", series([1.0, 1.0]), D0 + timedelta(days=5)))

    def test_duplicate_and_out_of_order(self) -> None:
        dup = [bar(D0, 1.0), bar(D0, 1.0)]
        back = [bar(D0 + timedelta(days=1), 1.0), bar(D0, 1.0)]
        assert "duplicate" in faults(check_order("X", dup, D0))[0].detail
        assert "out of order" in faults(check_order("X", back, D0 + timedelta(days=1)))[0].detail

    def test_future_date(self) -> None:
        (r,) = check_order("X", [bar(D0 + timedelta(days=1), 1.0)], D0)
        assert r.severity == Severity.FAULT and "future" in r.detail


class TestQ4Jumps:
    def test_jump_without_action_faults(self) -> None:
        (r,) = check_jumps("X", series([100.0, 120.0]), [], set(), 0.15)
        assert r.severity == Severity.FAULT and r.flag == QualityFlag.ANOMALY

    def test_threshold_is_strict(self) -> None:
        assert only_pass(check_jumps("X", series([100.0, 115.0]), [], set(), 0.15))

    def test_action_day_is_exempt(self) -> None:
        b = series([100.0, 50.0])
        split = CorporateAction(
            symbol="X", ex_date=b[1].session_date, action_type="split", amount=Decimal(2)
        )
        assert only_pass(check_jumps("X", b, [split], set(), 0.15))

    def test_confirmed_jump_is_a_warning(self) -> None:
        b = series([100.0, 80.0])
        (r,) = check_jumps("X", b, [], {b[1].session_date}, 0.15)
        assert r.severity == Severity.WARN and r.flag == QualityFlag.ANOMALY


class TestQ5ActionsReconcile:
    def test_every_session_matches(self) -> None:
        b = series([100.0, 101.0, 99.0], adj=[50.0, 50.5, 49.5])
        assert only_pass(check_actions_reconcile("X", b, [], 0.001))

    def test_distribution_reconciles_on_ex_date(self) -> None:
        b = series([100.0, 99.0], adj=[99.0, 99.0])  # vendor adjusts the 1.00 distribution
        d = CorporateAction(
            symbol="X", ex_date=b[1].session_date, action_type="cash", amount=Decimal(1)
        )
        assert only_pass(check_actions_reconcile("X", b, [d], 0.001))

    def test_missing_action_caught_on_a_day_without_one(self) -> None:
        """v1.1: the vendor's ratio shows a distribution we have no record of."""
        b = series([100.0, 99.0], adj=[99.0, 99.0])
        (r,) = check_actions_reconcile("X", b, [], 0.001)
        assert r.severity == Severity.FAULT and r.flag == QualityFlag.ACTION_MISMATCH
        assert r.session_date == b[1].session_date

    def test_within_tolerance(self) -> None:
        b = series([100.0, 100.0], adj=[100.0, 100.09])
        assert only_pass(check_actions_reconcile("X", b, [], 0.001))

    def test_source_without_adjusted_closes(self) -> None:
        (r,) = check_actions_reconcile("X", series([1.0, 1.0]), [], 0.001)
        assert r.severity == Severity.FAULT and "cannot run" in r.detail

    def test_one_missing_adjusted_close(self) -> None:
        b = series([1.0, 1.0, 1.0], adj=[1.0, 1.0, 1.0])
        b[2] = b[2].model_copy(update={"adj_close": None})
        (r,) = check_actions_reconcile("X", b, [], 0.001)
        assert r.severity == Severity.FAULT and r.session_date == b[2].session_date


class TestQ6CrossSource:
    def test_agree_within_tolerance(self) -> None:
        assert only_pass(check_cross_source("X", series([100.0]), series([100.4]), 0.005))

    def test_mismatch_faults(self) -> None:
        (r,) = check_cross_source("X", series([100.0]), series([100.6]), 0.005)
        assert r.severity == Severity.FAULT and r.flag == QualityFlag.SOURCE_MISMATCH

    def test_missing_on_decision_bar_faults(self) -> None:
        primary = series([100.0, 100.0])
        (r,) = check_cross_source("X", primary, primary[:1], 0.005)
        assert r.severity == Severity.FAULT and r.session_date == primary[1].session_date

    def test_missing_on_older_bar_is_a_warning(self) -> None:
        primary = series([100.0, 100.0, 100.0])
        results = check_cross_source("X", primary, primary[2:], 0.005)
        assert [r.severity for r in results] == [Severity.WARN, Severity.WARN]
        assert not faults(results)


def fx_obs(day: date, value: float = 1.35) -> SeriesObservation:
    return SeriesObservation(series="FXUSDCAD", obs_date=day, value=value, source="t")


class TestQ7Fx:
    def test_same_day(self) -> None:
        assert only_pass(check_fx("PDBC", [fx_obs(D0)], D0, 3))

    def test_filled_within_three_days(self) -> None:
        (r,) = check_fx("PDBC", [fx_obs(D0)], D0 + timedelta(days=3), 3)
        assert r.severity == Severity.WARN and r.flag == QualityFlag.FX_FILLED

    def test_stale(self) -> None:
        (r,) = check_fx("PDBC", [fx_obs(D0)], D0 + timedelta(days=4), 3)
        assert r.severity == Severity.FAULT and r.flag == QualityFlag.FX_STALE

    def test_none(self) -> None:
        (r,) = check_fx("PDBC", [], D0, 3)
        assert r.severity == Severity.FAULT


class TestQ7FxBridges:
    sessions = [date(2025, 4, 10), date(2025, 4, 21), date(2025, 4, 22)]

    def test_easter_sized_bridge_passes(self) -> None:
        s = [date(2025, 4, 17), date(2025, 4, 21), date(2025, 4, 22)]
        rates = [fx_obs(s[0]), fx_obs(s[2])]
        assert only_pass(check_fx_bridges("PDBC", s, rates, 3, 7, set(), "FXUSDCAD"))

    def test_outage_faults_with_confirm_command(self) -> None:
        rates = [fx_obs(self.sessions[0]), fx_obs(self.sessions[2])]
        (r,) = check_fx_bridges("PDBC", self.sessions, rates, 3, 7, set(), "FXUSDCAD")
        assert r.severity == Severity.FAULT and r.flag == QualityFlag.FX_OUTAGE
        assert "himari data confirm FXUSDCAD 2025-04-22" in r.detail

    def test_confirmed_outage_is_a_warning(self) -> None:
        rates = [fx_obs(self.sessions[0]), fx_obs(self.sessions[2])]
        confirmed = {date(2025, 4, 22)}
        (r,) = check_fx_bridges("PDBC", self.sessions, rates, 3, 7, confirmed, "FXUSDCAD")
        assert r.severity == Severity.WARN


def corra_obs(day: date) -> SeriesObservation:
    return SeriesObservation(series="AVG.INTWO", obs_date=day, value=3.0, source="t")


BOC_2025 = BocBusinessDays(forward=HolidayFile(frozenset({date(2025, 11, 11)}), frozenset({2025})))
NO_HOLIDAYS_2025 = BocBusinessDays(forward=HolidayFile(frozenset(), frozenset({2025})))


class TestQ8CashRate:
    def test_one_day_publication_lag_is_fresh(self) -> None:
        assert only_pass(
            check_cash_rate([corra_obs(date(2025, 11, 6))], date(2025, 11, 7), BOC_2025, 5)
        )

    def test_bank_holiday_not_counted(self) -> None:
        # (Thu Nov 6, Fri Nov 14] holds 5 Bank business days with Tue Nov 11 off, 6 without
        obs = [corra_obs(date(2025, 11, 6))]
        assert only_pass(check_cash_rate(obs, date(2025, 11, 14), BOC_2025, 5))
        (r,) = check_cash_rate(obs, date(2025, 11, 14), NO_HOLIDAYS_2025, 5)
        assert r.severity == Severity.WARN

    def test_stale_uses_last_value_with_flag(self) -> None:
        (r,) = check_cash_rate([corra_obs(date(2025, 11, 6))], date(2025, 11, 17), BOC_2025, 5)
        assert r.severity == Severity.WARN and r.flag == QualityFlag.RATE_STALE

    def test_no_value_faults(self) -> None:
        (r,) = check_cash_rate([], date(2025, 11, 14), BOC_2025, 5)
        assert r.severity == Severity.FAULT


class TestQ9ZeroVolume:
    def test_warning_only(self) -> None:
        (r,) = check_zero_volume("X", [bar(D0, 1.0, v=0)])
        assert r.severity == Severity.WARN and r.flag == QualityFlag.ZERO_VOLUME

    def test_pass(self) -> None:
        assert only_pass(check_zero_volume("X", [bar(D0, 1.0)]))
