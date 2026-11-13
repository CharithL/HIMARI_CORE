"""US CPI and Employment Situation releases 2015-2025 from bls.gov archived news releases.

The release date is in each archived file name; the time is the embargo time printed in
the release itself ("embargoed until 8:30 a.m. (ET)").
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from polite import fetch

ET = ZoneInfo("America/New_York")
SOURCES = {
    "US_CPI": ("https://www.bls.gov/bls/news-release/cpi.htm", "cpi"),
    "US_NFP": ("https://www.bls.gov/bls/news-release/empsit.htm", "empsit"),
}
# The Employment Situation prints its release number (USDL-...) inside this phrase,
# before or after "until".
EMBARGO = re.compile(
    r"embargoed\s+(?:USDL-[\d-]+\s+)?until\s+(?:USDL-[\d-]+\s+)?"
    r"(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?",
    re.I,
)


def collect(kind: str) -> list[dict[str, object]]:
    index, stem = SOURCES[kind]
    page = fetch(index)[0].decode("utf-8", "replace")
    out: dict[date, dict[str, object]] = {}
    links = rf'href="(?:https://www\.bls\.gov)?(/news\.release/archives/{stem}_(\d{{8}})\.htm)"'
    for href, mdy in re.findall(links, page):
        d = date(int(mdy[4:]), int(mdy[:2]), int(mdy[2:4]))
        if not 2015 <= d.year <= 2025 or d in out:
            continue
        url = "https://www.bls.gov" + href
        body, retrieved = fetch(url)
        m = EMBARGO.search(body.decode("utf-8", "replace"))
        if m:
            hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
            local, assumed = time(hour, int(m.group(2))), False
        else:
            local, assumed = time(8, 30), True  # BLS standard release time
        out[d] = {
            "type": kind,
            "release_utc": datetime.combine(d, local, ET).astimezone(ZoneInfo("UTC")),
            "source_url": url,
            "retrieved_utc": retrieved,
            "time_assumed": assumed,
        }
    return [out[d] for d in sorted(out)]


if __name__ == "__main__":
    for kind in SOURCES:
        rows = collect(kind)
        by_year: dict[int, int] = {}
        for r in rows:
            dt = r["release_utc"]
            assert isinstance(dt, datetime)
            by_year[dt.year] = by_year.get(dt.year, 0) + 1
        assumed = sum(1 for r in rows if r["time_assumed"])
        times = sorted({r["release_utc"].astimezone(ET).strftime("%H:%M") for r in rows})  # type: ignore[union-attr]
        print(kind, len(rows), by_year, "assumed", assumed, "times", times)
