"""Live smoke test of the Bank of Canada Valet adapter. Run with: pytest --run-network -m network"""

import json
import math
from datetime import date, timedelta

import pytest

from himari.data.sources import BocValetSource


@pytest.mark.network
def test_valet_fx_and_corra_parse() -> None:
    end = date.today()
    start = end - timedelta(days=30)
    src = BocValetSource()
    for series, low, high in (("FXUSDCAD", 0.5, 3.0), ("AVG.INTWO", -1.0, 25.0)):
        raw = src.fetch_raw(series, start, end)
        assert "observations" in json.loads(raw)
        obs = src.parse(series, raw)
        assert obs, f"no {series} observations in the last 30 days"
        assert all(start <= o.obs_date <= end for o in obs)
        assert all(math.isfinite(o.value) and low < o.value < high for o in obs)
        assert [o.obs_date for o in obs] == sorted(o.obs_date for o in obs)
