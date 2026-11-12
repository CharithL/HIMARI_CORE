"""Stage 1 decision on the fixture set: limits, determinism, no look-ahead and a golden."""

import math
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from himari.accounting.indices import cash_index, total_return_index
from himari.core.types import Bar
from himari.data.calendars import release_utc, sessions
from himari.data.fx import splice_fx
from himari.data.ingest import boc_calendar
from himari.data.storage import VersionedStore
from himari.risk.covariance import covariance
from himari.strategy.stage1 import Stage1Decision, stage1_decision
from tests.integration.test_fixture_ingest import CFG, EOD, NAMES, START, ingest_fixtures

CURRENCY = {i.symbol: i.currency for i in CFG.stage1.instruments()}
DECISION = date(2025, 12, 31)

# Weights from the fixture decision on 2025-12-31. Change only with an amendment or a
# CHANGELOG entry that explains why (last re-pinned for amendment v1.4).
GOLDEN_WEIGHTS = {
    "VCN": 0.0,
    "VUN": 0.0,
    "VIU": 0.06226519411285012,
    "VEE": 0.05449920594018528,
    "VGV": 0.22514870385485977,
    "CGL.C": 0.0,
    "PDBC": 0.16603954370464696,
    "DLR": 0.0,
}


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> VersionedStore:
    return ingest_fixtures(tmp_path_factory.mktemp("clean"))


def decide(
    store: VersionedStore, day: date, bars_after: dict[str, list[Bar]] | None = None
) -> Stage1Decision:
    """Point-in-time inputs as of the decision time, or full history when bars_after is given."""
    as_of = None if bars_after is not None else release_utc(day, EOD)
    bars = bars_after or store.bars("csv", as_of)
    actions = store.actions("csv", as_of)
    fx = splice_fx(store.series(NAMES.fx_legacy, as_of), store.series(NAMES.fx, as_of))
    corra = store.series(NAMES.cash_rate, as_of)
    boc = boc_calendar(store, NAMES, None)
    indices = {
        s: total_return_index(b, actions.get(s, []), CURRENCY[s], fx) for s, b in bars.items()
    }
    cash = {
        s: cash_index(idx.dates, corra, boc, cutoffs=[release_utc(d, EOD) for d in idx.dates])
        for s, idx in indices.items()
    }
    return stage1_decision(CFG, day, indices, cash, sessions("XTSE", START, day))


class TestFixtureDecision:
    def test_limits(self, store: VersionedStore) -> None:
        d = decide(store, DECISION)
        w = d.weights()
        assert len(d.eligible) == 8
        assert all(x >= 0.0 for x in w.values())
        assert sum(w.values()) <= 1.0 + 1e-12
        assert all(x <= 0.25 + 1e-12 for x in w.values())
        assert w["VCN"] + w["VUN"] + w["VIU"] + w["VEE"] <= 0.40 + 1e-12
        assert w["DLR"] <= 0.15 + 1e-12
        assert all(w[s] == 0.0 for s, f in d.funds.items() if not f.signal.on)
        sigmas = [d.funds[s].sigma_floored or 0.0 for s in d.eligible]
        all_on = d.sizing.all_on
        assert math.sqrt(all_on @ covariance(sigmas, d.corr) @ all_on) == pytest.approx(0.08)

    def test_deterministic(self, store: VersionedStore) -> None:
        assert decide(store, DECISION).weights() == decide(store, DECISION).weights()

    def test_golden_weights(self, store: VersionedStore) -> None:
        w = decide(store, DECISION).weights()
        assert w == pytest.approx(GOLDEN_WEIGHTS, abs=1e-12)

    def test_no_look_ahead(self, store: VersionedStore) -> None:
        """Changing every bar after the decision date leaves the decision unchanged."""
        day = date(2025, 6, 27)
        full = store.bars("csv")
        shocked = {
            s: [
                b if b.session_date <= day else b.model_copy(update={"close": b.close * 1.5})
                for b in bars
            ]
            for s, bars in full.items()
        }
        a, b = decide(store, day, full), decide(store, day, shocked)
        assert a.weights() == b.weights()
        assert np.array_equal(a.corr, b.corr)

    def test_new_fund_without_history_keeps_its_budget_in_cash(self, store: VersionedStore) -> None:
        """The early-2015 PDBC case (v1.4): PDBC has under 252 sessions, the others are eligible.

        PDBC stays in k with its 20% budget and zero correlation, so the others get no more
        risk than if PDBC were eligible and uncorrelated; its weight is 0.
        """
        late = {
            s: [b for b in bars if s != "PDBC" or b.session_date >= date(2025, 3, 3)]
            for s, bars in store.bars("csv").items()
        }
        d = decide(store, DECISION, late)
        assert "PDBC" not in d.eligible and len(d.eligible) == 7
        assert d.weights()["PDBC"] == 0.0
        symbols = list(d.funds)
        p = symbols.index("PDBC")
        others = [i for i in range(len(symbols)) if i != p]
        assert np.all(d.corr[p, others] == 0.0)
        b = np.array([CFG.stage1.budget_for(i) for i in CFG.stage1.instruments()])
        assert d.sizing.scale == pytest.approx(0.08 / math.sqrt(b @ d.corr @ b), rel=1e-12)
        k_if_dropped = 0.08 / math.sqrt(b[others] @ d.corr[np.ix_(others, others)] @ b[others])
        assert d.sizing.scale < k_if_dropped  # its budget is not handed to the others

    def test_short_history_is_not_eligible(self, store: VersionedStore) -> None:
        d = decide(store, date(2024, 6, 28))
        assert d.eligible == () and all(x == 0.0 for x in d.weights().values())


def test_fixture_store_path_is_independent(tmp_path: Path) -> None:
    """The decision does not depend on where the store lives."""
    a = decide(ingest_fixtures(tmp_path / "a"), DECISION).weights()
    b = decide(ingest_fixtures(tmp_path / "b"), DECISION).weights()
    assert a == b
