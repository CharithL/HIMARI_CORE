"""Polite, cached HTTP for collecting official event calendars (one request at a time).

- robots.txt is honoured, including its Crawl-delay (never faster than DELAY seconds).
- Every response is cached on disk, 404s included, so a rerun never repeats a request.
- Connection resets, timeouts, 429 and 5xx back off (30, 60, 120, 240 s) before giving up.
"""

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
BACKOFF = (30.0, 60.0, 120.0, 240.0)
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


def _not_found(url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        f"404 for {url}", request=request, response=httpx.Response(404, request=request)
    )


def _get(url: str, delay: float) -> httpx.Response:
    global _last
    for attempt in range(len(BACKOFF) + 1):
        wait = delay - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = httpx.get(url, headers={"User-Agent": UA}, timeout=60, follow_redirects=True)
        except httpx.TransportError:
            resp = None
        _last = time.monotonic()
        if resp is not None and resp.status_code != 429 and resp.status_code < 500:
            return resp
        if attempt == len(BACKOFF):
            if resp is None:
                raise httpx.ConnectError(f"gave up on {url} after {attempt + 1} attempts")
            resp.raise_for_status()
        time.sleep(BACKOFF[attempt])
    raise AssertionError("unreachable")


def fetch(url: str, check_robots: bool = True) -> tuple[bytes, str]:
    """Body and retrieval time (UTC ISO). Cached on disk, 404s included; robots.txt honoured."""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    body_path, meta_path = CACHE / f"{key}.body", CACHE / f"{key}.meta"
    if meta_path.exists():
        _, retrieved, status = meta_path.read_text(encoding="utf-8").split("\n")[:3]
        if status == "404":
            raise _not_found(url)
        return body_path.read_bytes(), retrieved
    delay = DELAY
    if check_robots:
        rp = _robots_for(url)
        if not rp.can_fetch(UA, url):
            raise PermissionError(f"robots.txt disallows {url}")
        delay = max(DELAY, float(rp.crawl_delay(UA) or 0.0))
    resp = _get(url, delay)
    retrieved = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if resp.status_code == 404:
        meta_path.write_text(f"{url}\n{retrieved}\n404", encoding="utf-8")
        raise _not_found(url)
    resp.raise_for_status()
    body_path.write_bytes(resp.content)
    meta_path.write_text(f"{url}\n{retrieved}\n{resp.status_code}", encoding="utf-8")
    return resp.content, retrieved
