"""ECB, SNB and BoJ scheduled policy decisions 2015-2025 from their official sites.

None of these decision pages states a release time, so each entry uses the institution's
published standard time and is flagged time_assumed (ECB: 13:45 CET, 14:15 CET from 21 July
2022 per its 27 June 2022 notice; SNB: 09:30 per its event schedule). The BoJ publishes no
standard time; 12:00 JST is used and flagged.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from polite import fetch

UTC = ZoneInfo("UTC")
ECB_TIME_CHANGE = date(2022, 7, 21)
# BoJ 'b' statements are special meetings. 2020-03-16 was the regular March meeting brought
# forward (announced before that Friday's decision close); 2020-05-22 was extraordinary.
BOJ_RESCHEDULED = {date(2020, 3, 16)}
ECB_TITLES = {"monetary policy decisions", "combined monetary policy decisions and statement"}


def ecb() -> list[dict[str, object]]:
    out: dict[date, dict[str, object]] = {}
    for year in range(2015, 2026):
        url = f"https://www.ecb.europa.eu/press/govcdec/mopo/{year}/html/index_include.en.html"
        s, retrieved = fetch(url)
        rows = re.findall(
            r'<dt isoDate="(\d{4}-\d{2}-\d{2})">.*?<div class="title"><a href="([^"]+)"\s*>([^<]+)</a>',
            s.decode("utf-8", "replace"),
            re.S,
        )
        for iso, href, title in rows:
            if html.unescape(title).strip().lower() not in ECB_TITLES:
                continue
            d = date.fromisoformat(iso)
            local = time(14, 15) if d >= ECB_TIME_CHANGE else time(13, 45)
            out.setdefault(d, {
                "type": "ECB",
                "release_utc": datetime.combine(d, local, ZoneInfo("Europe/Berlin")).astimezone(UTC),
                "source_url": "https://www.ecb.europa.eu" + href,
                "retrieved_utc": retrieved,
                "time_assumed": True,
            })
    return [out[d] for d in sorted(out)]


def snb() -> list[dict[str, object]]:
    url = "https://www.snb.ch/en/the-snb/mandates-goals/monetary-policy/decisions"
    s, retrieved = fetch(url)
    items = re.findall(
        r'<a class="m-mixed-list-item[^"]*"\s+href="([^"]*pre_(\d{8})[^"]*)"\s*>(.*?)</a>',
        s.decode("utf-8", "replace"),
        re.S,
    )
    out: dict[date, dict[str, object]] = {}
    for href, ymd, inner in items:
        if not ("2015" <= ymd[:4] <= "2025") or "Monetary policy assessment" not in inner:
            continue
        d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        out.setdefault(d, {
            "type": "SNB",
            "release_utc": datetime.combine(d, time(9, 30), ZoneInfo("Europe/Zurich")).astimezone(UTC),
            "source_url": "https://www.snb.ch" + href,
            "retrieved_utc": retrieved,
            "time_assumed": True,
        })
    return [out[d] for d in sorted(out)]


def boj() -> list[dict[str, object]]:
    url = "https://www.boj.or.jp/en/mopo/mpmsche_minu/past.htm"
    s, retrieved = fetch(url)
    out: dict[date, dict[str, object]] = {}
    page = s.decode("utf-8", "replace")
    for href, ymd, kind in re.findall(r'href="([^"]*mpmdeci/[^"]*/k(\d{6})([ab])\.pdf)"', page):
        d = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
        if not 2015 <= d.year <= 2025 or (kind == "b" and d not in BOJ_RESCHEDULED):
            continue
        out.setdefault(d, {
            "type": "BOJ",
            "release_utc": datetime.combine(d, time(12, 0), ZoneInfo("Asia/Tokyo")).astimezone(UTC),
            "source_url": "https://www.boj.or.jp" + href,
            "retrieved_utc": retrieved,
            "time_assumed": True,
        })
    return [out[d] for d in sorted(out)]


if __name__ == "__main__":
    for name, rows in (("ECB", ecb()), ("SNB", snb()), ("BOJ", boj())):
        by_year: dict[int, int] = {}
        for r in rows:
            dt = r["release_utc"]
            assert isinstance(dt, datetime)
            by_year[dt.year] = by_year.get(dt.year, 0) + 1
        print(name, len(rows), by_year)
