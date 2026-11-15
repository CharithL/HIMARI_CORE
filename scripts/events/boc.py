"""Bank of Canada scheduled rate announcements 2015-2025 from bankofcanada.ca.

Dates and the announcement time come from the Bank's yearly schedule releases; each date is
confirmed against its fixed-announcement-date (FAD) press release, which is the source URL.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import httpx
from polite import fetch

ET = ZoneInfo("America/Toronto")
BASE = "https://www.bankofcanada.ca"
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


def _text(raw: bytes) -> str:
    s = raw.decode("utf-8", "replace")
    s = re.sub(r"<(script|style|nav|header|footer).*?</\1>", " ", s, flags=re.S)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)))


def schedule_pages() -> dict[int, str]:
    """Target year -> URL of the release that published that year's schedule."""
    found: dict[int, str] = {}
    for pub_year in range(2014, 2025):
        for month in (6, 7, 8, 9):
            page = 1
            while True:
                url = f"{BASE}/{pub_year}/{month:02d}/" + (f"page/{page}/" if page > 1 else "")
                try:
                    s = fetch(url)[0].decode("utf-8", "replace")
                except httpx.HTTPStatusError:
                    break
                for link in set(
                    re.findall(rf'href="({BASE}/{pub_year}/{month:02d}/[^"]*schedule[^"]*)"', s)
                ):
                    m = re.search(r"(20\d\d)", link.split("/", 5)[-1])
                    if m and int(m.group(1)) == pub_year + 1:
                        found.setdefault(pub_year + 1, link)
                if f"/{pub_year}/{month:02d}/page/{page + 1}/" not in s:
                    break
                page += 1
    return found


def parse_schedule(url: str, year: int) -> tuple[list[date], time | None]:
    t = _text(fetch(url)[0])
    i = t.find(f"for {year}")
    body = t[i : i + 1500] if i >= 0 else t
    days: list[date] = []
    for month, day in re.findall(
        r"(?:Wednesday|Tuesday|Thursday|Monday|Friday),? (\w+) (\d{1,2})\b", body
    ):
        if month in MONTHS:
            days.append(date(year, MONTHS[month], int(day)))
    for day, month in re.findall(
        r"(?:Wednesday|Tuesday|Thursday|Monday|Friday),? (\d{1,2}) (\w+)\b", body
    ):
        if month in MONTHS:
            days.append(date(year, MONTHS[month], int(day)))
    m = re.search(
        r"announcements? (?:will (?:take place|be made)|are made|will occur) "
        r"at (\d{1,2})[:h.](\d{2})",
        t,
    )
    when = time(int(m.group(1)), int(m.group(2))) if m else None
    return sorted(set(days))[:8], when


def collect() -> tuple[list[dict[str, object]], list[str]]:
    from boc_parse import rate_dates

    pages = schedule_pages()
    listed: dict[date, tuple[time | None, str]] = {}
    for year in sorted(pages):  # a later release (reconfirmation) overrides an earlier one
        days, when = rate_dates(_text(fetch(pages[year])[0]))
        for d in days:
            listed[d] = (when, pages[year])
    events: list[dict[str, object]] = []
    problems: list[str] = [
        f"{y}: schedule release not found" for y in range(2015, 2026) if y not in pages
    ]
    for d in sorted(listed):
        if not 2015 <= d.year <= 2025:
            continue
        when, schedule_url = listed[d]
        url = f"{BASE}/{d.year}/{d.month:02d}/fad-press-release-{d.isoformat()}/"
        try:
            _, retrieved = fetch(url)
        except httpx.HTTPStatusError as exc:
            problems.append(f"{d}: FAD release not found ({exc.response.status_code})")
            continue
        events.append(
            {
                "type": "BOC",
                "release_utc": datetime.combine(d, when or time(10, 0), ET).astimezone(
                    ZoneInfo("UTC")
                ),
                "source_url": url,
                "retrieved_utc": retrieved,
                "time_assumed": when is None,
                "time_source_url": schedule_url,
            }
        )
    return events, problems


if __name__ == "__main__":
    rows, problems = collect()
    by_year: dict[int, list[str]] = {}
    for r in rows:
        dt = r["release_utc"]
        assert isinstance(dt, datetime)
        by_year.setdefault(dt.year, []).append(
            dt.astimezone(ET).strftime("%m-%d %H:%M") + ("*" if r["time_assumed"] else "")
        )
    for y in sorted(by_year):
        print(y, len(by_year[y]), " ".join(by_year[y]))
    print("problems:", problems or "none")
