"""Data-quality checks Q1–Q9 (§6, amendment v1.1). Each check is a pure function.

A FAULT blocks additions to the instrument; a WARN only sets a flag. No check ever
blocks a cut — that is enforced where the result is applied (quality.gate).
"""

from __future__ import annotations

import bisect
import enum
import math
from collections.abc import Sequence, Set
from datetime import date
from itertools import pairwise
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from himari.accounting.indices import (
    action_sessions,
    fx_bridges,
    is_outage,
    session_adjustments,
)
from himari.core.types import Bar, CorporateAction, QualityFlag, SeriesObservation

if TYPE_CHECKING:
    from himari.data.calendars import BocBusinessDays


class Severity(str, enum.Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAULT = "FAULT"


class CheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str
    symbol: str
    severity: Severity
    flag: QualityFlag | None = None
    session_date: date | None = None
    detail: str = ""


def _ok(check: str, symbol: str) -> list[CheckResult]:
    return [CheckResult(check=check, symbol=symbol, severity=Severity.PASS)]


def _result(
    check: str, symbol: str, severity: Severity, flag: QualityFlag, day: date | None, detail: str
) -> CheckResult:
    return CheckResult(
        check=check, symbol=symbol, severity=severity, flag=flag, session_date=day, detail=detail
    )


def check_fresh(symbol: str, bars: Sequence[Bar], last_session: date) -> list[CheckResult]:
    """Q1: the latest bar is the instrument's last completed session."""
    if not bars:
        return [_result("Q1", symbol, Severity.FAULT, QualityFlag.STALE, None, "no bars")]
    latest = max(b.session_date for b in bars)
    if latest == last_session:
        return _ok("Q1", symbol)
    if latest < last_session:
        detail = f"latest bar {latest}, last completed session {last_session}"
        return [_result("Q1", symbol, Severity.FAULT, QualityFlag.STALE, latest, detail)]
    detail = f"bar for {latest}, after the last completed session {last_session}"
    return [_result("Q1", symbol, Severity.FAULT, QualityFlag.INVALID, latest, detail)]


def check_ohlc(symbol: str, bars: Sequence[Bar]) -> list[CheckResult]:
    """Q2: 0 < low <= min(open, close) <= max(open, close) <= high, and volume >= 0."""
    out: list[CheckResult] = []
    for b in bars:
        finite = all(math.isfinite(v) for v in (b.open, b.high, b.low, b.close))
        sane = finite and 0 < b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
        if not sane or b.volume < 0:
            detail = f"O={b.open} H={b.high} L={b.low} C={b.close} V={b.volume}"
            out.append(
                _result("Q2", symbol, Severity.FAULT, QualityFlag.INVALID, b.session_date, detail)
            )
    return out or _ok("Q2", symbol)


def check_order(symbol: str, bars: Sequence[Bar], as_of_date: date) -> list[CheckResult]:
    """Q3: dates strictly increasing, no duplicates, no future dates."""
    out: list[CheckResult] = []
    for prev, cur in pairwise(bars):
        if cur.session_date <= prev.session_date:
            kind = "duplicate" if cur.session_date == prev.session_date else "out of order"
            detail = f"{kind}: {cur.session_date} after {prev.session_date}"
            out.append(
                _result("Q3", symbol, Severity.FAULT, QualityFlag.INVALID, cur.session_date, detail)
            )
    for b in bars:
        if b.session_date > as_of_date:
            detail = f"future date (as of {as_of_date})"
            out.append(
                _result("Q3", symbol, Severity.FAULT, QualityFlag.INVALID, b.session_date, detail)
            )
    return out or _ok("Q3", symbol)


def check_jumps(
    symbol: str,
    bars: Sequence[Bar],
    actions: Sequence[CorporateAction],
    confirmed: Set[date],
    max_jump: float,
) -> list[CheckResult]:
    """Q4: |close/prev_close - 1| > max_jump with no corporate action that session.

    A fault until a person confirms it (himari data confirm); confirmed jumps stay as warnings.
    """
    acted = action_sessions([b.session_date for b in bars], actions)
    out: list[CheckResult] = []
    for prev, cur in pairwise(bars):
        if prev.close <= 0 or cur.session_date in acted:
            continue
        move = cur.close / prev.close - 1.0
        if abs(move) > max_jump:
            day = cur.session_date
            if day in confirmed:
                detail = f"move {move:+.4f}, confirmed by a person"
                out.append(_result("Q4", symbol, Severity.WARN, QualityFlag.ANOMALY, day, detail))
            else:
                detail = f"move {move:+.4f} with no corporate action"
                out.append(_result("Q4", symbol, Severity.FAULT, QualityFlag.ANOMALY, day, detail))
    return out or _ok("Q4", symbol)


def check_actions_reconcile(
    symbol: str, bars: Sequence[Bar], actions: Sequence[CorporateAction], tol: float
) -> list[CheckResult]:
    """Q5 (v1.1): on every session, S·(P_t + D_t)/P_{t-1} matches adj_t/adj_{t-1} within tol."""
    if len(bars) < 2:
        return _ok("Q5", symbol)
    if all(b.adj_close is None for b in bars):
        detail = "source gives no adjusted closes; Q5 cannot run"
        return [_result("Q5", symbol, Severity.FAULT, QualityFlag.ACTION_MISMATCH, None, detail)]
    split, cash = session_adjustments([b.session_date for b in bars], actions)
    out: list[CheckResult] = []
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1], bars[i]
        if prev.close <= 0:
            continue
        a0, a1 = prev.adj_close, cur.adj_close
        if a0 is None or a1 is None or not (a0 > 0 and a1 > 0):
            detail = "no valid adjusted close; cannot reconcile"
            out.append(
                _result(
                    "Q5",
                    symbol,
                    Severity.FAULT,
                    QualityFlag.ACTION_MISMATCH,
                    cur.session_date,
                    detail,
                )
            )
            continue
        ours = split[i] * (cur.close + cash[i]) / prev.close
        vendor = a1 / a0
        if abs(ours / vendor - 1.0) > tol:
            detail = f"our ratio {ours:.6f}, vendor-adjusted ratio {vendor:.6f}"
            out.append(
                _result(
                    "Q5",
                    symbol,
                    Severity.FAULT,
                    QualityFlag.ACTION_MISMATCH,
                    cur.session_date,
                    detail,
                )
            )
    return out or _ok("Q5", symbol)


