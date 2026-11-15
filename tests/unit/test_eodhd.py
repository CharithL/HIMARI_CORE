"""The EODHD adapter (M6 primary) on mocked responses: no network, no real key."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from himari.cli import app
from himari.core.config import load_config
from himari.core.secrets import secret
from himari.core.types import Bar, CorporateAction
from himari.data.coverage import check_fund
from himari.data.eodhd import EodhdClient, EodhdError, EodhdSource
from himari.data.sources import DataSource

ROOT = Path(__file__).parents[2]
KEY = "test-key-1234567890"
EXCHANGES = {"VCN": "TSX", "CGL.C": "TSX", "PDBC": "NASDAQ"}
RESPONSES = {
    "/api/eod/VCN.TO": [
        {"date": "2025-01-02", "open": 44.1, "high": 44.5, "low": 44.0, "close": 44.4,
         "adjusted_close": 43.9, "volume": 120000},
        {"date": "2025-01-03", "open": 44.4, "high": 44.9, "low": 44.3, "close": 44.8,
         "adjusted_close": 44.3, "volume": 0},
    ],
    "/api/div/VCN.TO": [
        {"date": "2025-03-27", "declarationDate": "2025-03-24", "recordDate": "2025-03-27",
         "paymentDate": "2025-04-03", "period": "Quarterly", "value": 0.2, "unadjustedValue": 0.4,
         "currency": "CAD"},
        {"date": "2025-06-26", "declarationDate": None, "recordDate": None, "paymentDate": None,
         "period": None, "value": 0.21, "unadjustedValue": 0.21, "currency": "CAD"},
    ],
    "/api/splits/VCN.TO": [{"date": "2025-06-16", "split": "2.000000/1.000000"}],
    "/api/exchange-symbol-list/TO": [
        {"Code": "CGL-C", "Name": "iShares Gold Bullion ETF", "Country": "Canada",
         "Exchange": "TO", "Currency": "CAD", "Type": "ETF", "Isin": None},
        {"Code": "VCN", "Name": "Vanguard FTSE Canada All Cap", "Country": "Canada",
         "Exchange": "TO", "Currency": "CAD", "Type": "ETF", "Isin": None},
    ],
}  # fmt: skip


def transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/eod/BROKEN.TO":
            return httpx.Response(500, text="boom")
        body = RESPONSES.get(request.url.path)
        return httpx.Response(404) if body is None else httpx.Response(200, json=body)

    return httpx.MockTransport(handle)


def source(tmp_path: Path, seen: list[httpx.Request]) -> EodhdSource:
    client = EodhdClient(KEY, cache_dir=tmp_path / "cache", transport=transport(seen))
    return EodhdSource(client, {**EXCHANGES, "BROKEN": "TSX"})


def test_bars_are_raw_with_the_adjusted_close_beside(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    bars = source(tmp_path, seen).fetch_bars("VCN", date(2025, 1, 1), date(2025, 1, 31))
    assert [(b.session_date, b.close, b.adj_close, b.volume) for b in bars] == [
        (date(2025, 1, 2), 44.4, 43.9, 120000), (date(2025, 1, 3), 44.8, 44.3, 0),
    ]  # fmt: skip
    q = seen[0].url.params
    assert (q["from"], q["to"], q["fmt"]) == ("2025-01-01", "2025-01-31", "json")


def test_distributions_as_paid_and_splits_as_ratios(tmp_path: Path) -> None:
    acts = source(tmp_path, []).fetch_actions("VCN", date(2025, 1, 1), date(2025, 12, 31))
    cash = [a for a in acts if a.action_type == "cash"]
    assert [(a.ex_date, a.pay_date, a.amount) for a in cash] == [
        (date(2025, 3, 27), date(2025, 4, 3), Decimal("0.4")),  # unadjustedValue, not value
        (date(2025, 6, 26), None, Decimal("0.21")),
    ]
    (split,) = [a for a in acts if a.action_type == "split"]
    assert (split.ex_date, split.amount) == (date(2025, 6, 16), Decimal(2))


def test_a_class_ticker_is_resolved_from_the_symbol_list(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    src = source(tmp_path, seen)
    assert src.ticker("CGL.C") == "CGL-C.TO" and src.ticker("PDBC") == "PDBC.US"
    assert src.ticker("CGL.C") == "CGL-C.TO" and len(seen) == 1  # the list is read once


def test_responses_are_cached_and_the_key_stays_out(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    src = source(tmp_path, seen)
    src.fetch_bars("VCN", date(2025, 1, 1), date(2025, 1, 31))
    again = source(tmp_path, seen)  # a fresh client on the same cache
    again.fetch_bars("VCN", date(2025, 1, 1), date(2025, 1, 31))
    assert len(seen) == 1 and again.client.calls == 0
    for f in (tmp_path / "cache").iterdir():
        assert KEY not in f.name and KEY not in f.read_text(encoding="utf-8")
        assert json.loads(f.read_text(encoding="utf-8"))["request"].startswith("eod/VCN.TO?")
    with pytest.raises(EodhdError) as err:
        src.fetch_bars("BROKEN", date(2025, 1, 1), date(2025, 1, 31))
    assert KEY not in str(err.value) and "HTTP 500" in str(err.value)
    assert err.value.__cause__ is None and err.value.__context__ is None


def test_a_network_error_never_shows_the_key(tmp_path: Path) -> None:
    """httpx's own error text holds the URL, key included: it is replaced, not chained."""

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    client = EodhdClient(KEY, cache_dir=tmp_path, transport=httpx.MockTransport(fail))
    with pytest.raises(EodhdError) as err:
        client.get("eod/VCN.TO")
    assert KEY not in str(err.value) and err.value.__suppress_context__
    assert err.value.__cause__ is None


