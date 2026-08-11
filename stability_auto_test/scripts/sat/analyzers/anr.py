"""ANR main-thread root-cause classification."""

from __future__ import annotations

import re
from typing import Dict, List

_LOCK_PATTERNS = (
    "waiting to lock",
    "held by thread",
    "monitor",
    "synchronized",
    "Object.wait",
    "park",
)
_BINDER_PATTERNS = (
    "binder_thread_read",
    "IPCThreadState",
    "android.os.Binder",
    "BinderProxy",
    "BinderInternal",
    "nativeTransact",
)
_IO_PATTERNS = (
    "epollWait",
    "MessageQueue.next",
    "Looper.loop",
    "nativeRead",
    "socketRead",
    "InputStream",
    "FileInputStream",
    "OkHttp",
    "readFully",
)


def _main_thread_lines(trace_lines: List[str]) -> List[str]:
    out: List[str] = []
    in_main = False
    for line in trace_lines:
        if re.search(r'"main"\s+prio=', line):
            in_main = True
            out.append(line)
            continue
        if in_main:
            if line.startswith('"') or re.match(r"\s*\"[^\"]+\"\s+prio=", line):
                break
            out.append(line)
    return out


def analyze_anr_trace(trace_lines: List[str]) -> Dict:
    """Return a machine-readable diagnosis for an ANR trace."""
    lines = _main_thread_lines(trace_lines)
    if not lines:
        lines = trace_lines[:60]
    text = "\n".join(lines)

    category = "unknown"
    confidence = "low"
    supporting: List[str] = []
    for pattern in _LOCK_PATTERNS:
        if pattern in text:
            category = "lock_contention"
            confidence = "high"
            supporting = [ln.strip() for ln in lines if pattern in ln][:5]
            break
    if category == "unknown":
        for pattern in _BINDER_PATTERNS:
            if pattern in text:
                category = "binder_wait"
                confidence = "medium"
                supporting = [ln.strip() for ln in lines if pattern in ln][:5]
                break
    if category == "unknown":
        for pattern in _IO_PATTERNS:
            if pattern in text:
                category = "io_wait"
                confidence = "medium"
                supporting = [ln.strip() for ln in lines if pattern in ln][:5]
                break

    summary = {
        "lock_contention": "main thread blocked waiting for a lock",
        "binder_wait": "main thread blocked in a Binder call",
        "io_wait": "main thread blocked on I/O / message queue",
        "unknown": "could not classify main-thread wait reason",
    }[category]
    return {
        "category": category,
        "summary": summary,
        "confidence": confidence,
        "supporting_frames": supporting,
    }
