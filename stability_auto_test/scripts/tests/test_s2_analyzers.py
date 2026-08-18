"""S2 analyzer tests: ANR root cause (T-L0-015..017), OOM taxonomy
(T-L0-027), startup crash and crash_loop grouping (S2-01)."""

from __future__ import annotations

from sat.analyzers.anr import analyze_anr_trace, classify_anr_type
from sat.analyzers.fingerprint import group_incidents
from sat.analyzers.java_crash import classify_java_crash, classify_oom

# ── T-L0-016: lock wait with holder ──────────────────────────────────────────

_LOCK_TRACE = [
    '"main" prio=5 tid=1 Blocked',
    "  at com.example.Main.onClick(Main.java:9)",
    "  - waiting to lock <0x0a1b2c3d> (a java.lang.Object) held by thread 7",
    "",
    '"sat-lock-holder" prio=5 tid=7 Sleeping',
    "  at java.lang.Thread.sleep(Native Method)",
    "  at com.example.LockHolder.hold(LockHolder.java:12)",
]


def test_lock_contention_with_holder():
    diag = analyze_anr_trace(_LOCK_TRACE, reason="Input dispatching timed out")
    assert diag["category"] == "lock_contention"
    assert diag["confidence"] == "high"
    holder = diag.get("lock_holder")
    assert holder is not None
    assert holder["name"] == "sat-lock-holder"
    assert holder["tid"] == 7
    assert diag["anr_type"]["type"] == "input_dispatch"


# ── T-L0-017: binder wait with transaction info ──────────────────────────────

_BINDER_TRACE = [
    '"main" prio=5 tid=1 Blocked',
    "  at android.os.BinderProxy.transactNative(Native Method)",
    "  at android.os.BinderProxy.transact(BinderProxy.java:510)",
    "  at android.app.IActivityManager$Stub$Proxy.bindService(IActivityManager.java:1234)",
]


def test_binder_wait_not_io():
    diag = analyze_anr_trace(
        _BINDER_TRACE,
        reason="executing service com.example.app/.MyService",
    )
    assert diag["category"] == "binder_wait"
    assert diag["anr_type"]["type"] == "service"


def test_anr_type_classification():
    assert classify_anr_type("Input dispatching timed out")["type"] == "input_dispatch"
    assert (
        classify_anr_type("Broadcast of Intent { act=android.intent.action.MAIN }")["type"]
        == "broadcast"
    )
    assert classify_anr_type("executing service com.x/.S")["type"] == "service"
    assert classify_anr_type("weird future reason")["type"] == "unknown"


# ── T-L0-015: idle trace is late/non-actionable (IMP-14) ─────────────────────


def test_idle_trace_never_io_wait():
    diag = analyze_anr_trace(
        [
            '"main" prio=5 tid=1 Native',
            "  at android.os.MessageQueue.nativePollOnce(Native Method)",
            "  at android.os.MessageQueue.next(MessageQueue.java:330)",
            "  at android.os.Looper.loop(Looper.java:200)",
        ],
        reason="Input dispatching timed out",
    )
    assert diag["category"] == "late_or_non_actionable_trace"
    assert diag["category"] != "io_wait"


# ── T-L0-027: OOM taxonomy ───────────────────────────────────────────────────


def test_oom_taxonomy():
    assert (
        classify_oom("java.lang.OutOfMemoryError", "bitmap size exceeds VM budget") == "bitmap_oom"
    )
    assert (
        classify_oom("java.lang.OutOfMemoryError", "Failed to allocate a 4096 byte allocation")
        == "native_alloc_oom"
    )
    assert (
        classify_oom("java.lang.OutOfMemoryError", "GC overhead limit exceeded")
        == "gc_overhead_oom"
    )
    assert classify_oom("java.lang.OutOfMemoryError", "plain") == "java_heap_oom"
    assert classify_oom("java.lang.RuntimeException", "x") is None


# ── S2-01: startup crash + crashing thread ───────────────────────────────────


def test_startup_crash_classification():
    c = classify_java_crash(
        exception_class="java.lang.RuntimeException",
        summary="boom",
        crashing_thread="main",
        process_start_host_sec=100.0,
        crash_host_sec=105.0,
    )
    assert c["startup_crash"] is True
    assert c["thread_category"] == "main"
    assert c["subtype"] == "uncaught_exception"


def test_not_startup_crash_when_old_process():
    c = classify_java_crash(
        exception_class="java.lang.RuntimeException",
        summary="boom",
        crashing_thread="main",
        process_start_host_sec=100.0,
        crash_host_sec=500.0,
    )
    assert c["startup_crash"] is False


def test_background_thread_classification():
    c = classify_java_crash(
        exception_class="java.lang.RuntimeException",
        summary="boom",
        crashing_thread="sat-bg-crasher",
        process_start_host_sec=None,
        crash_host_sec=100.0,
    )
    assert c["thread_category"] == "background"


