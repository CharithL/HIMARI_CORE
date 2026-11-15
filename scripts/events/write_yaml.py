"""Write all nine event types 2015-2025 to config/events/events_YYYY.yaml (cached pages only)."""

import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
REPO = Path("C:/Users/chari/OneDrive/Documents/himari-core")
sys.path.insert(0, str(HERE))

import adjustments  # noqa: E402
import bls  # noqa: E402
import boc  # noqa: E402
import central_banks  # noqa: E402
import fomc  # noqa: E402
import statcan  # noqa: E402

rows = fomc.collect() + boc.collect()[0]
rows += central_banks.ecb() + central_banks.snb() + central_banks.boj()
rows += bls.collect("US_CPI") + bls.collect("US_NFP")
for kind in ("CA_CPI", "CA_LFS"):
    found, problems = statcan.collect(kind)
    assert not problems, problems
    rows += found

rows = adjustments.apply(rows)  # v1.7: unscheduled kept and flagged; rescheduled flagged

by_year: dict[int, list[dict[str, object]]] = {}
for r in rows:
    dt = r["release_utc"]
    assert isinstance(dt, datetime)
    by_year.setdefault(dt.year, []).append(r)
assert sorted(by_year) == list(range(2015, 2026)), sorted(by_year)

out_dir = REPO / "config" / "events"
for year, events in sorted(by_year.items()):
    lines = [
        f"# Official release times for the event gate (§8.4, v1.5), {year}.",
        "# Collected politely from the official sites by scripts/events/ (see source_url).",
        "# Scheduled releases, plus unscheduled announcements kept for crisis replays.",
        "# time_assumed: true means only the date is published and the institution's standard",
        "# time is used (ECB 13:45 CET, 14:15 CET from 2022-07-21; SNB 09:30; BoJ 12:00 JST;",
        "# StatCan 08:30 ET).",
        "# unscheduled: true marks announcements not on the published calendar in advance; the",
        "# event gate ignores them (v1.7). rescheduled: true marks releases moved from their",
        "# published date (notice_url); the gate uses them only from announced_utc.",
        "events:",
    ]
    for e in sorted(events, key=lambda e: (e["release_utc"], e["type"])):
        dt = e["release_utc"]
        assert isinstance(dt, datetime)
        extra = ""
        if e.get("unscheduled"):
            extra += ", unscheduled: true"
        if e.get("rescheduled"):
            extra += f", rescheduled: true, notice_url: '{e['notice_url']}'"
            if e.get("announced_utc"):
                extra += f", announced_utc: '{e['announced_utc'].isoformat()}'"  # type: ignore[union-attr]
        lines.append(
            f"  - {{type: {e['type']}, release_utc: '{dt.isoformat()}', "
            f"source_url: '{e['source_url']}', retrieved_utc: '{e['retrieved_utc']}', "
            f"version: 1, time_assumed: {str(e['time_assumed']).lower()}{extra}}}"
        )
    text = "\n".join(lines) + "\n"
    (out_dir / f"events_{year}.yaml").write_text(text, encoding="utf-8", newline="\n")

scripts = REPO / "scripts" / "events"
scripts.mkdir(parents=True, exist_ok=True)
for name in (
    "polite.py",
    "fomc.py",
    "boc.py",
    "boc_parse.py",
    "central_banks.py",
    "bls.py",
    "statcan.py",
    "adjustments.py",
    "write_yaml.py",
):
    shutil.copy(HERE / name, scripts / name)
counts: dict[str, int] = {}
for r in rows:
    counts[str(r["type"])] = counts.get(str(r["type"]), 0) + 1
print("wrote", len(rows), "events in", len(by_year), "files:", dict(sorted(counts.items())))
