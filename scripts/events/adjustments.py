"""Unscheduled announcements and rescheduled releases (amendment v1.7).

Unscheduled announcements are kept in the archive for crisis replays and flagged
`unscheduled: true`; the event gate ignores them (they were not on the published calendar
before the decision close). Their pages publish a date but, except the Fed's, no time: the
institution's standard time is used and flagged `time_assumed` (BoC 10:00 ET in 2020, ECB 13:45
CET, SNB 09:30, BoJ 12:00 JST).

Rescheduled releases keep the date they actually happened, flagged `rescheduled: true`, with the
official notice. A rescheduled release counts for the gate only from `announced_utc`; it is set
only where the notice is dated.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import fomc
from polite import fetch

UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/Toronto")

STATIC = [  # (type, date, official page, standard local time, zone)
    ("BOC", date(2020, 3, 13),
     "https://www.bankofcanada.ca/2020/03/bank-of-canada-lowers-overnight-rate-target-to-%C2%BE-percent/",
     time(10, 0), "America/Toronto"),
    ("BOC", date(2020, 3, 27), "https://www.bankofcanada.ca/2020/03/press-release-2020-03-27/",
     time(10, 0), "America/Toronto"),
    ("ECB", date(2020, 3, 18),
     "https://www.ecb.europa.eu/press/pr/date/2020/html/ecb.pr200318_1~3949d6f266.en.html",
     time(13, 45), "Europe/Berlin"),
    ("SNB", date(2015, 1, 15),
     "https://www.snb.ch/en/publications/communication/press-releases/2015/pre_20150115",
     time(9, 30), "Europe/Zurich"),
    ("SNB", date(2020, 3, 25),
     "https://www.snb.ch/en/publications/communication/press-releases/2020/pre_20200325",
     time(9, 30), "Europe/Zurich"),
    ("SNB", date(2020, 5, 11),
     "https://www.snb.ch/en/publications/communication/press-releases/2020/pre_20200511",
     time(9, 30), "Europe/Zurich"),
]  # fmt: skip

BOJ_UNSCHEDULED = {date(2020, 3, 16), date(2020, 5, 22)}  # brought forward; extraordinary

RESCHEDULED = {  # (type, date it happened) -> (originally scheduled, notice, announced_utc)
    ("US_CPI", date(2025, 10, 24)): (
        date(2025, 10, 15),
        "https://www.bls.gov/bls/092025-cpi-reschedule-notice.htm",
        # the notice's only date is "last modified October 10, 2025": known by that day's end
        datetime(2025, 10, 10, 23, 59, 59, tzinfo=ET).astimezone(UTC),
    ),
    ("US_NFP", date(2025, 11, 20)): (
        date(2025, 10, 3), "https://www.bls.gov/bls/2025-lapse-revised-release-dates.htm", None,
    ),
    ("US_NFP", date(2025, 12, 16)): (
        date(2025, 12, 5), "https://www.bls.gov/bls/2025-lapse-revised-release-dates.htm", None,
    ),
    ("US_CPI", date(2025, 12, 18)): (
        date(2025, 12, 10), "https://www.bls.gov/bls/2025-lapse-revised-release-dates.htm", None,
    ),
}  # fmt: skip


def unscheduled_fomc() -> list[dict[str, object]]:
    """Unscheduled meetings and notation votes with an FOMC statement, 2015-2025."""
    out: dict[date, dict[str, object]] = {}
    for block in fomc._blocks():
        head = re.sub(r"<[^>]+>", " ", block[:600])
        if not fomc.SKIP.search(head):
            continue
        m = fomc.STATEMENT.search(block)
        if not m or not ("2015" <= m.group(1)[:4] <= "2025"):
            continue
        d = date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:]))
        url = f"{fomc.BASE}/newsevents/pressreleases/monetary{m.group(1)}a.htm"
        body, retrieved = fetch(url)
        t = fomc.RELEASE_AT.search(body.decode("utf-8", "replace"))
        if t:
            hour = int(t.group(1)) % 12 + (12 if t.group(3).lower() == "p" else 0)
            local, assumed = time(hour, int(t.group(2))), False
        else:
            local, assumed = time(14, 0), True
        out[d] = {
            "type": "FOMC",
            "release_utc": datetime.combine(d, local, ET).astimezone(UTC),
            "source_url": url,
            "retrieved_utc": retrieved,
            "time_assumed": assumed,
            "unscheduled": True,
        }
    return [out[d] for d in sorted(out)]


def unscheduled_static() -> list[dict[str, object]]:
    out = []
    for kind, d, url, local, zone in STATIC:
        _, retrieved = fetch(url)
        out.append(
            {
                "type": kind,
                "release_utc": datetime.combine(d, local, ZoneInfo(zone)).astimezone(UTC),
                "source_url": url,
                "retrieved_utc": retrieved,
                "time_assumed": True,
                "unscheduled": True,
            }
        )
    return out


def unscheduled_boj() -> list[dict[str, object]]:
    url = "https://www.boj.or.jp/en/mopo/mpmsche_minu/past.htm"
    page, retrieved = fetch(url)
    out = []
    for href, ymd in re.findall(
        r'href="([^"]*mpmdeci/[^"]*/k(\d{6})b\.pdf)"', page.decode("utf-8", "replace")
    ):
        d = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
        if d in BOJ_UNSCHEDULED:
            out.append(
                {
                    "type": "BOJ",
                    "release_utc": datetime.combine(
                        d, time(12, 0), ZoneInfo("Asia/Tokyo")
                    ).astimezone(UTC),
                    "source_url": "https://www.boj.or.jp" + href,
                    "retrieved_utc": retrieved,
                    "time_assumed": True,
                    "unscheduled": True,
                }
            )
    return out


def apply(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Add the unscheduled entries (replacing a scheduled copy of the same release) and flag
    the rescheduled ones."""
    extra = unscheduled_fomc() + unscheduled_static() + unscheduled_boj()
    keys = {(e["type"], e["release_utc"].astimezone(UTC).date()) for e in extra}  # type: ignore[union-attr]
    kept = [r for r in rows if (r["type"], r["release_utc"].astimezone(UTC).date()) not in keys]  # type: ignore[union-attr]
    found = set()
    for r in kept:
        day = r["release_utc"].astimezone(ET).date()  # type: ignore[union-attr]
        key = (r["type"], day)
        if key in RESCHEDULED:
            original, notice, announced = RESCHEDULED[key]
            r["rescheduled"] = True
            r["notice_url"] = notice
            if announced is not None:
                r["announced_utc"] = announced
            found.add(key)
    missing = set(RESCHEDULED) - found
    assert not missing, f"rescheduled releases not in the archive: {missing}"
    return kept + extra
