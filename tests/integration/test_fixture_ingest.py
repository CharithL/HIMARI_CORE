"""M1 acceptance: fixture data ingests deterministically; release times are enforced."""

import hashlib
import importlib
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from himari.accounting.indices import total_return_index
from himari.core.config import load_config
from himari.core.types import Currency, QualityFlag
from himari.data.calendars import last_completed_session, load_boc_holidays, release_utc
from himari.data.ingest import SeriesNames, ingest_actions, ingest_bars, ingest_series_source
from himari.data.sources import CsvSource
from himari.data.storage import ACTIONS, BARS, SERIES, VersionedStore
from himari.quality.checks import Severity
from himari.quality.gate import QualityInputs, apply_data_gate, run_quality_gate

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "market"
CFG = load_config(ROOT / "config" / "frozen_v1.yaml")
UTC = timezone.utc
START, END = date(2024, 1, 1), date(2025, 12, 31)
OBSERVED = datetime(2026, 1, 5, tzinfo=UTC)
EOD = time.fromisoformat(CFG.schedule.daily_job_after_et)
SYMBOLS = [i.symbol for i in CFG.stage1.instruments()]
NAMES = SeriesNames(CFG.sources.fx_series, CFG.sources.cash_rate_series)

# sha256 of the canonical CSV of every stored row (all versions). Update only on purpose.
GOLDEN_CONTENT_HASH = "225ed5747c1f00dd588ddd67d31492bc158b71cd9a3bc9d7a8d90aa17f785fe2"


def ingest_fixtures(root: Path) -> VersionedStore:
    store = VersionedStore(root)
    boc = load_boc_holidays(FIXTURES / "boc_holidays.yaml")
    for folder, name in (("primary", "csv"), ("crosscheck", "xcheck")):
        src = CsvSource(FIXTURES / folder, name=name)
        ingest_bars(src, store, SYMBOLS, START, END, OBSERVED, EOD)
        ingest_actions(src, store, SYMBOLS, START, END, OBSERVED, EOD)
    ingest_series_source(CsvSource(FIXTURES / "primary"), store, NAMES, boc, START, END, OBSERVED)
    return store


def content_hash(store: VersionedStore) -> str:
    h = hashlib.sha256()
    for spec in (BARS, ACTIONS, SERIES):
        table = store.load(spec).sort_values([*spec.key, "version"], kind="mergesort")
        h.update(table.to_csv(index=False, lineterminator="\n").encode("utf-8"))
    return h.hexdigest()


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> VersionedStore:
    return ingest_fixtures(tmp_path_factory.mktemp("clean"))


def decision_time(day: date) -> datetime:
    return release_utc(day, EOD)


class TestDeterministicIngest:
    def test_byte_identical_parquet(self, store: VersionedStore, tmp_path: Path) -> None:
        again = ingest_fixtures(tmp_path)
        for name in ("bars", "actions", "series"):
            a = (store.root / f"{name}.parquet").read_bytes()
            b = (again.root / f"{name}.parquet").read_bytes()
            assert a == b, name

    def test_content_hash_golden(self, store: VersionedStore) -> None:
        assert content_hash(store) == GOLDEN_CONTENT_HASH

    def test_reingest_adds_nothing(self, tmp_path: Path) -> None:
        s = ingest_fixtures(tmp_path)
        src = CsvSource(FIXTURES / "primary", name="csv")
        later = datetime(2026, 2, 1, tzinfo=UTC)
        results = ingest_bars(src, s, SYMBOLS, START, END, later, EOD)
        assert all(r.added == 0 and r.corrected == 0 for r in results.values())

    def test_fixtures_match_generator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gen = importlib.import_module("tests.fixtures.make_market_fixtures")
        monkeypatch.setattr(gen, "ROOT", tmp_path)
        gen.main()
        committed = sorted(p.relative_to(FIXTURES) for p in FIXTURES.rglob("*") if p.is_file())
        regenerated = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
        assert committed == regenerated
        for rel in committed:
            want = (FIXTURES / rel).read_bytes().replace(b"\r\n", b"\n")
            assert (tmp_path / rel).read_bytes() == want, rel


