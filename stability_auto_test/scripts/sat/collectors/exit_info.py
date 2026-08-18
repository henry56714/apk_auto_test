"""ApplicationExitInfo collector (Android 11 / API 30+).

`dumpsys activity exit-info` exposes the historical process-exit records that
the framework itself classified (crash, ANR, low memory, user stop, ...). We
capability-probe the command, record a watermark at run start, and only keep
records created after the watermark so a previous run never pollutes the
current one. When the command is unavailable we return nothing and the pool
falls back to logcat / DropBox / watcher sources (already implemented).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..adb import Adb, AdbError
from ..detection import _name_matches_package

log = logging.getLogger(__name__)

EXIT_INFO_SOURCE = "exit_info"

# ApplicationExitInfo.REASON_* constants.
REASON_CRASHED = "crashed"
REASON_ANR = "anr"
REASON_EXIT_SELF = "exit_self"
REASON_SIGNALED = "signaled"
REASON_LOW_MEMORY = "low_memory"
REASON_DEAD_OBJECT = "dead_object"
REASON_INITIALIZATION_FAILURE = "initialization_failure"
REASON_PERMISSION_CHANGE = "permission_change"
REASON_PACKAGE_STATE_CHANGE = "package_state_change"
REASON_PACKAGE_UPDATED = "package_updated"
REASON_USER_REQUESTED = "user_requested"
REASON_USER_STOPPED = "user_stopped"
REASON_DEPENDENCY_DEATH = "dependency_death"
REASON_EXCESSIVE_RESOURCE_USAGE = "excessive_resource_usage"
REASON_FREEZER = "freezer"
REASON_OTHER = "other"

STABILITY_FAILURE_REASONS = {
    REASON_CRASHED,
    REASON_ANR,
    REASON_LOW_MEMORY,
    REASON_INITIALIZATION_FAILURE,
    REASON_DEPENDENCY_DEATH,
    REASON_EXCESSIVE_RESOURCE_USAGE,
    REASON_SIGNALED,
}


@dataclass
class ExitInfoRecord:
    pid: int
    process: str
    timestamp: str
    exit_reason: str
    exit_subreason: str = ""
    description: str = ""
    status: str = ""
    importance: str = ""
    pss_kb: Optional[int] = None
    rss_kb: Optional[int] = None
    source: str = EXIT_INFO_SOURCE
    confidence: str = "high"
    expected: bool = False
    # Raw device string for reasons this tool does not recognise yet
    # (preserved so future Android versions never get silently mislabelled).
    raw_reason: str = ""
    # Real UTC epoch seconds, computed by the query with the device tz.
    timestamp_epoch: Optional[float] = None

    @property
    def is_stability_failure(self) -> bool:
        return self.exit_reason in STABILITY_FAILURE_REASONS

    @property
    def category(self) -> str:
        if self.raw_reason and self.exit_reason not in ("normal_recycle",):
            # Unrecognised reason: keep the raw value visible, never guess.
            return "unknown"
        if self.exit_reason in (REASON_CRASHED, REASON_SIGNALED):
            return "crash"
        if self.exit_reason == REASON_ANR:
            return "anr"
        if self.exit_reason == REASON_LOW_MEMORY:
            return "low_memory"
        return "process_exit"

    def to_dict(self) -> Dict:
        return {
            "pid": self.pid,
            "process": self.process,
            "timestamp": self.timestamp,
            "exit_reason": self.exit_reason,
            "exit_subreason": self.exit_subreason,
            "description": self.description,
            "status": self.status,
            "importance": self.importance,
            "pss_kb": self.pss_kb,
            "rss_kb": self.rss_kb,
            "source": self.source,
            "confidence": self.confidence,
            "expected": self.expected,
            "raw_reason": self.raw_reason,
            "timestamp_epoch": self.timestamp_epoch,
            "category": self.category,
            "is_stability_failure": self.is_stability_failure,
        }


def _normalize_reason(raw: str) -> Tuple[str, bool]:
    """Normalize an ExitInfo reason string. Returns `(reason, is_known)`.

    Unknown/future Android reasons are preserved in `raw_reason` and reported
    with `category=unknown` instead of being silently mapped (T-L0-010).
    """
    r = (raw or "").strip().upper()
    # API 30+ dumpsys prints e.g. `APP CRASH(EXCEPTION)` / `APP CRASH(NATIVE)`;
    # both are REASON_CRASH with the kind embedded in the string.
    if r.startswith("APP CRASH"):
        return REASON_CRASHED, True
    mapping = {
        "CRASHED": REASON_CRASHED,
        "CRASH": REASON_CRASHED,
        "ANR": REASON_ANR,
        "EXIT_SELF": REASON_EXIT_SELF,
        "SELF_EXIT": REASON_EXIT_SELF,
        "SIGNALED": REASON_SIGNALED,
        "LOW_MEMORY": REASON_LOW_MEMORY,
        "DEAD_OBJECT": REASON_DEAD_OBJECT,
        "INITIALIZATION_FAILURE": REASON_INITIALIZATION_FAILURE,
        "PERMISSION_CHANGE": REASON_PERMISSION_CHANGE,
        "PACKAGE_STATE_CHANGE": REASON_PACKAGE_STATE_CHANGE,
        "PACKAGE_UPDATED": REASON_PACKAGE_UPDATED,
        "USER_REQUESTED": REASON_USER_REQUESTED,
        "USER_STOPPED": REASON_USER_STOPPED,
        "DEPENDENCY_DEATH": REASON_DEPENDENCY_DEATH,
        "DEPENDENCY_DIED": REASON_DEPENDENCY_DEATH,
        "EXCESSIVE_RESOURCE_USAGE": REASON_EXCESSIVE_RESOURCE_USAGE,
        "FREEZER": REASON_FREEZER,
        "LOW MEMORY": REASON_LOW_MEMORY,
        "DEAD OBJECT": REASON_DEAD_OBJECT,
        "DEPENDENCY DEATH": REASON_DEPENDENCY_DEATH,
        "INITIALIZATION FAILURE": REASON_INITIALIZATION_FAILURE,
        "EXCESSIVE RESOURCE USAGE": REASON_EXCESSIVE_RESOURCE_USAGE,
        "PERMISSION CHANGE": REASON_PERMISSION_CHANGE,
        "STATE CHANGE": REASON_PACKAGE_STATE_CHANGE,
        "PACKAGE UPDATED": REASON_PACKAGE_UPDATED,
        "OTHER KILLS BY SYSTEM": "system_recycle",
        "USER REQUESTED": REASON_USER_REQUESTED,
        "USER STOPPED": REASON_USER_STOPPED,
    }
    if r in mapping:
        return mapping[r], True
    if not raw or not raw.strip():
        return REASON_OTHER, False
    return REASON_OTHER, False


def _normalize_cached_recycle(reason: str, proc_state: str) -> str:
    """Map cache/background kills to normal_recycle instead of a failure."""
    if reason == REASON_OTHER and proc_state and "cached" in proc_state.lower():
        return "normal_recycle"
    if reason in (
        REASON_PACKAGE_UPDATED,
        REASON_PERMISSION_CHANGE,
        REASON_PACKAGE_STATE_CHANGE,
        "system_recycle",
    ):
        return "normal_recycle"
    return reason


_RE_PACKAGE = re.compile(r"^[Pp]ackage:\s*(\S+)")
_RE_PROCESS = re.compile(r"^Process:\s*(\S+)\s*\(pid\s*(\d+)\)")
_RE_TIMESTAMP = re.compile(r"^Timestamp:\s*(.+)$")
_RE_REASON = re.compile(r"^Reason:\s*(.+)$")
_RE_SUBREASON = re.compile(r"^Subreason:\s*(.+)$")
_RE_STATUS = re.compile(r"^Status:\s*(.+)$")
_RE_IMPORTANCE = re.compile(r"^Importance:\s*(.+)$")
_RE_PSS = re.compile(r"^PSS:\s*(\d+)\s*kB")
_RE_RSS = re.compile(r"^RSS:\s*(\d+)\s*kB")
_RE_DESCRIPTION = re.compile(r"^Description:\s*(.*)$")
# Android 12+ `dumpsys activity exit-info` format.
_RE_APP_INFO = re.compile(r"^ApplicationExitInfo #\d+:")
_RE_TS_PID = re.compile(r"timestamp=(\S+ \S+)\s+pid=(\d+)")
_RE_PROCESS2 = re.compile(
    # Greedy paren groups: API 30+ reason strings can nest parens, e.g.
    # `(APP CRASH(EXCEPTION))` — `[^)]+` would truncate at the inner `)`.
    r"process=(\S+)\s+reason=(\d+)\s+\((.+)\)"
    r"\s+subreason=(\d+)\s+\((.+)\)\s+status=(\S+)"
)
_RE_MEM2 = re.compile(r"importance=(\d+)\s+pss=([\d.]+)(MB)?\s+rss=([\d.]+)(MB)?")
_RE_DESC2 = re.compile(r"description=(\S+)")


def parse_exit_info_text(text: str) -> List[ExitInfoRecord]:
    """Parse `dumpsys activity exit-info` output."""
    records: List[ExitInfoRecord] = []
    cur: Optional[Dict] = None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        m = _RE_PACKAGE.match(stripped)
        if m:
            if cur:
                records.append(_record_from(cur))
            cur = {"package": m.group(1)}
            continue
        if cur is None:
            continue
        m = _RE_APP_INFO.match(stripped)
        if m:
            if cur.get("process"):
                records.append(_record_from(cur))
                cur = {"package": cur["package"]}
            continue
        m = _RE_TS_PID.search(stripped)
        if m:
            cur["timestamp"] = m.group(1)
            cur["pid"] = int(m.group(2))
            continue
        m = _RE_PROCESS2.match(stripped)
        if m:
            cur["process"] = m.group(1)
            cur["reason"] = m.group(3).strip()
            cur["subreason"] = m.group(5).strip()
            cur["status"] = m.group(6)
            continue
        m = _RE_MEM2.match(stripped)
        if m:
            cur["importance"] = m.group(1)
            pss = float(m.group(2))
            cur["pss_kb"] = int(pss * (1024 if m.group(3) else 1))
            rss = float(m.group(4))
            cur["rss_kb"] = int(rss * (1024 if m.group(5) else 1))
            continue
        m = _RE_DESC2.match(stripped)
        if m:
            cur["description"] = "null" if m.group(1) == "null" else m.group(1)
            continue
        m = _RE_PROCESS.match(stripped)
        if m:
            cur["process"] = m.group(1)
            cur["pid"] = int(m.group(2))
            continue
        m = _RE_TIMESTAMP.match(stripped)
        if m:
            cur["timestamp"] = m.group(1).strip()
            continue
        m = _RE_REASON.match(stripped)
        if m:
            cur["reason"] = m.group(1).strip()
            continue
        m = _RE_SUBREASON.match(stripped)
        if m:
            cur["subreason"] = m.group(1).strip()
            continue
        m = _RE_STATUS.match(stripped)
        if m:
            cur["status"] = m.group(1).strip()
            continue
        m = _RE_IMPORTANCE.match(stripped)
        if m:
            cur["importance"] = m.group(1).strip()
            continue
        m = _RE_PSS.match(stripped)
        if m:
            cur["pss_kb"] = int(m.group(1))
            continue
        m = _RE_RSS.match(stripped)
        if m:
            cur["rss_kb"] = int(m.group(1))
            continue
        m = _RE_DESCRIPTION.match(stripped)
        if m and not cur.get("description"):
            cur["description"] = m.group(1).strip()
    if cur:
        records.append(_record_from(cur))
    return records


def _record_from(cur: Dict) -> ExitInfoRecord:
    raw_reason = (cur.get("reason") or "").strip()
    reason, known = _normalize_reason(raw_reason)
    proc_state = f"{cur.get('status', '')} {cur.get('importance', '')}"
    reason = _normalize_cached_recycle(reason, proc_state)
    process = cur.get("process", "")
    expected = reason in (
        REASON_EXIT_SELF,
        REASON_USER_REQUESTED,
        REASON_USER_STOPPED,
        REASON_PACKAGE_UPDATED,
        "normal_recycle",
    )
    return ExitInfoRecord(
        pid=int(cur.get("pid", 0)),
        process=process,
        timestamp=cur.get("timestamp", ""),
        exit_reason=reason,
        exit_subreason=cur.get("subreason", "") or "",
        description=cur.get("description", "") or "",
        status=cur.get("status", "") or "",
        importance=cur.get("importance", "") or "",
        pss_kb=cur.get("pss_kb"),
        rss_kb=cur.get("rss_kb"),
        expected=expected,
        raw_reason=raw_reason if not known else "",
    )


def _parse_ts_epoch(ts: str, tz_offset_minutes: Optional[int] = None) -> Optional[float]:
    """Parse an ExitInfo timestamp into real UTC epoch seconds.

    dumpsys timestamps are naive device-local strings; when
    `tz_offset_minutes` is given they are converted to real UTC (IMP-05).
    """
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        epoch = dt.timestamp()
        if tz_offset_minutes is not None:
            epoch -= tz_offset_minutes * 60
        return epoch
    except ValueError:
        return None


def _device_tz_offset_minutes(adb: Adb) -> Optional[int]:
    from ..evidence.trace_matcher import query_device_tz_offset_minutes

    try:
        return query_device_tz_offset_minutes(adb)
    except Exception:
        return None


def exit_info_available(adb: Adb) -> bool:
    try:
        r = adb.shell("dumpsys activity exit-info", check=False, timeout=8.0)
    except AdbError:
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def query_exit_info(
    adb: Adb,
    package: str,
    *,
    since_epoch: Optional[float] = None,
    max_records: int = 500,
    available: Optional[bool] = None,
) -> List[ExitInfoRecord]:
    """Query exit-info and filter to this package + post-watermark records.

    `available` lets callers reuse a capability probe taken earlier in the run
    instead of probing again on every query (IMP-03).
    """
    capable = exit_info_available(adb) if available is None else available
    if not capable:
        log.info("exit-info unavailable; falling back to logcat/dropbox/watcher")
        return []
    try:
        r = adb.shell("dumpsys activity exit-info", check=False, timeout=30.0)
    except AdbError:
        # Under emulator load a single dumpsys can exceed the deadline;
        # retry once before giving up (records must never vanish silently).
        try:
            r = adb.shell("dumpsys activity exit-info", check=False, timeout=30.0)
        except AdbError:
            log.warning("exit-info dumpsys failed twice; no records")
            return []
    if r.returncode != 0:
        return []
    base_pkg = package.split(":")[0]
    tz_offset = _device_tz_offset_minutes(adb)
    out = []
    for rec in parse_exit_info_text(r.stdout):
        if not _name_matches_package(rec.process, base_pkg):
            continue
        ts_epoch = _parse_ts_epoch(rec.timestamp, tz_offset)
        rec.timestamp_epoch = ts_epoch
        if since_epoch is not None and (ts_epoch is None or ts_epoch <= since_epoch):
            continue
        out.append(rec)
        if len(out) >= max_records:
            break
    return out


def latest_watermark(
    adb: Adb,
    package: str,
    *,
    available: Optional[bool] = None,
) -> Optional[float]:
    """Return the newest exit-info timestamp (run-start watermark).

    Uses the query-computed real-UTC `timestamp_epoch` (device-tz corrected).
    """
    records = query_exit_info(adb, package, available=available)
    newest: Optional[float] = None
    for rec in records:
        ts = rec.timestamp_epoch
        if ts is None:
            ts = _parse_ts_epoch(rec.timestamp)
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    return newest
