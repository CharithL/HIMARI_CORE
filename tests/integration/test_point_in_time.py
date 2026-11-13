"""The backtest's point-in-time data (rule 2): epochs for corrections and late rows."""

from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from himari.core.config import load_config
from himari.core.types import Bar
from himari.data.calendars import release_utc
from himari.data.storage import BARS, VersionedStore, bar_row
from himari.sim.market import PointInTimeData
from himari.sim.run import ingest_demo_fixtures

ROOT = Path(__file__).parents[2]
CFG = load_config(ROOT / "config" / "frozen_v1.yaml")
EOD = time.fromisoformat(CFG.schedule.daily_job_after_et)
DAY = date(2025, 3, 3)
FIXED_AT = datetime(2025, 3, 10, 15, tzinfo=timezone.utc)  # the correction arrives a week later


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> VersionedStore:
    s = ingest_demo_fixtures(CFG, ROOT, tmp_path_factory.mktemp("pit"))
    v1 = next(b for b in s.bars("csv")["VCN"] if b.session_date == DAY)
    corrected = Bar(**{**v1.model_dump(), "close": round(v1.close * 1.02, 2), "version": 1})
    s.append(BARS, [bar_row(corrected, v1.released_at_utc)], FIXED_AT)  # type: ignore[arg-type]
    return s


def test_clean_fixtures_have_one_epoch(tmp_path: Path) -> None:
    """Nothing is late in the clean set: CORRA's next-day publication is handled by the cash
    index's own cutoffs, so it does not start epochs."""
    clean = ingest_demo_fixtures(CFG, ROOT, tmp_path)
    assert PointInTimeData(clean, CFG, "csv", "xcheck").late_times == []


def test_a_correction_is_seen_only_from_its_release(store: VersionedStore) -> None:
    pit = PointInTimeData(store, CFG, "csv")
    assert pit.late_times == [FIXED_AT]
    before = release_utc(date(2025, 3, 7), EOD)
    after = release_utc(date(2025, 3, 10), EOD)
    assert pit.epoch_number(before) == 0 and pit.epoch_number(after) == 1
    old = pit.last_close("VCN", DAY, before)
    new = pit.last_close("VCN", DAY, after)
    assert old is not None and new is not None and new[1] == pytest.approx(old[1] * 1.02, abs=0.01)
    i = pit.epoch(before).indices["VCN"].dates.index(DAY)
    w0 = pit.epoch(before).indices["VCN"].wealth[i]
    w1 = pit.epoch(after).indices["VCN"].wealth[i]
    assert w1 > w0  # the index is rebuilt for the new epoch, never edited in place


def test_nothing_after_the_tick_is_visible(store: VersionedStore) -> None:
    pit = PointInTimeData(store, CFG, "csv", "xcheck")
    now = release_utc(DAY, EOD)
    snap = pit.snapshot(now)
    assert max(b.session_date for b in snap.bars["VCN"]) == DAY
    assert all(o.released_at_utc is not None and o.released_at_utc <= now for o in snap.corra)
    assert pit.usd_to_cad(DAY, now) == (DAY, next(o.value for o in snap.fx if o.obs_date == DAY))