class TestReleaseTimes:
    def test_fx_known_from_1630_et(self, store: VersionedStore) -> None:
        day = date(2025, 3, 4)
        before = datetime(2025, 3, 4, 21, 29, tzinfo=UTC)  # 16:29 ET
        at = datetime(2025, 3, 4, 21, 30, tzinfo=UTC)
        assert day not in {o.obs_date for o in store.series("FXUSDCAD", before)}
        assert day in {o.obs_date for o in store.series("FXUSDCAD", at)}

    def test_corra_waits_for_next_bank_business_day(self, store: VersionedStore) -> None:
        friday = date(2024, 11, 8)  # Monday Nov 11 is a Bank holiday in the fixture calendar
        monday_decision = decision_time(date(2024, 11, 11))
        tuesday_1100 = datetime(2024, 11, 12, 16, 0, tzinfo=UTC)
        assert friday not in {o.obs_date for o in store.series("AVG.INTWO", monday_decision)}
        assert friday in {o.obs_date for o in store.series("AVG.INTWO", tuesday_1100)}

    def test_bars_known_from_daily_job_time(self, store: VersionedStore) -> None:
        day = date(2025, 3, 4)
        just_before = datetime(2025, 3, 4, 23, 29, tzinfo=UTC)
        assert all(b.session_date < day for b in store.bars("csv", just_before)["VCN"])
        assert store.bars("csv", decision_time(day))["VCN"][-1].session_date == day


def gate_inputs(store: VersionedStore, day: date, **overrides: object) -> QualityInputs:
    as_of = decision_time(day)
    codes = CFG.calendars.by_exchange
    instruments = CFG.stage1.instruments()
    fields: dict[str, object] = {
        "instruments": instruments,
        "bars": store.bars("csv", as_of),
        "actions": store.actions("csv", as_of),
        "crosscheck": store.bars("xcheck", as_of),
        "fx": store.series("FXUSDCAD", as_of),
        "corra": store.series("AVG.INTWO", as_of),
        "last_sessions": {
            i.symbol: last_completed_session(codes[i.exchange.value], as_of) for i in instruments
        },
        "master_session": last_completed_session(CFG.calendars.master, as_of),
        "as_of_date": day,
        "boc_days": load_boc_holidays(FIXTURES / "boc_holidays.yaml"),
        "fx_series": "FXUSDCAD",
    }
    fields.update(overrides)
    return QualityInputs(**fields)  # type: ignore[arg-type]


class TestGateOnFixtures:
    def test_clean_fixtures_pass(self, store: VersionedStore) -> None:
        report = run_quality_gate(gate_inputs(store, date(2025, 12, 31)), CFG.data_quality)
        assert {s: q.status for s, q in report.instruments.items()} == dict.fromkeys(SYMBOLS, "OK")
        assert report.instruments["VGV"].count("Q9", Severity.WARN) == 1

    def test_missing_cross_check_faults_only_that_instrument(self, store: VersionedStore) -> None:
        day = date(2025, 12, 31)
        cross = dict(store.bars("xcheck", decision_time(day)))
        cross["PDBC"] = cross["PDBC"][:-1]  # no cross-check for PDBC's decision bar
        report = run_quality_gate(gate_inputs(store, day, crosscheck=cross), CFG.data_quality)
        assert report.instruments["PDBC"].status == "FAULT"
        assert all(q.status == "OK" for s, q in report.instruments.items() if s != "PDBC")
        out, _ = apply_data_gate({"PDBC": 0.18, "VCN": 0.05}, {"PDBC": 0.10}, report)
        assert out == {"PDBC": 0.10, "VCN": 0.05}  # no addition to PDBC; VCN untouched
        cut, _ = apply_data_gate({"PDBC": 0.02}, {"PDBC": 0.10}, report)
        assert cut == {"PDBC": 0.02}  # the fault never blocks a cut

    def test_no_cash_rate_faults_every_instrument(self, store: VersionedStore) -> None:
        report = run_quality_gate(
            gate_inputs(store, date(2025, 12, 31), corra=[]), CFG.data_quality
        )
        assert all(q.status == "FAULT" for q in report.instruments.values())


class TestFixtureUsdIndex:
    def test_easter_mondays_are_bridged_and_canada_day_filled(self, store: VersionedStore) -> None:
        src = CsvSource(FIXTURES / "primary")
        bars = src.fetch_bars("PDBC", START, END)
        idx = total_return_index(
            bars, src.fetch_actions("PDBC", START, END), Currency.USD, store.series("FXUSDCAD")
        )
        assert idx.flags[date(2024, 4, 1)] == [QualityFlag.FX_BRIDGED]
        assert idx.flags[date(2025, 4, 21)] == [QualityFlag.FX_BRIDGED]
        assert idx.flags[date(2025, 4, 22)] == [QualityFlag.FX_BRIDGED]
        assert idx.flags[date(2025, 7, 1)] == [QualityFlag.FX_FILLED]
        assert QualityFlag.FX_OUTAGE not in {f for fs in idx.flags.values() for f in fs}
