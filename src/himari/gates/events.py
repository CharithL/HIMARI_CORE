"""Event gate (§8.4): no additions to instruments with a scheduled release in the window.

The window runs from the decision close (XTSE) to the end of each instrument's execution
session. Event calendars live in config/events/events_YYYY.yaml, maintained by hand from the
official sources, one entry per release with type, release_utc, source_url, retrieved_utc and
version.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from himari.core.types import GateLog
from himari.data.calendars import next_session, session_close_utc
from himari.gates.invariants import no_additions


@dataclass(frozen=True)
class Event:
    type: str
    release_utc: datetime
    source_url: str
    retrieved_utc: datetime
    version: int = 1
    time_assumed: bool = False  # only the date was published; the standard time is used


def _utc(value: object) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"event time {value!r} must carry a timezone")
    return dt.astimezone(timezone.utc)


def load_events(directory: Path, known_types: Iterable[str]) -> list[Event]:
    """All events_YYYY.yaml files; an unknown event type is an error, not a skip."""
    known = set(known_types)
    events: list[Event] = []
    for path in sorted(directory.glob("events_*.yaml")):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for e in raw.get("events") or []:
            event = Event(
                type=str(e["type"]),
                release_utc=_utc(e["release_utc"]),
                source_url=str(e["source_url"]),
                retrieved_utc=_utc(e["retrieved_utc"]),
                version=int(e.get("version", 1)),
                time_assumed=bool(e.get("time_assumed", False)),
            )
            if event.type not in known:
                raise ValueError(f"{path}: unknown event type {event.type!r}")
            events.append(event)
    return sorted(events, key=lambda e: (e.release_utc, e.type))


def missing_coverage(
    events: Iterable[Event], types: Iterable[str], years: Iterable[int]
) -> dict[str, list[int]]:
    """Event types with no release in a year; backtest reports must name them."""
    have = {(e.type, e.release_utc.year) for e in events}
    years = list(years)
    out = {t: [y for y in years if (t, y) not in have] for t in types}
    return {t: ys for t, ys in out.items() if ys}


def event_window(
    decision_date: date, master_code: str, exchange_codes: Mapping[str, str]
) -> tuple[datetime, dict[str, datetime]]:
    """Decision close, and the close of each instrument's next session (its execution session)."""
    start = session_close_utc(master_code, decision_date)
    ends = {
        sym: session_close_utc(code, next_session(code, decision_date))
        for sym, code in exchange_codes.items()
    }
    return start, ends


def events_in_window(
    events: Iterable[Event],
    event_map: Mapping[str, Sequence[str]],
    start_utc: datetime,
    end_utc_by_symbol: Mapping[str, datetime],
) -> dict[str, list[Event]]:
    """Instrument -> the mapped events released between the decision close and its window end."""
    out: dict[str, list[Event]] = {}
    for e in events:
        for sym in event_map.get(e.type, ()):
            end = end_utc_by_symbol.get(sym)
            if end is not None and start_utc <= e.release_utc <= end:
                out.setdefault(sym, []).append(e)
    return out


def apply_event_gate(
    weights_in: Mapping[str, float],
    holdings: Mapping[str, float],
    pre_event: Mapping[str, Sequence[Event]],
) -> tuple[dict[str, float], GateLog]:
    """target_i = min(target_i, current_i) for instruments in a pre-event window; cuts pass."""
    out: dict[str, float] = {}
    details: dict[str, str] = {}
    for sym, w in weights_in.items():
        out[sym] = no_additions(w, holdings.get(sym, 0.0)) if sym in pre_event else w
        if out[sym] != w:
            names = ", ".join(f"{e.type} {e.release_utc.isoformat()}" for e in pre_event[sym])
            details[sym] = f"pre-event ({names}): target {w} capped at {out[sym]}"
    log = GateLog(
        gate_name="event",
        weights_in=dict(weights_in),
        weights_out=out,
        bound=bool(details),
        details=details,
    )
    return out, log
