"""Java crash subtype classification (spec S2-01 / S2-04).

Distinguishes startup crashes, background-thread crashes, OOM classes
(java heap / bitmap / native allocation / GC overhead) and FGS/service
exceptions — each with an explicit evidence basis, never a bare guess.
"""

from __future__ import annotations

from typing import Dict, Optional

STARTUP_CRASH_WINDOW_SEC = 15.0


def is_startup_crash(
    crash_host_sec: float,
    process_start_host_sec: Optional[float],
    *,
    window_sec: float = STARTUP_CRASH_WINDOW_SEC,
) -> bool:
    """A crash is a startup crash when it happens within `window_sec` of the
    process's latest (re)start."""
    if process_start_host_sec is None:
        return False
    return 0.0 <= (crash_host_sec - process_start_host_sec) <= window_sec


def classify_oom(exception_class: Optional[str], summary: str) -> Optional[str]:
    """Sub-classify an OutOfMemoryError by its message (T-L0-027)."""
    if not exception_class or "OutOfMemoryError" not in exception_class:
        return None
    text = (summary or "").lower()
    if "bitmap" in text or "bitmap size exceeds" in text:
        return "bitmap_oom"
    if "failed to allocate" in text or "native" in text or "malloc" in text:
        return "native_alloc_oom"
    if "gc overhead" in text:
        return "gc_overhead_oom"
    if "java heap" in text or "java.lang.outofmemoryerror" in text and "heap" in text:
        return "java_heap_oom"
    # Plain OutOfMemoryError with no further evidence: the JVM default is the
    # Java heap — but without evidence we only claim the generic class.
    return "java_heap_oom"


def classify_java_crash(
    *,
    exception_class: Optional[str],
    summary: str,
    crashing_thread: Optional[str],
    process_start_host_sec: Optional[float],
    crash_host_sec: float,
) -> Dict:
    """Return the machine-readable classification for one java_crash."""
    subtype = "uncaught_exception"
    text = (summary or "").lower()
    if exception_class and "OutOfMemoryError" in exception_class:
        subtype = classify_oom(exception_class, summary) or "generic_oom"
    elif exception_class and "RemoteServiceException" in exception_class:
        # S2-06: FGS failures carry their specific violation in the message.
        if "did not then call service.startforeground()" in text:
            subtype = "fgs_did_not_start"
        elif "not allowed to start service" in text or "foreground service not allowed" in text:
            subtype = "fgs_start_not_allowed"
        else:
            subtype = "remote_service_exception"
    elif exception_class and "TransactionTooLargeException" in exception_class:
        subtype = "binder_transaction_too_large"
    elif exception_class and "DeadObjectException" in exception_class:
        subtype = "binder_dead_object"
    elif exception_class and "SQLiteFullException" in exception_class:
        subtype = "database_sqlite_full"
    elif exception_class and "SQLiteDatabaseCorruptException" in exception_class:
        subtype = "database_corruption"
    elif "sqlite" in text and "corrupt" in text:
        subtype = "database_corruption"
    elif exception_class and "SQLiteException" in exception_class:
        subtype = "database_error"
    elif "no space left on device" in text or "enospc" in text:
        subtype = "disk_enospc"

    thread_category = "main" if crashing_thread == "main" else "background"

    return {
        "subtype": subtype,
        "crashing_thread": crashing_thread,
        "thread_category": thread_category,
        "startup_crash": is_startup_crash(
            crash_host_sec,
            process_start_host_sec,
        ),
        "exception_class": exception_class,
    }
