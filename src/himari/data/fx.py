"""USD/CAD history (v1.3): the Bank of Canada legacy noon rate, then FXUSDCAD.

Both series are CAD per 1 USD. IEXE0101 (legacy U.S. dollar noon rate, last updated
2017-04-28) is used through 2016-12-30; FXUSDCAD (published from 2017-01-03) after.
Every row keeps its own series name, so the origin of each rate stays visible.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from himari.core.types import SeriesObservation

LEGACY_FX_SERIES = "IEXE0101"
LEGACY_FX_LAST = date(2016, 12, 30)  # last legacy row used
LEGACY_FX_FINAL = date(2017, 4, 28)  # the Bank stopped updating the legacy series
FX_FIRST = date(2017, 1, 3)


def splice_fx(
    legacy: Sequence[SeriesObservation], current: Sequence[SeriesObservation]
) -> list[SeriesObservation]:
    rows = [o for o in legacy if o.obs_date <= LEGACY_FX_LAST]
    rows += [o for o in current if o.obs_date >= FX_FIRST]
    return sorted(rows, key=lambda o: o.obs_date)


@dataclass(frozen=True)
class OverlapRow:
    obs_date: date
    legacy: float
    current: float

    @property
    def diff(self) -> float:
        return self.current - self.legacy

    @property
    def rel_diff(self) -> float:
        return self.current / self.legacy - 1.0


def splice_overlap(
    legacy: Sequence[SeriesObservation], current: Sequence[SeriesObservation]
) -> list[OverlapRow]:
    """Dates both series published (Jan-Apr 2017), for the splice log."""
    old = {o.obs_date: o.value for o in legacy}
    return [
        OverlapRow(o.obs_date, old[o.obs_date], o.value)
        for o in sorted(current, key=lambda o: o.obs_date)
        if o.obs_date in old
    ]


def write_overlap_log(rows: Sequence[OverlapRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date", LEGACY_FX_SERIES, "FXUSDCAD", "diff", "rel_diff"])
        for r in rows:
            w.writerow(
                [r.obs_date.isoformat(), r.legacy, r.current, f"{r.diff:.6f}", f"{r.rel_diff:.6f}"]
            )