def test_oom_crash_subtype():
    c = classify_java_crash(
        exception_class="java.lang.OutOfMemoryError",
        summary="Failed to allocate a 4096 byte allocation",
        crashing_thread="main",
        process_start_host_sec=None,
        crash_host_sec=100.0,
    )
    assert c["subtype"] == "native_alloc_oom"


# ── S2-01: crash_loop grouping ───────────────────────────────────────────────


def _inc(pid: int, exc: str = "java.lang.RuntimeException") -> dict:
    return {
        "id": f"incident-{pid}",
        "type": "java_crash",
        "process": "com.example.app",
        "pid": pid,
        "triggered_at": f"2026-08-13 10:00:{pid % 60:02d}.000",
        "summary": f"{exc}: boom",
        "evidence": {
            "exception_class": exc,
            "top_frames": ["at com.example.A.b(A.java:1)"],
        },
    }


def test_crash_loop_group_detected():
    groups = group_incidents([_inc(1), _inc(2), _inc(3)])
    assert len(groups) == 1
    assert groups[0]["kind"] == "crash_loop"
    assert groups[0]["occurrence_count"] == 3


def test_single_crash_is_not_crash_loop():
    groups = group_incidents([_inc(1)])
    assert groups[0]["kind"] == "occurrence_group"


def test_two_startup_crashes_form_loop():
    a, b = _inc(1), _inc(2)
    a["evidence"]["startup_crash"] = True
    b["evidence"]["startup_crash"] = True
    groups = group_incidents([a, b])
    assert groups[0]["kind"] == "crash_loop"


def test_different_exceptions_group_separately():
    groups = group_incidents(
        [
            _inc(1, "java.lang.RuntimeException"),
            _inc(2, "java.lang.NullPointerException"),
        ]
    )
    assert len(groups) == 2
    assert all(g["kind"] == "occurrence_group" for g in groups)


# ── T-L1-026: DropBox crash storm → bounded dumpsys calls (IMP-20) ───────────

def test_dropbox_storm_cache_bounds_dumpsys_calls():
    from unittest.mock import MagicMock

    from sat.collectors.dropbox import CachingDropboxFetcher

    adb = MagicMock()
    adb.shell.return_value = MagicMock(
        returncode=0,
        stdout=(
            "Drop box contents: 1 entries\n"
            "==========================================\n"
            "2026-08-13 12:00:00 data_app_crash (text, 10 bytes)\n"
            "Process: com.example.app\n"
            "java.lang.RuntimeException: boom\n"
            "==========================================\n"
        ),
    )
    fetcher = CachingDropboxFetcher(adb, ttl_sec=30.0)
    # 100 incident requests for the same tag must not produce 100 dumpsys.
    for i in range(100):
        body = fetcher.fetch(
            "java_crash", "com.example.app", "2026-08-13 12:00:00.000",
        )
        assert body is not None
    assert fetcher.dumpsys_calls <= 2, f"dumpsys called {fetcher.dumpsys_calls} times"


def test_dropbox_cache_expires_after_ttl():
    from unittest.mock import MagicMock

    from sat.collectors.dropbox import CachingDropboxFetcher

    adb = MagicMock()
    adb.shell.return_value = MagicMock(
        returncode=0,
        stdout=(
            "Drop box contents: 1 entries\n"
            "==========================================\n"
            "2026-08-13 12:00:00 data_app_crash (text, 10 bytes)\n"
            "Process: com.example.app\n"
            "==========================================\n"
        ),
    )
    fetcher = CachingDropboxFetcher(adb, ttl_sec=0.01)
    assert fetcher.fetch("java_crash", "com.example.app") is not None
    import time as _time

    _time.sleep(0.02)
    assert fetcher.fetch("java_crash", "com.example.app") is not None
    assert fetcher.dumpsys_calls == 2


# ── T-L0-026: cause chain parsing ────────────────────────────────────────────

def test_cause_chain_captured():
    from sat.detection import LogcatLineParser

    lines = [
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: Process: com.example.app, PID: 1234",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: java.lang.RuntimeException: outer",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: Caused by: java.lang.IllegalStateException: inner",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: Caused by: java.io.IOException: deepest",
        "05-21 10:00:00.200  9999  9999 I OtherTag: end",
    ]
    parser = LogcatLineParser(
        "com.example.app", now_iso_fn=lambda: "2026-08-13T10:00:00Z",
    )
    events = []
    for ln in lines:
        events.extend(parser.feed_line(ln))
    events.extend(parser.flush())
    assert len(events) == 1
    ev = events[0]
    assert ev.crashing_thread == "main"
    assert ev.cause_chain == [
        "java.lang.IllegalStateException",
        "java.io.IOException",
    ]