def check_cross_source(
    symbol: str, primary: Sequence[Bar], crosscheck: Sequence[Bar], tol: float
) -> list[CheckResult]:
    """Q6 (v1.1): closes agree within tol on every shared date.

    A missing cross-check on the latest (decision) bar is a fault; on an older bar it is a
    warning, counted in the weekly report.
    """
    if not primary:
        return _ok("Q6", symbol)
    cross = {b.session_date: b.close for b in crosscheck}
    latest = max(b.session_date for b in primary)
    out: list[CheckResult] = []
    for b in primary:
        c = cross.get(b.session_date)
        day = b.session_date
        if c is None:
            if day == latest:
                detail = "no cross-check for the decision bar"
                out.append(
                    _result("Q6", symbol, Severity.FAULT, QualityFlag.SOURCE_MISMATCH, day, detail)
                )
            else:
                out.append(
                    _result(
                        "Q6",
                        symbol,
                        Severity.WARN,
                        QualityFlag.SOURCE_MISMATCH,
                        day,
                        "no cross-check",
                    )
                )
            continue
        if not c > 0 or abs(b.close / c - 1.0) > tol:
            detail = f"primary {b.close}, cross-check {c}"
            out.append(
                _result("Q6", symbol, Severity.FAULT, QualityFlag.SOURCE_MISMATCH, day, detail)
            )
    return out or _ok("Q6", symbol)


def _latest_on_or_before(obs: Sequence[SeriesObservation], day: date) -> SeriesObservation | None:
    ordered = sorted(obs, key=lambda o: o.obs_date)
    i = bisect.bisect_right([o.obs_date for o in ordered], day) - 1
    return ordered[i] if i >= 0 else None


def check_fx(
    symbol: str, fx: Sequence[SeriesObservation], session: date, max_age_days: int
) -> list[CheckResult]:
    """Q7: FXUSDCAD for the session, or the latest rate at most max_age_days old (FX_FILLED)."""
    latest = _latest_on_or_before(fx, session)
    if latest is None:
        return [_result("Q7", symbol, Severity.FAULT, QualityFlag.FX_STALE, session, "no FX rate")]
    age = (session - latest.obs_date).days
    if age == 0:
        return _ok("Q7", symbol)
    if age <= max_age_days:
        detail = f"using the {latest.obs_date} rate ({age} days old)"
        return [_result("Q7", symbol, Severity.WARN, QualityFlag.FX_FILLED, session, detail)]
    detail = f"latest rate {latest.obs_date} is {age} days old"
    return [_result("Q7", symbol, Severity.FAULT, QualityFlag.FX_STALE, session, detail)]


def check_fx_bridges(
    symbol: str,
    session_dates: Sequence[date],
    fx: Sequence[SeriesObservation],
    max_fill_days: int,
    max_bridge_days: int,
    confirmed: Set[date],
    fx_series: str,
) -> list[CheckResult]:
    """Q7, history part (v1.2): an FX gap longer than the bridge cap is an outage.

    It faults the USD fund until a person confirms it; confirmed outages stay as warnings.
    """
    out: list[CheckResult] = []
    for bridge in fx_bridges(session_dates, fx, max_fill_days):
        days = bridge.calendar_days
        if bridge.to_rate is None or days is None or days <= max_bridge_days:
            continue
        span = f"FX outage {bridge.from_rate} to {bridge.to_rate} ({days} calendar days)"
        if is_outage(bridge, max_bridge_days, confirmed):
            detail = (
                f"{span}; confirm with: himari data confirm {fx_series} {bridge.to_rate}"
                ' --reason "..."'
            )
            severity = Severity.FAULT
        else:
            detail, severity = f"{span}, confirmed by a person", Severity.WARN
        out.append(_result("Q7", symbol, severity, QualityFlag.FX_OUTAGE, bridge.booked_on, detail))
    return out or _ok("Q7", symbol)


def check_cash_rate(
    corra: Sequence[SeriesObservation],
    session: date,
    boc_days: BocBusinessDays,
    max_age_bdays: int,
) -> list[CheckResult]:
    """Q8: CORRA at most max_age_bdays Bank of Canada business days old.

    Older: use the last value and flag RATE_STALE. No value at all: fault (nothing to use).
    """
    latest = _latest_on_or_before(corra, session)
    if latest is None:
        return [_result("Q8", "*", Severity.FAULT, QualityFlag.RATE_STALE, session, "no CORRA")]
    age = boc_days.count_between(latest.obs_date, session)
    if age <= max_age_bdays:
        return _ok("Q8", "*")
    detail = f"using the {latest.obs_date} value ({age} business days old)"
    return [_result("Q8", "*", Severity.WARN, QualityFlag.RATE_STALE, session, detail)]


def check_zero_volume(symbol: str, bars: Sequence[Bar]) -> list[CheckResult]:
    """Q9: a session with zero volume (warning only)."""
    out = [
        _result("Q9", symbol, Severity.WARN, QualityFlag.ZERO_VOLUME, b.session_date, "volume 0")
        for b in bars
        if b.volume == 0
    ]
    return out or _ok("Q9", symbol)
