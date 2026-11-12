"""Versioned, append-only storage: corrections, idempotence and as-of views (§5.3)."""

import json
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from himari.core.types import Bar, CorporateAction, SeriesObservation
from himari.data.fx import splice_fx, splice_overlap, write_overlap_log
from himari.data.ingest import SeriesNames, ingest_boc
from himari.data.sources import BocValetSource
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
VALET = Path(__file__).parents[1] / "fixtures" / "boc_valet"
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
    """Valet ingestion with mocked HTTP: raw kept unchanged, release times, FX splice (v1.3)."""

    names = SeriesNames("FXUSDCAD", "AVG.INTWO")
    eod = time(18, 30)

    @staticmethod
    def source(payloads: dict[str, bytes]) -> BocValetSource:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payloads[request.url.path.split("/")[-2]])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        return BocValetSource("https://valet.test", client=client)

    @staticmethod
    def payload(series: str, values: dict[str, str]) -> bytes:
        obs = [{"d": d, series: {"v": v}} for d, v in values.items()]
        return json.dumps({"observations": obs}).encode()

    def test_2025_pull(self, tmp_path: Path) -> None:
        payloads = {s: (VALET / f"{s}.json").read_bytes() for s in ("FXUSDCAD", "AVG.INTWO")}
        raw, store = RawStore(tmp_path / "raw"), VersionedStore(tmp_path / "clean")
        observed = datetime(2025, 7, 10, tzinfo=UTC)
        span = (date(2025, 6, 1), date(2025, 7, 31))
        ingest_boc(self.source(payloads), raw, store, self.names, None, *span, observed, self.eod)

        saved = sorted((tmp_path / "raw" / "boc_valet").iterdir())
        assert [p.read_bytes() for p in saved] == [payloads["AVG.INTWO"], payloads["FXUSDCAD"]]
        fx = {o.obs_date: o.released_at_utc for o in store.series("FXUSDCAD")}
        assert fx[date(2025, 7, 2)] == datetime(2025, 7, 2, 20, 30, tzinfo=UTC)  # 16:30 ET
        corra = {o.obs_date: o.released_at_utc for o in store.series("AVG.INTWO")}
        # Jul 1 is absent from the record, so CORRA for Jun 30 is known Jul 2 at 11:00 ET
        assert corra[date(2025, 6, 30)] == datetime(2025, 7, 2, 15, 0, tzinfo=UTC)

    def test_legacy_splice_across_2017(self, tmp_path: Path) -> None:
        """SYNTHETIC values. Legacy noon rate through 2016-12-30, FXUSDCAD from 2017-01-03."""
        payloads = {
            "IEXE0101": self.payload(
                "IEXE0101",
                {
                    "2016-12-28": "1.3500",
                    "2016-12-29": "1.3510",
                    "2016-12-30": "1.3427",
                    "2017-01-03": "1.3480",
                    "2017-01-04": "1.3437",
                },
            ),
            "FXUSDCAD": self.payload("FXUSDCAD", {"2017-01-03": "1.3483", "2017-01-04": "1.3440"}),
            "AVG.INTWO": self.payload(
                "AVG.INTWO",
                {
                    "2016-12-28": "0.48",
                    "2016-12-29": "0.48",
                    "2016-12-30": "0.48",
                    "2017-01-03": "0.47",
                },
            ),
        }
        raw, store = RawStore(tmp_path / "raw"), VersionedStore(tmp_path / "clean")
        observed = datetime(2026, 1, 5, tzinfo=UTC)
        span = (date(2016, 12, 28), date(2017, 1, 4))
        ingest_boc(self.source(payloads), raw, store, self.names, None, *span, observed, self.eod)

        assert len(list((tmp_path / "raw" / "boc_valet").iterdir())) == 3
        legacy = {o.obs_date: o.released_at_utc for o in store.series("IEXE0101")}
        assert legacy[date(2016, 12, 30)] == datetime(2016, 12, 30, 23, 30, tzinfo=UTC)  # 18:30
        corra = {o.obs_date: o.released_at_utc for o in store.series("AVG.INTWO")}
        # Jan 2, 2017 has no publication, so CORRA for Dec 30 is known Jan 3 at 11:00 ET
        assert corra[date(2016, 12, 30)] == datetime(2017, 1, 3, 16, 0, tzinfo=UTC)

        spliced = splice_fx(store.series("IEXE0101"), store.series("FXUSDCAD"))
        assert [(o.obs_date.isoformat(), o.series) for o in spliced] == [
            ("2016-12-28", "IEXE0101"),
            ("2016-12-29", "IEXE0101"),
            ("2016-12-30", "IEXE0101"),
            ("2017-01-03", "FXUSDCAD"),
            ("2017-01-04", "FXUSDCAD"),
        ]
        overlap = splice_overlap(store.series("IEXE0101"), store.series("FXUSDCAD"))
        assert [r.obs_date for r in overlap] == [date(2017, 1, 3), date(2017, 1, 4)]
        assert overlap[0].diff == pytest.approx(0.0003, abs=1e-12)

        log = tmp_path / "logs" / "fx_splice_overlap.csv"
        write_overlap_log(overlap, log)
        lines = log.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "date,IEXE0101,FXUSDCAD,diff,rel_diff,note"
        assert lines[1].startswith("2017-01-03,1.348,1.3483,0.000300,")
        assert lines[1].endswith(",")  # no note for an unreviewed day

    def test_reviewed_overlap_day_is_annotated(self, tmp_path: Path) -> None:
        day = date(2017, 1, 18)
        legacy = [SeriesObservation(series="IEXE0101", obs_date=day, value=1.3177, source="t")]
        current = [SeriesObservation(series="FXUSDCAD", obs_date=day, value=1.3099, source="t")]
        log = tmp_path / "fx_splice_overlap.csv"
        write_overlap_log(splice_overlap(legacy, current), log)
        row = log.read_text(encoding="utf-8").splitlines()[1]
        assert row.startswith("2017-01-18,1.3177,1.3099,-0.007800,-0.005919,")
        assert "rate decision day" in row

    def test_legacy_not_fetched_after_it_stopped(self, tmp_path: Path) -> None:
        payloads = {s: (VALET / f"{s}.json").read_bytes() for s in ("FXUSDCAD", "AVG.INTWO")}
        raw, store = RawStore(tmp_path / "raw"), VersionedStore(tmp_path / "clean")
        observed = datetime(2025, 7, 10, tzinfo=UTC)
        span = (date(2025, 6, 1), date(2025, 7, 31))
        ingest_boc(self.source(payloads), raw, store, self.names, None, *span, observed, self.eod)
        assert store.series("IEXE0101") == []  # a KeyError in the mock would have failed the pull


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
