"""Versioned, append-only storage: corrections, idempotence and as-of views (§5.3)."""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from himari.core.types import Bar, CorporateAction, SeriesObservation
from himari.data.storage import (
    ACTIONS,
    BARS,
    SERIES,
    RawStore,
    VersionedStore,
    action_row,
    bar_row,
    series_row,
)

UTC = timezone.utc
RELEASE = datetime(2025, 1, 2, 23, 30, tzinfo=UTC)
FIRST_PULL = datetime(2025, 1, 3, tzinfo=UTC)
CORRECTION_PULL = datetime(2025, 3, 1, tzinfo=UTC)


def vcn(close: float, adj: float | None = None) -> Bar:
    return Bar(
        symbol="VCN",
        session_date=date(2025, 1, 2),
        open=40.0,
        high=41.0,
        low=39.5,
        close=close,
        volume=100,
        source="vendor",
        adj_close=adj,
    )


class TestVersioning:
    def test_new_then_unchanged_then_corrected(self, tmp_path: Path) -> None:
        store = VersionedStore(tmp_path)
        assert store.append(BARS, [bar_row(vcn(40.5), RELEASE)], FIRST_PULL).added == 1
        assert store.append(BARS, [bar_row(vcn(40.5), RELEASE)], FIRST_PULL).unchanged == 1
        assert store.append(BARS, [bar_row(vcn(40.6), RELEASE)], CORRECTION_PULL).corrected == 1
        assert len(store.load(BARS)) == 2  # the original row is kept

    def test_correction_is_released_when_observed(self, tmp_path: Path) -> None:
        store = VersionedStore(tmp_path)
        store.append(BARS, [bar_row(vcn(40.5), RELEASE)], FIRST_PULL)
        store.append(BARS, [bar_row(vcn(40.6), RELEASE)], CORRECTION_PULL)
        before = store.bars("vendor", datetime(2025, 2, 1, tzinfo=UTC))["VCN"][0]
        after = store.bars("vendor", CORRECTION_PULL)["VCN"][0]
        assert (before.close, before.version) == (40.5, 1)
        assert (after.close, after.version) == (40.6, 2)

    def test_nothing_visible_before_release(self, tmp_path: Path) -> None:
        store = VersionedStore(tmp_path)
        store.append(BARS, [bar_row(vcn(40.5), RELEASE)], FIRST_PULL)
        assert store.bars("vendor", datetime(2025, 1, 2, 22, 0, tzinfo=UTC)) == {}
        assert store.bars("vendor", RELEASE)["VCN"][0].close == 40.5

    def test_missing_adjusted_close_is_not_a_correction(self, tmp_path: Path) -> None:
        store = VersionedStore(tmp_path)
        store.append(BARS, [bar_row(vcn(40.5), RELEASE)], FIRST_PULL)
        assert store.append(BARS, [bar_row(vcn(40.5), RELEASE)], CORRECTION_PULL).unchanged == 1
        assert store.bars("vendor")["VCN"][0].adj_close is None

    def test_duplicate_key_in_one_batch_is_rejected(self, tmp_path: Path) -> None:
        rows = [bar_row(vcn(40.5), RELEASE), bar_row(vcn(40.6), RELEASE)]
        with pytest.raises(ValueError, match="duplicate"):
            VersionedStore(tmp_path).append(BARS, rows, FIRST_PULL)


class TestOtherTables:
    def test_action_amount_round_trips_exactly(self, tmp_path: Path) -> None:
        store = VersionedStore(tmp_path)
        action = CorporateAction(
            symbol="VGV",
            ex_date=date(2025, 1, 27),
            action_type="cash",
            amount=Decimal("0.0712"),
            source="vendor",
        )
        store.append(ACTIONS, [action_row(action, RELEASE)], FIRST_PULL)
        (got,) = store.actions("vendor")["VGV"]
        assert got.amount == Decimal("0.0712") and got.action_type == "cash"

    def test_series_by_name(self, tmp_path: Path) -> None:
        store = VersionedStore(tmp_path)
        obs = SeriesObservation(
            series="FXUSDCAD", obs_date=date(2025, 1, 2), value=1.4381, source="boc_valet"
        )
        store.append(SERIES, [series_row(obs, RELEASE)], FIRST_PULL)
        assert store.series("FXUSDCAD")[0].value == 1.4381
        assert store.series("AVG.INTWO") == []


class TestRawStore:
    def test_never_overwrites(self, tmp_path: Path) -> None:
        raw = RawStore(tmp_path)
        path = raw.save("boc_valet", "FXUSDCAD.json", b"{}", FIRST_PULL)
        assert path.read_bytes() == b"{}"
        with pytest.raises(FileExistsError):
            raw.save("boc_valet", "FXUSDCAD.json", b"{changed}", FIRST_PULL)


class TestIngestBoc:
    def test_raw_kept_unchanged_and_release_times_stamped(self, tmp_path: Path) -> None:
        import httpx

        from himari.data.calendars import BocBusinessDays
        from himari.data.ingest import SeriesNames, ingest_boc
        from himari.data.sources import BocValetSource

        valet = Path(__file__).parents[1] / "fixtures" / "boc_valet"

        def handler(request: httpx.Request) -> httpx.Response:
            series = request.url.path.split("/")[-2]
            return httpx.Response(200, content=(valet / f"{series}.json").read_bytes())

        src = BocValetSource(
            "https://valet.test", client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        raw, store = RawStore(tmp_path / "raw"), VersionedStore(tmp_path / "clean")
        boc = BocBusinessDays([date(2025, 7, 1)], [2025])
        observed = datetime(2025, 7, 10, tzinfo=UTC)
        names = SeriesNames("FXUSDCAD", "AVG.INTWO")
        ingest_boc(src, raw, store, names, boc, date(2025, 6, 1), date(2025, 7, 31), observed)

        saved = sorted((tmp_path / "raw" / "boc_valet").iterdir())
        assert [p.read_bytes() for p in saved] == [
            (valet / "AVG.INTWO.json").read_bytes(),
            (valet / "FXUSDCAD.json").read_bytes(),
        ]
        fx = {o.obs_date: o.released_at_utc for o in store.series("FXUSDCAD")}
        assert fx[date(2025, 7, 2)] == datetime(2025, 7, 2, 20, 30, tzinfo=UTC)  # 16:30 ET
        corra = {o.obs_date: o.released_at_utc for o in store.series("AVG.INTWO")}
        assert corra[date(2025, 6, 30)] == datetime(2025, 7, 2, 15, 0, tzinfo=UTC)  # 11:00 ET


class TestConfirmations:
    def test_persist_and_read_back(self, tmp_path: Path) -> None:
        from himari.quality.confirmations import ConfirmationStore

        path = tmp_path / "state" / "confirmations.jsonl"
        assert ConfirmationStore(path).confirmed() == {}
        ConfirmationStore(path).add("VCN", date(2025, 3, 4), "index rebalance", FIRST_PULL)
        ConfirmationStore(path).add("FXUSDCAD", date(2025, 4, 22), "Easter", FIRST_PULL)
        assert ConfirmationStore(path).confirmed() == {
            "VCN": {date(2025, 3, 4)},
            "FXUSDCAD": {date(2025, 4, 22)},
        }
