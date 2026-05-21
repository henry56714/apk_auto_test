"""Small shared helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("T", " ").replace("+00:00", "")


def safe_ts(iso: str) -> str:
    """Convert an ISO timestamp to a filename-safe string (no spaces or colons)."""
    return iso.replace(":", "-").replace(" ", "_")


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(s: str) -> str:
    """Make `s` safe for use as a filename component.

    Process names contain `:` (e.g. `com.foo:remote`) which is invalid on
    some filesystems; replace any non-[A-Za-z0-9._-] with `_`.
    """
    return _UNSAFE.sub("_", s).strip("_") or "unknown"
