"""Data sources: CsvSource, BocValetSource (offline) and the VendorSource stub."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pydantic
import pytest

from himari.data.sources import BocValetSource, CsvSource, VendorSource

FIXTURES = Path(__file__).parents[1] / "fixtures"
MARKET = FIXTURES / "market" / "primary"
VALET = FIXTURES / "boc_valet"
ALL = (date(2000, 1, 1), date(2030, 1, 1))


class TestCsvSource:
    def test_bars_with_adjusted_close(self) -> None:
        bars = CsvSource(MARKET).fetch_bars("PDBC", *ALL)
        assert len(bars) > 400
        first = bars[0]
        assert first.symbol == "PDBC" and first.source == "csv"
        assert first.session_date == date(2024, 1, 2)
        assert first.adj_close is not None

    def test_date_range_is_inclusive(self) -> None:
        bars = CsvSource(MARKET).fetch_bars("VCN", date(2025, 1, 2), date(2025, 1, 3))
        assert [b.session_date for b in bars] == [date(2025, 1, 2), date(2025, 1, 3)]

    def test_actions_parse_split_and_cash_exactly(self) -> None:
        actions = CsvSource(MARKET).fetch_actions("VIU", *ALL)
        split = next(a for a in actions if a.action_type == "split")
        assert split.ex_date == date(2025, 6, 16) and split.amount == Decimal("2")
        cash = next(a for a in actions if a.action_type == "cash")
        assert cash.amount == Decimal("0.2545")

    def test_series(self) -> None:
        fx = CsvSource(MARKET).fetch_series("FXUSDCAD", *ALL)
        assert fx[0].obs_date == date(2024, 1, 2) and fx[0].value == 1.35

    def test_missing_file_is_empty(self) -> None:
        assert CsvSource(MARKET).fetch_bars("NOPE", *ALL) == []

    def test_rows_keep_file_order(self, tmp_path: Path) -> None:
        (tmp_path / "bars").mkdir()
        (tmp_path / "bars" / "X.csv").write_text(
            "session_date,open,high,low,close,volume\n2025-01-03,1,1,1,1,1\n2025-01-02,1,1,1,1,1\n",
            encoding="utf-8",
        )
        bars = CsvSource(tmp_path).fetch_bars("X", *ALL)
        assert [b.session_date.day for b in bars] == [3, 2]  # Q3 must be able to see this

    def test_unknown_action_type_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "actions").mkdir()
        (tmp_path / "actions" / "X.csv").write_text(
            "ex_date,pay_date,type,amount,currency\n2025-01-03,,rights,1,\n", encoding="utf-8"
        )
        with pytest.raises(pydantic.ValidationError):
            CsvSource(tmp_path).fetch_actions("X", *ALL)


class TestBocValetParse:
    def test_values_and_blank_skipped(self) -> None:
        obs = BocValetSource.parse("FXUSDCAD", (VALET / "FXUSDCAD.json").read_bytes())
        assert [o.obs_date for o in obs] == [
            date(2025, 6, 26),
            date(2025, 6, 27),
            date(2025, 7, 2),
            date(2025, 7, 3),
        ]
        assert obs[0].value == 1.365 and obs[0].source == "boc_valet"

    def test_corra(self) -> None:
        obs = BocValetSource.parse("AVG.INTWO", (VALET / "AVG.INTWO.json").read_bytes())
        assert [o.value for o in obs] == [2.74, 2.75, 2.76]

    def test_missing_observations_raises(self) -> None:
        with pytest.raises(ValueError, match="observations"):
            BocValetSource.parse("FXUSDCAD", b'{"error": "not found"}')

    def test_non_finite_value_raises(self) -> None:
        raw = b'{"observations":[{"d":"2025-01-02","FXUSDCAD":{"v":"NaN"}}]}'
        with pytest.raises(ValueError, match="non-finite"):
            BocValetSource.parse("FXUSDCAD", raw)


class TestBocValetHttp:
    def _source(self, handler: httpx.MockTransport) -> BocValetSource:
        return BocValetSource("https://valet.test/valet", client=httpx.Client(transport=handler))

    def test_request_shape_and_range_filter(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=(VALET / "FXUSDCAD.json").read_bytes())

        src = self._source(httpx.MockTransport(handler))
        obs = src.fetch_series("FXUSDCAD", date(2025, 6, 27), date(2025, 7, 2))
        assert seen[0].url.path == "/valet/observations/FXUSDCAD/json"
        assert seen[0].url.params["start_date"] == "2025-06-27"
        assert seen[0].url.params["end_date"] == "2025-07-02"
        assert [o.obs_date for o in obs] == [date(2025, 6, 27), date(2025, 7, 2)]

    def test_http_error_raises(self) -> None:
        src = self._source(httpx.MockTransport(lambda r: httpx.Response(404)))
        with pytest.raises(httpx.HTTPStatusError):
            src.fetch_raw("NOPE", date(2025, 1, 1), date(2025, 1, 2))

    def test_raw_is_returned_unchanged(self) -> None:
        payload = (VALET / "AVG.INTWO.json").read_bytes()
        src = self._source(httpx.MockTransport(lambda r: httpx.Response(200, content=payload)))
        assert src.fetch_raw("AVG.INTWO", date(2025, 1, 1), date(2025, 7, 1)) == payload


class TestVendorStub:
    """v1.9: the placeholder points at the EODHD adapter."""

    def test_bars_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="EodhdSource"):
            VendorSource().fetch_bars("VCN", *ALL)

    def test_actions_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="EodhdSource"):
            VendorSource().fetch_actions("VCN", *ALL)
