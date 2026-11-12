"""FOMC scheduled policy statements 2015-2025 from federalreserve.gov."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from polite import fetch

ET = ZoneInfo("America/New_York")
BASE = "https://www.federalreserve.gov"
SKIP = re.compile(r"unscheduled|conference call|notation", re.I)
STATEMENT = re.compile(r"/newsevents/pressreleases/monetary(\d{8})a\.htm")
RELEASE_AT = re.compile(r"For release at\s+(\d{1,2}):(\d{2})\s*([ap])\.m\.", re.I)


def _blocks() -> list[str]:
    out: list[str] = []
    cal = fetch(f"{BASE}/monetarypolicy/fomccalendars.htm")[0].decode("utf-8", "replace")
    out += re.split(r'<div class="[^"]*\bfomc-meeting\b(?![_-])[^"]*"', cal)[1:]
    for year in range(2015, 2021):
        page = fetch(f"{BASE}/monetarypolicy/fomchistorical{year}.htm")[0].decode("utf-8", "replace")
        out += page.split('<div class="panel panel-default panel-padded">')[1:]
    return out


def collect() -> list[dict[str, object]]:
    events: dict[str, dict[str, object]] = {}
    for block in _blocks():
        head = re.sub(r"<[^>]+>", " ", block[:600])
        if SKIP.search(head):
            continue
        m = STATEMENT.search(block)
        if not m or not ("2015" <= m.group(1)[:4] <= "2025"):
            continue
        d = date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:]))
        url = f"{BASE}/newsevents/pressreleases/monetary{m.group(1)}a.htm"
        body, retrieved = fetch(url)
        t = RELEASE_AT.search(body.decode("utf-8", "replace"))
        if t:
            hour = int(t.group(1)) % 12 + (12 if t.group(3).lower() == "p" else 0)
            local, assumed = time(hour, int(t.group(2))), False
        else:
            local, assumed = time(14, 0), True  # the Fed's standard 2:00 p.m. ET statement time
        events[d.isoformat()] = {
            "type": "FOMC",
            "release_utc": datetime.combine(d, local, ET).astimezone(ZoneInfo("UTC")),
            "source_url": url,
            "retrieved_utc": retrieved,
            "time_assumed": assumed,
        }
    return [events[k] for k in sorted(events)]


if __name__ == "__main__":
    rows = collect()
    by_year: dict[int, list[str]] = {}
    for r in rows:
        dt = r["release_utc"]
        assert isinstance(dt, datetime)
        by_year.setdefault(dt.year, []).append(
            dt.astimezone(ET).strftime("%m-%d %H:%M") + ("*" if r["time_assumed"] else "")
        )
    for y in sorted(by_year):
        print(y, len(by_year[y]), " ".join(by_year[y]))
