"""Polite, cached HTTP for collecting official event calendars (one request at a time)."""

from __future__ import annotations

import hashlib
import time
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

CACHE = Path(__file__).parent / "cache"
UA = "himari-core-event-calendar/0.1 (personal research; polite, cached)"
DELAY = 2.0
_last = 0.0
_robots: dict[str, urllib.robotparser.RobotFileParser] = {}


def _robots_for(url: str) -> urllib.robotparser.RobotFileParser:
    base = "{0.scheme}://{0.netloc}".format(urlparse(url))
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = fetch(base + "/robots.txt", check_robots=False)
            rp.parse(r[0].decode("utf-8", "replace").splitlines())
        except httpx.HTTPError:
            rp.parse([])
        _robots[base] = rp
    return _robots[base]


def fetch(url: str, check_robots: bool = True) -> tuple[bytes, str]:
    """Body and retrieval time (UTC ISO). Cached on disk; robots.txt honoured."""
    global _last
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    body_path, meta_path = CACHE / f"{key}.body", CACHE / f"{key}.meta"
    if body_path.exists():
        return body_path.read_bytes(), meta_path.read_text(encoding="utf-8").split("\n")[1]
    if check_robots and not _robots_for(url).can_fetch(UA, url):
        raise PermissionError(f"robots.txt disallows {url}")
    wait = DELAY - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    resp = httpx.get(url, headers={"User-Agent": UA}, timeout=60, follow_redirects=True)
    _last = time.monotonic()
    resp.raise_for_status()
    retrieved = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    body_path.write_bytes(resp.content)
    meta_path.write_text(f"{url}\n{retrieved}\n{resp.status_code}", encoding="utf-8")
    return resp.content, retrieved
