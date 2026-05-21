"""Memory collector parsing `dumpsys meminfo <pid>` output.

Primary metric: TOTAL PSS (the value humans cite for "how much memory does
this process use" on Android). Secondary: the App Summary breakdown so the
HTML report can stack-plot Java/Native/Code/Stack/Graphics.

We parse two sources within the same output, in priority order:
  1) "App Summary" section (present on Android 7+) — labelled, unambiguous
  2) The final "TOTAL" row of the table — works on older Android too

Output formatting differs slightly across versions (column count, label
casing); parser uses keyword anchors instead of fixed offsets.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Dict, Optional

from ..adb import Adb, AdbError


@dataclass
class MemSample:
    timestamp: float
    pid: int
    total_pss_mb: float
    java_heap_pss_mb: float = 0.0
    native_heap_pss_mb: float = 0.0
    code_pss_mb: float = 0.0
    stack_pss_mb: float = 0.0
    graphics_pss_mb: float = 0.0


_TOTAL_PSS_RE = re.compile(r"TOTAL\s*PSS\s*:\s*(\d+)", re.IGNORECASE)
_TOTAL_ROW_RE = re.compile(r"^\s*TOTAL[\s:]+(\d+)", re.IGNORECASE)
_SUMMARY_ROW_RE = re.compile(r"^\s*([A-Za-z][\w \-/]+?):\s+(\d+)")

# Map App Summary labels (lowercase, normalized) → MemSample fields.
_SUMMARY_FIELD = {
    "java_heap": "java_heap_pss_mb",
    "native_heap": "native_heap_pss_mb",
    "code": "code_pss_mb",
    "stack": "stack_pss_mb",
    "graphics": "graphics_pss_mb",
}


def _normalize_label(s: str) -> str:
    return s.strip().lower().replace(" ", "_").replace("-", "_")


def _find_total_pss_kb(lines: list) -> Optional[int]:
    for line in lines:
        m = _TOTAL_PSS_RE.search(line)
        if m:
            return int(m.group(1))
    for line in lines:
        m = _TOTAL_ROW_RE.match(line)
        if m:
            return int(m.group(1))
    return None


def _parse_app_summary(lines: list) -> Dict[str, int]:
    """Return {field_name: pss_kb} for known App Summary rows."""
    in_summary = False
    out: Dict[str, int] = {}
    for line in lines:
        if "App Summary" in line:
            in_summary = True
            continue
        if not in_summary:
            continue
        if _TOTAL_PSS_RE.search(line):
            break
        m = _SUMMARY_ROW_RE.match(line)
        if not m:
            continue
        field = _SUMMARY_FIELD.get(_normalize_label(m.group(1)))
        if field:
            out[field] = int(m.group(2))
    return out


def parse_meminfo(text: str, pid: int = 0) -> Optional[MemSample]:
    """Parse `dumpsys meminfo <pid>` output → MemSample (PSS in MB).

    Returns None only if TOTAL PSS can't be located (output truncated /
    unrecognized format).
    """
    lines = text.splitlines()
    total_kb = _find_total_pss_kb(lines)
    if total_kb is None:
        return None
    breakdown = _parse_app_summary(lines)
    return MemSample(
        timestamp=time.time(),
        pid=pid,
        total_pss_mb=total_kb / 1024.0,
        java_heap_pss_mb=breakdown.get("java_heap_pss_mb", 0) / 1024.0,
        native_heap_pss_mb=breakdown.get("native_heap_pss_mb", 0) / 1024.0,
        code_pss_mb=breakdown.get("code_pss_mb", 0) / 1024.0,
        stack_pss_mb=breakdown.get("stack_pss_mb", 0) / 1024.0,
        graphics_pss_mb=breakdown.get("graphics_pss_mb", 0) / 1024.0,
    )


def sample(
    adb: Adb,
    pid: int,
    *,
    timeout: float = 30.0,
    retries: int = 0,
) -> Optional[MemSample]:
    """One memory snapshot. Returns None on adb failure or unparseable output.

    `dumpsys meminfo` reads `/proc/<pid>/smaps`; for large-PSS processes on a
    memory-pressured device this can take 10-30s, so timeout defaults to 30s
    (vs adb's 10s) and retries default to 0 — retrying the same big smaps read
    won't recover, it just burns mem_interval_sec.
    """
    try:
        r = adb.shell(
            f"dumpsys meminfo {pid}",
            check=False, timeout=timeout, retries=retries,
        )
    except AdbError:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return parse_meminfo(r.stdout, pid=pid)
