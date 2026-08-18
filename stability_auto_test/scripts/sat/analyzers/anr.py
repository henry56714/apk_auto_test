"""ANR type + main-thread root-cause classification (spec S2-03 / IMP-14).

Rules of evidence:

- an idle main thread (MessageQueue.next / nativePollOnce / epollWait /
  Looper.loop) can mean "the trace was taken too late" — it must NEVER be
  classified as `io_wait` on those frames alone (T-L0-015);
- lock contention is only claimed when the main thread shows
  `waiting to lock` (+ a holder when the trace names one);
- Binder blocking requires Binder-transaction frames;
- the ANR *type* (input dispatch / broadcast / service / ...) comes from the
  system reason string, not from the trace.

Every diagnosis carries a confidence and the supporting frames it is based
on, so consumers can audit the reasoning.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# ── ANR type classification from the system reason ───────────────────────────

_TYPE_RULES = (
    (
        "input_dispatch",
        (r"input dispatching timed out", r"no focused window", r"waiting to send non-key event"),
    ),
    ("broadcast", (r"broadcast of intent", r"broadcast")),
    ("service", (r"executing service", r"service")),
    ("content_provider", (r"contentprovider", r"content provider")),
    ("job", (r"jobscheduler", r"executing job")),
    ("fgs_timeout", (r"fgs", r"foreground service")),
)

_ANR_TYPE_MAP = {
    "input_dispatch": "input dispatch",
    "broadcast": "broadcast",
    "service": "service",
    "content_provider": "content provider",
    "job": "job",
    "fgs_timeout": "foreground service",
}


def classify_anr_type(reason: Optional[str]) -> Dict:
    """Classify the ANR type from the system reason string."""
    reason = reason or ""
    for anr_type, patterns in _TYPE_RULES:
        for pattern in patterns:
            if re.search(pattern, reason, re.IGNORECASE):
                return {
                    "type": anr_type,
                    "display": _ANR_TYPE_MAP[anr_type],
                    "reason": reason,
                }
    return {"type": "unknown", "display": "unknown", "reason": reason}


# ── trace parsing helpers ────────────────────────────────────────────────────

_THREAD_HEAD_RE = re.compile(
    r'^\s*"(?P<name>[^"]+)"\s+prio=\d+\s+(tid=\d+\s+)?'
    r"(?P<state>[A-Za-z]+)"
)

_LOCK_PATTERNS = (
    "waiting to lock",
    "held by thread",
    "monitor",
    "Object.wait",
)

_HELD_BY_RE = re.compile(r"held by thread (\d+)")

_BINDER_PATTERNS = (
    "binder_thread_read",
    "IPCThreadState",
    "BinderProxy.transact",
    "nativeTransact",
    "transactNative",
)

_IO_PATTERNS = (
    "FileInputStream",
    "SocketInputStream",
    "readFully",
    "OkHttp",
    "libcore.io",
    "RandomAccessFile",
)

_IDLE_PATTERNS = (
    "MessageQueue.next",
    "nativePollOnce",
    "epollWait",
    "Looper.loop",
)


def _parse_threads(trace_lines: List[str]) -> Dict[str, List[str]]:
    """Split a trace into per-thread frames: {name: [state_line, frames...]}."""
    threads: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in trace_lines:
        m = _THREAD_HEAD_RE.match(line)
        if m:
            current = m.group("name")
            threads.setdefault(current, []).append(line)
            continue
        if current is not None:
            threads[current].append(line)
    return threads


def _main_thread(threads: Dict[str, List[str]]) -> List[str]:
    return threads.get("main", [])


def _holder_thread(threads: Dict[str, List[str]], main_lines: List[str]) -> Optional[Dict]:
    """Find the lock-holder thread when the trace names one."""
    for line in main_lines:
        m = _HELD_BY_RE.search(line)
        if m:
            tid = int(m.group(1))
            for name, frames in threads.items():
                for frame in frames:
                    if f"tid={tid}" in frame:
                        return {
                            "name": name,
                            "tid": tid,
                            "frames": [f.strip() for f in frames[:8]],
                        }
    return None


def analyze_anr_trace(
    trace_lines: List[str],
    reason: Optional[str] = None,
) -> Dict:
    """Return a machine-readable ANR diagnosis (type + root cause + evidence)."""
    threads = _parse_threads(trace_lines)
    main_lines = _main_thread(threads)
    if not main_lines and trace_lines:
        main_lines = list(trace_lines[:80])
    text = "\n".join(main_lines)

    type_info = classify_anr_type(reason)

    category = "unknown"
    confidence = "low"
    supporting: List[str] = []
    holder: Optional[Dict] = None

    # 1. Lock contention — must show `waiting to lock` on the main thread.
    if "waiting to lock" in text:
        category = "lock_contention"
        confidence = "high"
        supporting = [
            ln.strip() for ln in main_lines if "waiting to lock" in ln or "held by thread" in ln
        ][:6]
        holder = _holder_thread(threads, main_lines)

    # 2. Binder blocking — Binder transaction frames on the main thread.
    if category == "unknown":
        binder_hits = [ln.strip() for ln in main_lines if any(p in ln for p in _BINDER_PATTERNS)]
        if binder_hits:
            category = "binder_wait"
            confidence = "medium"
            supporting = binder_hits[:6]

    # 3. Busy loop / CPU-bound: main thread state is RUNNABLE and the tail is
    #    dominated by repeating app frames (not epoll/poll waits).
    if category == "unknown":
        state_line = main_lines[0] if main_lines else ""
        idle_hits = [ln for ln in main_lines if any(p in ln for p in _IDLE_PATTERNS)]
        app_frames = [ln.strip() for ln in main_lines if re.search(r"^\s*at\s+", ln)]
        if (
            app_frames
            and not idle_hits
            and ("runnable" in state_line.lower() or "native" in state_line.lower())
        ):
            category = "busy_loop"
            confidence = "medium"
            supporting = app_frames[:6]

    # 4. I/O blocking — real I/O frames only; idle-frame patterns are
    #    explicitly excluded (IMP-14).
    if category == "unknown":
        io_hits = [ln.strip() for ln in main_lines if any(p in ln for p in _IO_PATTERNS)]
        if io_hits:
            category = "io_wait"
            confidence = "medium"
            supporting = io_hits[:6]

    # 5. Idle main thread → the trace was taken too late (or the app was
    #    genuinely idle); NOT an I/O diagnosis.
    if category == "unknown":
        idle_hits = [ln.strip() for ln in main_lines if any(p in ln for p in _IDLE_PATTERNS)]
        if idle_hits:
            category = "late_or_non_actionable_trace"
            confidence = "low"
            supporting = idle_hits[:4]

    summary = {
        "lock_contention": "main thread blocked waiting for a lock",
        "binder_wait": "main thread blocked in a Binder call",
        "busy_loop": "main thread runnable (CPU-bound or busy loop)",
        "io_wait": "main thread blocked on I/O",
        "late_or_non_actionable_trace": (
            "main thread idle at trace time; the trace may be late (no actionable root cause)"
        ),
        "unknown": "could not classify main-thread wait reason",
    }[category]

    result: Dict = {
        "category": category,
        "summary": summary,
        "confidence": confidence,
        "supporting_frames": supporting,
        "anr_type": type_info,
    }
    if holder is not None:
        result["lock_holder"] = holder
    return result
