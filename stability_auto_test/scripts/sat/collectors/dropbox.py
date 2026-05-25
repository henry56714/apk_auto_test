"""Dropbox fetcher — on-demand evidence collector for crash/ANR events.

When logcat detects a stability event, the event's dumper calls
`DropboxFetcher.fetch()` to retrieve the matching dropbox entry body as
supplementary evidence. The body is written alongside the logcat slice so
analysts have the full Android crash report available.

This replaces the previous polling approach where the dropbox was used as a
secondary detection source. Detection now runs exclusively through logcat;
dropbox is evidence-only.

Dropbox format (Android 10+):

    Drop box contents: N entries
    ==========================================
    2026-05-21 10:00:00 data_app_crash (text, 1234 bytes)
    Process: com.example.app
    PID: 1234
    java.lang.RuntimeException: foo
        at com.example.MainActivity.onResume(MainActivity.java:42)
    ...
    ==========================================
    2026-05-21 10:05:00 SYSTEM_TOMBSTONE (compressed text, ...)
    pid: 1234, tid: 5678, name: Thread-1 >>> com.example.app <<<
    signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0
    ...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from ..adb import Adb, AdbError
from ..detection import (
    EVENT_ANR,
    EVENT_JAVA_CRASH,
    EVENT_NATIVE_CRASH,
    _name_matches_package,
    _parse_device_ts_sec,
)

log = logging.getLogger(__name__)

ENTRY_HEAD_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<tag>[A-Za-z0-9_]+)\s+\((?P<meta>[^)]*)\)"
)

DROPBOX_TAG_TO_TYPE = {
    "data_app_crash": EVENT_JAVA_CRASH,
    "system_app_crash": EVENT_JAVA_CRASH,
    "system_server_crash": EVENT_JAVA_CRASH,
    "data_app_native_crash": EVENT_NATIVE_CRASH,
    "system_app_native_crash": EVENT_NATIVE_CRASH,
    "SYSTEM_TOMBSTONE": EVENT_NATIVE_CRASH,
    "data_app_anr": EVENT_ANR,
    "system_app_anr": EVENT_ANR,
    "system_server_anr": EVENT_ANR,
}


@dataclass
class _Entry:
    device_ts: str
    tag: str
    body: List[str]


def parse_dropbox_dump(text: str) -> List[_Entry]:
    """Split a `dumpsys dropbox --print` blob into entries.

    Entries are separated by the `====` banner line; the first line of each
    entry is the timestamp + tag header.
    """
    entries: List[_Entry] = []
    cur: Optional[_Entry] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            if cur is not None:
                cur.body.append("")
            continue
        if set(line) == {"="} and len(line) >= 8:
            if cur is not None and (cur.tag or cur.body):
                entries.append(cur)
                cur = None
            continue
        m = ENTRY_HEAD_RE.match(line)
        if m and cur is None:
            cur = _Entry(device_ts=m.group("ts"), tag=m.group("tag"), body=[])
            continue
        if cur is not None:
            cur.body.append(line)
    if cur is not None and (cur.tag or cur.body):
        entries.append(cur)
    return entries


def _process_from_body(body: List[str]) -> Optional[str]:
    """Find the target process name in a dropbox entry body (best-effort)."""
    for line in body[:50]:
        line = line.strip()
        if line.startswith("Process:"):
            return line.split(":", 1)[1].strip().split(",")[0].strip()
        if ">>>" in line and "<<<" in line:
            try:
                return line.split(">>>", 1)[1].split("<<<", 1)[0].strip()
            except Exception:  # noqa: BLE001
                pass
    return None


class DropboxFetcher:
    """Pull a matching dropbox entry on demand for evidence collection.

    Called by dumpers after logcat detects an event. Runs `dumpsys dropbox
    --print`, finds the entry that best matches (event_type, process,
    device_ts) within `window_sec`, and returns its raw body lines.

    Returns None when the device is unreachable, the entry is not found, or
    the device timestamp is too far from any stored entry.
    """

    def __init__(self, adb: Adb) -> None:
        self.adb = adb

    def fetch(
        self,
        event_type: str,
        process: str,
        device_ts: Optional[str] = None,
        window_sec: float = 60.0,
    ) -> Optional[List[str]]:
        """Return body lines of the best-matching dropbox entry, or None."""
        try:
            r = self.adb.shell("dumpsys dropbox --print", check=False, timeout=30.0)
        except AdbError as e:
            log.warning("dropbox fetch failed: %s", e)
            return None
        if r.returncode != 0:
            return None

        entries = parse_dropbox_dump(r.stdout)
        relevant_tags = {tag for tag, et in DROPBOX_TAG_TO_TYPE.items() if et == event_type}
        base_pkg = process.split(":")[0]
        event_dev = _parse_device_ts_sec(device_ts)

        best: Optional[_Entry] = None
        best_delta = float("inf")

        for entry in entries:
            if entry.tag not in relevant_tags:
                continue
            p = _process_from_body(entry.body)
            if not p or not _name_matches_package(p, base_pkg):
                continue
            if event_dev is not None:
                entry_dev = _parse_device_ts_sec(entry.device_ts)
                if entry_dev is not None:
                    delta = abs(event_dev - entry_dev)
                    delta = min(delta, 86400.0 - delta)
                    if delta > window_sec:
                        continue
                    if delta < best_delta:
                        best_delta = delta
                        best = entry
                    continue
            # No device_ts to compare: take the first matching entry.
            if best is None:
                best = entry

        return list(best.body) if best is not None else None
