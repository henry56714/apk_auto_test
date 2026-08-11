"""Confidence-scored matching of tombstone / ANR trace files to events.

Simply grabbing the newest file in `/data/tombstones/` or `/data/anr/` is wrong
when multiple processes crash close together. This module lists candidate
files, parses their headers for PID/process, scores each candidate against the
event (PID, process/package, device-time proximity, new/updated-after-event)
and only binds when the score reaches a threshold. Pulled traces are verified
again from the local file header.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from ..adb import Adb, AdbError
from ..detection import StabilityEvent, _name_matches_package

log = logging.getLogger(__name__)

MATCH_THRESHOLD = 70
TIME_WINDOW_SEC = 60.0
NEW_FILE_SKEW_SEC = 5.0


@dataclass
class TraceCandidate:
    name: str
    path: str
    size: int
    mtime: Optional[float] = None
    pid: Optional[int] = None
    process: Optional[str] = None


@dataclass
class TraceMatchResult:
    candidate: Optional[TraceCandidate]
    score: int
    confidence: str
    reasons: List[str] = field(default_factory=list)
    threshold: int = MATCH_THRESHOLD

    @property
    def bound(self) -> bool:
        return self.candidate is not None and self.score >= self.threshold


def _parse_mtime(raw_date: str, raw_time: str, year: int) -> Optional[float]:
    date_part = raw_date if re.match(r"^\d{4}-", raw_date) else f"{year}-{raw_date}"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            ts = datetime.strptime(
                f"{date_part} {raw_time}", fmt,
            ).replace(tzinfo=timezone.utc)
            return ts.timestamp()
        except ValueError:
            continue
    return None


def parse_ls_listing(text: str, year: int) -> List[TraceCandidate]:
    """Parse `ls -ln` output (toybox Android format)."""
    out: List[TraceCandidate] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("total"):
            continue
        parts = line.split()
        # -rw-r----- 1 root root 106058 2026-08-10 20:00 tombstone_00
        if len(parts) < 8:
            continue
        try:
            size = int(parts[4])
        except ValueError:
            continue
        name = parts[-1]
        mtime = _parse_mtime(parts[5], parts[6], year)
        out.append(TraceCandidate(
            name=name,
            path=name,  # callers fix the directory
            size=size,
            mtime=mtime,
        ))
    return out


def _parse_pid_process(header: str) -> Tuple[Optional[int], Optional[str]]:
    pid: Optional[int] = None
    process: Optional[str] = None
    for line in header.splitlines()[:50]:
        m = re.search(r"\bpid:?\s*(\d+)", line)
        if m and pid is None:
            pid = int(m.group(1))
        m2 = re.search(r">>>\s*(\S+)\s*<<<", line)
        if m2:
            process = m2.group(1)
        m3 = re.match(r"Cmd line:\s*(\S+)", line.strip())
        if m3 and process is None:
            process = m3.group(1)
    return pid, process


def list_trace_candidates(
    adb: Adb,
    remote_dir: str,
    *,
    event: StabilityEvent,
    year: int,
) -> List[TraceCandidate]:
    """List candidate files and best-effort parse their headers."""
    try:
        r = adb.shell(f"ls -ln {remote_dir} 2>/dev/null", check=False, timeout=5.0)
    except AdbError:
        return []
    if r.returncode != 0:
        return []
    candidates = parse_ls_listing(r.stdout, year)
    for cand in candidates:
        cand.path = f"{remote_dir.rstrip('/')}/{cand.name}"
        header = _read_header(adb, cand.path)
        if header:
            pid, process = _parse_pid_process(header)
            cand.pid = pid
            cand.process = process
    return candidates


def _read_header(adb: Adb, path: str) -> str:
    try:
        r = adb.shell(
            f"head -n 20 {path} 2>/dev/null",
            check=False,
            timeout=5.0,
        )
    except AdbError:
        return ""
    return r.stdout if r.returncode == 0 else ""


def score_candidate(
    cand: TraceCandidate,
    event: StabilityEvent,
    event_time_sec: Optional[float],
    *,
    time_window_sec: float = TIME_WINDOW_SEC,
) -> Tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []
    base_pkg = (event.process or "").split(":")[0]

    if cand.pid is not None and event.pid and cand.pid == event.pid:
        score += 40
        reasons.append("pid_match")
    if cand.process and base_pkg and _name_matches_package(cand.process, base_pkg):
        score += 20
        reasons.append("process_match")
    if cand.mtime is not None and event_time_sec is not None:
        if abs(cand.mtime - event_time_sec) <= time_window_sec:
            score += 20
            reasons.append("time_proximity")
        if cand.mtime >= event_time_sec - NEW_FILE_SKEW_SEC:
            score += 10
            reasons.append("new_or_updated_after_event")
    return score, reasons


def _event_time_sec(event: StabilityEvent) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(event.triggered_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def match_trace(
    adb: Adb,
    event: StabilityEvent,
    remote_dir: str,
    *,
    year: Optional[int] = None,
    threshold: int = MATCH_THRESHOLD,
) -> TraceMatchResult:
    """Score candidates and return the best match (if any)."""
    if year is None:
        year = _event_year(event) or datetime.now(timezone.utc).year
    candidates = list_trace_candidates(
        adb, remote_dir, event=event, year=year,
    )
    if not candidates:
        return TraceMatchResult(None, 0, "none", ["no candidates or permission denied"])

    event_time = _event_time_sec(event)
    best: Optional[TraceCandidate] = None
    best_score = -1
    best_reasons: List[str] = []
    for cand in candidates:
        score, reasons = score_candidate(cand, event, event_time)
        if score > best_score:
            best, best_score, best_reasons = cand, score, reasons

    if best is None or best_score < threshold:
        return TraceMatchResult(
            best, max(0, best_score), "low",
            best_reasons + ["no_confident_match"],
            threshold=threshold,
        )
    confidence = "high" if best_score >= 80 else "medium"
    return TraceMatchResult(
        best, best_score, confidence, best_reasons, threshold=threshold,
    )


def _event_year(event: StabilityEvent) -> Optional[int]:
    try:
        return datetime.fromisoformat(
            event.triggered_at.replace("Z", "+00:00"),
        ).year
    except (ValueError, TypeError):
        return None


def verify_local_trace(
    local_path: Path,
    event: StabilityEvent,
) -> Tuple[bool, str]:
    """Re-parse the pulled file header and verify PID + process."""
    try:
        header = Path(local_path).read_text(
            encoding="utf-8", errors="replace",
        )[:4000]
    except OSError:
        return False, "unreadable local trace"
    pid, process = _parse_pid_process(header)
    base_pkg = (event.process or "").split(":")[0]
    if pid is not None and event.pid and pid != event.pid:
        return False, f"pid mismatch: file={pid} event={event.pid}"
    if process and base_pkg and not _name_matches_package(process, base_pkg):
        return False, f"process mismatch: file={process} event={base_pkg}"
    return True, "verified"