def test_a_client_without_a_key_is_refused() -> None:
    with pytest.raises(EodhdError):
        EodhdClient("")


def test_coverage_without_a_key_says_where_it_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VENDOR_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    config = str(ROOT / "config" / "frozen_v1.yaml")
    result = CliRunner().invoke(app, ["data", "coverage", "--config", config])
    assert result.exit_code == 2 and "VENDOR_API_KEY" in result.output and ".env" in result.output


def test_the_inception_file_covers_every_fund_with_a_source() -> None:
    cfg = load_config(ROOT / "config" / "frozen_v1.yaml")
    raw = yaml.safe_load((ROOT / "config" / "fund_inceptions.yaml").read_text(encoding="utf-8"))
    funds = {i.symbol for i in cfg.stage1.instruments()} | {cfg.cash_sleeve.symbol}
    assert set(raw["funds"]) == funds
    for v in raw["funds"].values():
        assert v["source"].startswith("https://") and isinstance(v["inception"], date)
        assert v.get("listing", v["inception"]) >= v["inception"]


def test_secret_reads_the_environment_then_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text('# vendor\nVENDOR_API_KEY="from-file"\nOTHER=\n', encoding="utf-8")
    monkeypatch.delenv("VENDOR_API_KEY", raising=False)
    assert secret("VENDOR_API_KEY", env) == "from-file"
    assert secret("OTHER", env) is None and secret("MISSING", env) is None
    monkeypatch.setenv("VENDOR_API_KEY", "from-env")
    assert secret("VENDOR_API_KEY", env) == "from-env"


class FakeSource(DataSource):
    name = "fake"

    def __init__(self, days: list[date], actions: list[CorporateAction]) -> None:
        self.days, self.actions = days, actions

    def fetch_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        return [Bar(symbol=symbol, session_date=d, open=1, high=1, low=1, close=1,
                    volume=0 if d == date(2025, 1, 9) else 10, source="fake")
                for d in self.days]  # fmt: skip

    def fetch_actions(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        return self.actions


def test_coverage_lists_late_starts_missing_sessions_and_staleness() -> None:
    days = [date(2025, 1, d) for d in (6, 7, 8, 9, 13, 14)]  # 10 Jan missing; ends 14 Jan
    div = CorporateAction(symbol="X", ex_date=date(2025, 1, 8), action_type="cash",
                          amount=Decimal("0.1"))  # fmt: skip
    r = check_fund(FakeSource(days, [div]), "X", "XTSE", date(2024, 12, 2),
                   date(2024, 1, 1), date(2025, 1, 31))  # fmt: skip
    assert r.first_bar == date(2025, 1, 6) and r.missing_sessions == [date(2025, 1, 10)]
    assert r.distributions_by_year == {2025: 1} and r.zero_volume == 1
    text = " | ".join(r.gaps)
    assert "35 days after the fund's start 2024-12-02" in text
    assert "1 XTSE sessions without a bar: 2025-01-10" in text
    assert "last bar 2025-01-14" in text and "1 sessions with zero volume" in text
    empty = check_fund(FakeSource([], []), "Y", "XTSE", None, date(2024, 1, 1), date(2025, 1, 31))
    assert empty.gaps == ["no bars at all"]
