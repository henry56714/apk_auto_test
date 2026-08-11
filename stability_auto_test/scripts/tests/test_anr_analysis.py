from __future__ import annotations

from sat.analyzers.anr import analyze_anr_trace


def _trace(frames: list) -> list:
    return [
        '"main" prio=5 tid=1 Native',
        "  at android.os.MessageQueue.nativePollOnce(Native Method)",
        *frames,
    ]


def test_lock_contention_detected():
    diag = analyze_anr_trace(_trace([
        "  at com.example.Main.run(Main.java:1)",
        "  - waiting to lock <0x1234> (a java.lang.Object)",
    ]))
    assert diag["category"] == "lock_contention"
    assert diag["confidence"] == "high"
    assert any("waiting to lock" in f for f in diag["supporting_frames"])


def test_binder_wait_detected():
    diag = analyze_anr_trace(_trace([
        "  at android.os.BinderProxy.transactNative(Native Method)",
        "  at android.os.BinderProxy.transact(BinderProxy.java:100)",
    ]))
    assert diag["category"] == "binder_wait"


def test_io_wait_detected():
    diag = analyze_anr_trace(_trace([
        "  at android.os.MessageQueue.next(MessageQueue.java:330)",
        "  at android.os.Looper.loop(Looper.java:200)",
    ]))
    assert diag["category"] == "io_wait"


def test_unknown_when_no_pattern():
    diag = analyze_anr_trace(_trace([
        "  at com.example.DeepLoop.recurse(DeepLoop.java:7)",
    ]))
    assert diag["category"] == "unknown"
    assert diag["confidence"] == "low"
