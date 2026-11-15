"""Secrets (§18): read from the process environment, else from the gitignored `.env` file.

Values are returned to the caller only; nothing here prints or logs them. `.env.example` lists
the variable names (VENDOR_API_KEY, VENDOR_BASE_URL, IBKR_*, SMTP_*).
"""

from __future__ import annotations

import os
from pathlib import Path


def secret(name: str, env_file: Path | None = Path(".env")) -> str | None:
    """The value of `name`, or None when it is unset or empty."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if env_file is None or not env_file.exists():
        return None
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip().removeprefix("export ").strip() == name:
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            return val or None
    return None
