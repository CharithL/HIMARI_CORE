"""Canada CPI and Labour Force Survey releases 2015-2025 from Statistics Canada's The Daily.

Each release announces the next one, so the collector starts at the first 2015 release and
follows that chain, confirming every article by its title. The Daily does not print a time
on these articles; its standard 8:30 a.m. ET is used and flagged time_assumed.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from polite import fetch

ET = ZoneInfo("America/Toronto")
BASE = "https://www150.statcan.gc.ca/n1/daily-quotidien"
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}
TITLE = {"CA_LFS": "Labour Force Survey", "CA_CPI": "Consumer Price Index"}
START_WINDOW = {
    "CA_LFS": (date(2015, 1, 5), date(2015, 1, 16)),
    "CA_CPI": (date(2015, 1, 14), date(2015, 1, 27)),
}


def _decode(raw: bytes) -> str:
    """Older Daily pages declare UTF-8 but carry Windows-1252 bytes."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text).replace("\xa0", " ")).strip()


def _article(day: date, kind: str) -> tuple[str, str, str] | None:
    """(url, text, retrieved) of the day's main release of this kind, or None."""
    ymd = day.strftime("%y%m%d")
    for suffix in "abcdefghij":
        url = f"{BASE}/{ymd}/dq{ymd}{suffix}-eng.htm"
        try:
            raw, retrieved = fetch(url)
        except httpx.HTTPStatusError:
            return None  # no further articles that day
        s = _decode(raw)
        title = re.search(r"<title>(.*?)</title>", s, re.S)
        name = _clean(title.group(1)) if title else ""
        if re.search(rf"{TITLE[kind]}, (?:{'|'.join(MONTHS)}) \d{{4}}", name):
            body = re.sub(r"<(script|style).*?</\1>", " ", s, flags=re.S)
            return url, _clean(re.sub(r"<[^>]+>", " ", body)), retrieved
    return None


NEXT = {
    "CA_LFS": re.compile(
        r"next release of the (?:LFS|Labour Force Survey) will be (?:released |published )?"
        r"on (?:[A-Z][a-z]+day,? )?([A-Z][a-z]+) (\d{1,2})(?:, (\d{4}))?"
    ),
    "CA_CPI": re.compile(
        r"(?:CPI|Consumer Price Index)(?: \( ?CPI ?\))? for [A-Z][a-z]+(?: \d{4})?"
        r"(?:, [^,.]{1,80},)? will be (?:released|published) "
        r"on (?:[A-Z][a-z]+day,? )?([A-Z][a-z]+) (\d{1,2})(?:, (\d{4}))?"
    ),
}


def _next_date(text: str, kind: str, current: date) -> date | None:
    """The announced next release; a date without a year is the next such date."""
    m = NEXT[kind].search(text)
    if not m or m.group(1) not in MONTHS:
        return None
    month, day = MONTHS[m.group(1)], int(m.group(2))
    if m.group(3):
        return date(int(m.group(3)), month, day)
    year = current.year + (1 if month < current.month else 0)
    return date(year, month, day)


def collect(kind: str) -> tuple[list[dict[str, object]], list[str]]:
    problems: list[str] = []
    day, last = START_WINDOW[kind]
    found = None
    while day <= last and found is None:
        if day.weekday() < 5:
            found = _article(day, kind)
        if found is None:
            day += timedelta(days=1)
    events: list[dict[str, object]] = []
    while found is not None and day.year <= 2025:
        url, text, retrieved = found
        events.append(
            {
                "type": kind,
                "release_utc": datetime.combine(day, time(8, 30), ET).astimezone(ZoneInfo("UTC")),
                "source_url": url,
                "retrieved_utc": retrieved,
                "time_assumed": True,
            }
        )
        nxt = _next_date(text, kind, day)
        if nxt is None:
            problems.append(f"{kind} {day}: no next-release date in the article")
            break
        day, found = nxt, _article(nxt, kind)
        if found is None and nxt.year <= 2025:
            problems.append(f"{kind} {nxt}: announced release not found")
    return events, problems


if __name__ == "__main__":
    for kind in TITLE:
        rows, problems = collect(kind)
        by_year: dict[int, int] = {}
        for r in rows:
            dt = r["release_utc"]
            assert isinstance(dt, datetime)
            by_year[dt.year] = by_year.get(dt.year, 0) + 1
        print(kind, len(rows), by_year, "problems:", problems or "none")
