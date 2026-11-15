"""The event archive and the known-in-advance rule (§8.4, rule 2, amendment v1.7)."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from himari.core.config import load_config
from himari.gates.events import Event, event_window, events_in_window, load_events, missing_coverage

ROOT = Path(__file__).parents[2]
CFG = load_config(ROOT / "config" / "frozen_v1.yaml")
EVENTS = load_events(ROOT / "config" / "events", CFG.events.map.keys())
CODES = {i.symbol: CFG.calendars.by_exchange[i.exchange.value] for i in CFG.stage1.instruments()}


def find(kind: str, day: date) -> Event:
    (e,) = [e for e in EVENTS if e.type == kind and e.release_utc.date() == day]
    return e


@pytest.mark.parametrize(
    ("kind", "day"),
    [
        ("FOMC", date(2020, 3, 3)),
        ("FOMC", date(2020, 3, 15)),
        ("BOC", date(2020, 3, 13)),
        ("BOC", date(2020, 3, 27)),
        ("BOJ", date(2020, 3, 16)),
        ("ECB", date(2020, 3, 18)),
        ("SNB", date(2015, 1, 15)),
    ],
)
def test_unscheduled_announcements_are_kept_and_flagged(kind: str, day: date) -> None:
    e = find(kind, day)
    assert e.unscheduled and not e.known_by(e.release_utc)
    assert e.source_url.startswith("https://") and e.retrieved_utc.tzinfo is not None


def test_the_sunday_fed_cut_is_ignored_by_the_13_march_2020_decision() -> None:
    """Sunday 15 March 2020, 17:00 ET: inside the window from Friday's close to Monday's."""
    cut = find("FOMC", date(2020, 3, 15))
    assert cut.release_utc == datetime(2020, 3, 15, 21, 0, tzinfo=timezone.utc)
    start, ends = event_window(date(2020, 3, 13), CFG.calendars.master, CODES)
    assert start < cut.release_utc <= ends["VUN"]
    assert events_in_window(EVENTS, CFG.events.map, start, ends) == {}
    # flagged as scheduled, the same release would have capped every FOMC-mapped fund
    scheduled = Event(cut.type, cut.release_utc, cut.source_url, cut.retrieved_utc)
    hit = events_in_window([scheduled], CFG.events.map, start, ends)
    assert set(hit) == set(CFG.events.map["FOMC"])


def test_2025_us_releases_as_they_happened() -> None:
    """11 CPI and 11 jobs releases: October's were cancelled; four moved (BLS notices)."""
    for kind, moved in (
        ("US_CPI", [date(2025, 10, 24), date(2025, 12, 18)]),
        ("US_NFP", [date(2025, 11, 20), date(2025, 12, 16)]),
    ):
        year = [e for e in EVENTS if e.type == kind and e.release_utc.year == 2025]
        assert len(year) == 11
        assert sorted(e.release_utc.date() for e in year if e.rescheduled) == moved
        assert all(e.notice_url.startswith("https://www.bls.gov/") for e in year if e.rescheduled)
    sept_cpi = find("US_CPI", date(2025, 10, 24))
    assert sept_cpi.announced_utc is not None and sept_cpi.known_by(
        datetime(2025, 10, 17, 20, tzinfo=timezone.utc)
    )
    nov_jobs = find("US_NFP", date(2025, 12, 16))
    assert nov_jobs.announced_utc is None and not nov_jobs.known_by(nov_jobs.release_utc)


def test_scheduled_coverage_is_complete() -> None:
    assert missing_coverage(EVENTS, CFG.events.map.keys(), range(2015, 2026)) == {}


def test_rescheduled_counts_only_from_its_announcement() -> None:
    when = datetime(2025, 11, 20, 13, 30, tzinfo=timezone.utc)
    e = Event("US_NFP", when, "u", when, rescheduled=True,
              announced_utc=datetime(2025, 11, 14, 15, tzinfo=timezone.utc))  # fmt: skip
    assert not e.known_by(datetime(2025, 11, 13, 21, tzinfo=timezone.utc))
    assert e.known_by(datetime(2025, 11, 14, 21, tzinfo=timezone.utc))
