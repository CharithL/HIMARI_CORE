"""Parse Bank of Canada schedule releases into (date, time) for rate announcements."""

from __future__ import annotations

import re
from datetime import date, time

from boc import MONTHS

ITEM = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday),?\s+(?:([A-Z][a-z]+)\s+(\d{1,2})|(\d{1,2})\s+([A-Z][a-z]+))\*?"
)
TIME = re.compile(r"rate announcements[^.]*?(\d{1,2}):(\d{2}) \(ET\)")
OTHER = re.compile(r"Survey|Financial (?:Stability|System) Review|Senior Loan", re.I)


def rate_dates(text: str) -> tuple[list[date], time | None]:
    """All rate-announcement dates listed in a release (main list and reconfirmations)."""
    matches = list(ITEM.finditer(text))
    groups: list[list[re.Match[str]]] = []
    for m in matches:
        if groups and re.fullmatch(r"[\s*]*", text[groups[-1][-1].end() : m.start()]):
            groups[-1].append(m)
        else:
            groups.append([m])
    out: list[date] = []
    prev_end = 0
    for g in groups:
        header = text[max(prev_end, g[0].start() - 300) : g[0].start()]
        prev_end = g[-1].end()
        sentences = re.split(r"\.\s+", header)
        header = sentences[-1]  # the sentence that introduces this list
        if not re.search(r"20\d\d", header) and len(sentences) > 1:
            header = sentences[-2] + ". " + header  # e.g. "...2023 schedule... The dates are:"
        if not re.search(r"announcement|decision", header, re.I) or OTHER.search(header):
            continue
        span = re.search(r"from \w+ (\d{4}) through", header)
        years = re.findall(r"\b(20\d\d)\b", header)
        if not years:
            continue
        year = int(span.group(1)) if span else int(years[-1])
        for m in g:
            month, day = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            if month in MONTHS:
                out.append(date(year, MONTHS[month], int(day)))
    t = TIME.search(text)
    return sorted(set(out)), (time(int(t.group(1)), int(t.group(2))) if t else None)
