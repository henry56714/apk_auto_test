"""Thread-safe time-based logcat ring buffer for pre/post-context slices.

Every logcat line is appended with its host receive time, device timestamp and
the parsed pid/tid/tag/level. The buffer evicts by age, line count and byte
size so a log storm cannot grow memory without bound.

When an incident is dispatched, the pool records an anchor and waits up to
``post_context_sec`` for trailing lines, then snapshots
``[event_time - pre_context_sec, event_time + post_context_sec]``. The slice is
written to a per-incident ``*_context.txt`` file with explicit
``PRE_CONTEXT`` / ``EVENT_BLOCK`` / ``POST_CONTEXT`` sections.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional


@dataclass
class LogEntry:
    host_ts: float
    device_ts: Optional[str]
    pid: Optional[int]
    tid: Optional[int]
    tag: str
    level: str
    raw: str


@dataclass
class ContextSlice:
    pre_entries: List[LogEntry] = field(default_factory=list)
    post_entries: List[LogEntry] = field(default_factory=list)
    pre_context_sec_actual: float = 0.0
    post_context_sec_actual: float = 0.0
    pre_missing_reason: Optional[str] = None
    post_missing_reason: Optional[str] = None
    dropped_by_cap_count: int = 0


class LogcatContextBuffer:
    """Ring buffer with age / line-count / byte-size eviction."""

    def __init__(
        self,
        *,
        retention_sec: float = 600.0,
        max_entries: int = 5000,
        max_bytes: int = 4 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.retention_sec = float(retention_sec)
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1, int(max_bytes))
        self._clock = clock
        self._entries: Deque[LogEntry] = deque()
        self._bytes = 0
        self._dropped_by_cap = 0
        self._evicted_by_age = 0
        self._lock = threading.Lock()

    def append(self, entry: LogEntry) -> None:
        with self._lock:
            self._entries.append(entry)
            self._bytes += len(entry.raw.encode("utf-8", errors="replace")) + 64
            self._evict_locked(entry.host_ts)

    def _evict_locked(self, now_ts: float) -> None:
        cutoff = now_ts - self.retention_sec
        while self._entries:
            oldest = self._entries[0]
            if oldest.host_ts >= cutoff and len(self._entries) <= self.max_entries \
                    and self._bytes <= self.max_bytes:
                break
            self._entries.popleft()
            self._bytes -= len(oldest.raw.encode("utf-8", errors="replace")) + 64
            if oldest.host_ts < cutoff:
                self._evicted_by_age += 1
            else:
                self._dropped_by_cap += 1

    def snapshot(
        self,
        event_ts: float,
        *,
        pre_sec: float,
        post_sec: float,
        now_ts: Optional[float] = None,
    ) -> ContextSlice:
        """Return entries within ``[event_ts - pre_sec, event_ts + post_sec]``."""
        now_ts = self._clock() if now_ts is None else now_ts
        with self._lock:
            self._evict_locked(now_ts)
            pre_start = event_ts - max(0.0, float(pre_sec))
            post_end = event_ts + max(0.0, float(post_sec))
            pre_entries = [
                e for e in self._entries if pre_start <= e.host_ts < event_ts
            ]
            post_entries = [
                e for e in self._entries if event_ts < e.host_ts <= post_end
            ]

        pre_actual = 0.0
        if pre_entries:
            pre_actual = event_ts - pre_entries[0].host_ts
        post_actual = 0.0
        if post_entries:
            post_actual = max(0.0, post_entries[-1].host_ts - event_ts)
        elif post_sec > 0:
            post_actual = min(float(post_sec), max(0.0, now_ts - event_ts))

        return ContextSlice(
            pre_entries=pre_entries,
            post_entries=post_entries,
            pre_context_sec_actual=round(pre_actual, 3),
            post_context_sec_actual=round(post_actual, 3),
            dropped_by_cap_count=self._dropped_by_cap,
        )

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._bytes,
                "dropped_by_cap": self._dropped_by_cap,
                "evicted_by_age": self._evicted_by_age,
            }


def format_context_slice(
    event_lines: List[str],
    slice_: ContextSlice,
) -> str:
    """Render a slice as an independent, human-readable text file."""
    out = ["=== PRE_CONTEXT ==="]
    out.extend(e.raw for e in slice_.pre_entries)
    out.append("=== EVENT_BLOCK ===")
    out.extend(event_lines)
    out.append("=== POST_CONTEXT ===")
    out.extend(e.raw for e in slice_.post_entries)
    out.append("")
    return "\n".join(out)
