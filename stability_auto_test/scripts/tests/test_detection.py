from __future__ import annotations

from pathlib import Path

import pytest

from sat.detection import (
    EVENT_ANR,
    EVENT_JAVA_CRASH,
    EVENT_NATIVE_CRASH,
    EVENT_PROCESS_DEATH,
    Deduper,
    LogcatLineParser,
    StabilityEvent,
)

FIXTURES = Path(__file__).parent / "fixtures"
PACKAGE = "com.example.app"


def _parse_all(parser: LogcatLineParser, text: str):
    events = []
    for line in text.splitlines():
        events.extend(parser.feed_line(line))
    events.extend(parser.flush())
    return events


def _parser():
    return LogcatLineParser(PACKAGE, now_iso_fn=lambda: "2026-05-21 10:00:00.000")


def test_java_crash_extracts_exception_and_frames():
    text = (FIXTURES / "logcat_java_crash.txt").read_text(encoding="utf-8")
    events = _parse_all(_parser(), text)
    crashes = [e for e in events if e.event_type == EVENT_JAVA_CRASH]
    assert len(crashes) == 1
    e = crashes[0]
    assert e.process == PACKAGE
    assert e.pid == 1234
    assert e.exception_class == "java.lang.NullPointerException"
    assert any("MainActivity.onResume" in f for f in e.top_frames)
    assert e.severity == "fatal"


def test_native_crash_extracts_signal_and_proc():
    text = (FIXTURES / "logcat_native_crash.txt").read_text(encoding="utf-8")
    events = _parse_all(_parser(), text)
    crashes = [e for e in events if e.event_type == EVENT_NATIVE_CRASH]
    assert len(crashes) == 1
    e = crashes[0]
    assert e.process == PACKAGE
    assert e.pid == 1234
    assert e.signal == "SIGSEGV"
    assert e.fault_addr == "0x0"
    assert any("libc.so" in f for f in e.top_frames)


def test_anr_from_main_buffer():
    text = (FIXTURES / "logcat_anr.txt").read_text(encoding="utf-8")
    events = _parse_all(_parser(), text)
    anrs = [e for e in events if e.event_type == EVENT_ANR]
    assert len(anrs) == 1
    a = anrs[0]
    assert a.process == PACKAGE
    assert a.pid == 1234
    assert "Input dispatching" in (a.reason or "")


def test_events_buffer_proc_died_kill_and_anr():
    text = (FIXTURES / "logcat_events_buffer.txt").read_text(encoding="utf-8")
    events = _parse_all(_parser(), text)
    types = [e.event_type for e in events]
    # am_proc_died + am_kill → process_death (2); am_anr → anr (1)
    assert types.count(EVENT_PROCESS_DEATH) == 2
    assert types.count(EVENT_ANR) == 1
    anr = [e for e in events if e.event_type == EVENT_ANR][0]
    assert anr.pid == 1236
    assert "Input dispatching" in (anr.reason or "")


def test_other_package_is_dropped():
    parser = _parser()
    line = (
        "05-21 10:00:00.200  9999  9999 E AndroidRuntime: FATAL EXCEPTION: main"
    )
    parser.feed_line(line)
    parser.feed_line(
        "05-21 10:00:00.200  9999  9999 E AndroidRuntime: Process: com.other.app, PID: 9999"
    )
    parser.feed_line(
        "05-21 10:00:00.200  9999  9999 E AndroidRuntime: java.lang.RuntimeException: nope"
    )
    parser.feed_line(
        "05-21 10:00:00.200  9999  9999 E AndroidRuntime: 	at com.other.X(X.java:1)"
    )
    events = parser.feed_line(
        "05-21 10:00:00.300  1000  1000 I DifferentTag: x"
    )
    events += parser.flush()
    assert events == []


def test_subprocess_name_matches_package_prefix():
    parser = _parser()
    parser.feed_line(
        "05-21 10:00:00.100   500   600 E ActivityManager: ANR in com.example.app:remote"
    )
    parser.feed_line(
        "05-21 10:00:00.100   500   600 E ActivityManager: PID: 2222"
    )
    parser.feed_line(
        "05-21 10:00:00.100   500   600 E ActivityManager: Reason: foo"
    )
    events = parser.feed_line(
        "05-21 10:00:00.300  1000  1000 I X: bye"
    )
    events += parser.flush()
    anrs = [e for e in events if e.event_type == EVENT_ANR]
    assert len(anrs) == 1
    assert anrs[0].process == "com.example.app:remote"


def test_deduper_suppresses_within_window():
    # Events without device_ts use the host-time fallback window.
    d = Deduper(window_sec=5.0)
    ev = StabilityEvent(
        event_type="java_crash", process="x", pid=1,
        triggered_at="...", source="logcat",
    )
    assert d.observe(ev, now_sec=100.0) is True
    assert d.observe(ev, now_sec=101.0) is False
    assert d.observe(ev, now_sec=104.5) is False
    # Window from anchor (5s after 100 = 105). Anchor expires at >105.
    assert d.observe(ev, now_sec=106.0) is True


def test_deduper_separates_keys():
    d = Deduper(window_sec=5.0)
    a = StabilityEvent("java_crash", "x", 1, "t")
    b = StabilityEvent("java_crash", "x", 2, "t")
    c = StabilityEvent("anr", "x", 1, "t")
    assert d.observe(a, 0.0) is True
    assert d.observe(b, 0.0) is True
    assert d.observe(c, 0.0) is True
    assert d.observe(a, 1.0) is False


def test_deduper_cross_source_dedup_on_device_ts():
    """logcat detects a crash; dropbox reports the same crash 31 s later.

    With only a 5 s host-time window the dropbox event would slip through.
    The device_ts window (10 s default) should suppress it because both events
    have nearly identical device timestamps (< 2 s apart).
    """
    d = Deduper(window_sec=5.0, device_ts_window_sec=10.0)

    ev_logcat = StabilityEvent(
        event_type="java_crash", process="com.example.app", pid=1234,
        triggered_at="2026-05-24 12:42:43.384",
        device_ts="05-24 12:42:43.384",   # logcat format (no year)
    )
    ev_dropbox = StabilityEvent(
        event_type="java_crash", process="com.example.app", pid=1234,
        triggered_at="2026-05-24 12:43:14.000",   # host: 31 s later
        device_ts="2026-05-24 12:42:44",           # dropbox format; Δdevice ≈ 1 s
    )

    assert d.observe(ev_logcat, now_sec=0.0) is True    # logcat: emitted
    assert d.observe(ev_dropbox, now_sec=31.0) is False  # dropbox: suppressed ✓


def test_deduper_allows_genuinely_different_crashes_same_pid():
    """Two crashes with the same pid but device_ts 60 s apart are distinct."""
    d = Deduper(window_sec=5.0, device_ts_window_sec=10.0)

    ev1 = StabilityEvent(
        event_type="anr", process="com.example.app", pid=1234,
        triggered_at="t1", device_ts="05-24 12:00:00.000",
    )
    ev2 = StabilityEvent(
        event_type="anr", process="com.example.app", pid=1234,
        triggered_at="t2", device_ts="05-24 12:01:00.000",  # 60 s later
    )

    assert d.observe(ev1, now_sec=0.0) is True
    assert d.observe(ev2, now_sec=60.0) is True   # different device_ts → emitted ✓


def test_deduper_midnight_rollover():
    """device_ts spanning midnight (23:59:xx → 00:00:xx) still deduplicates."""
    d = Deduper(window_sec=5.0, device_ts_window_sec=10.0)

    ev_logcat = StabilityEvent(
        event_type="java_crash", process="com.example.app", pid=9,
        triggered_at="t1", device_ts="05-24 23:59:58.000",
    )
    ev_dropbox = StabilityEvent(
        event_type="java_crash", process="com.example.app", pid=9,
        triggered_at="t2", device_ts="2026-05-25 00:00:01",  # 3 s later, next day
    )

    assert d.observe(ev_logcat, now_sec=0.0) is True
    # delta via rollover = |86398 - 1| → min(86397, 3) = 3 ≤ 10 → suppressed
    assert d.observe(ev_dropbox, now_sec=31.0) is False


def test_flush_emits_pending_block():
    parser = _parser()
    parser.feed_line("05-21 10:00:00.100  1234 1234 E AndroidRuntime: FATAL EXCEPTION: main")
    parser.feed_line("05-21 10:00:00.100  1234 1234 E AndroidRuntime: Process: com.example.app, PID: 1234")
    parser.feed_line("05-21 10:00:00.100  1234 1234 E AndroidRuntime: java.lang.RuntimeException: x")
    # No terminator line — depends on flush() to emit.
    immediate = parser.feed_line("05-21 10:00:00.100  1234 1234 E AndroidRuntime: 	at X.y(X.java:1)")
    assert immediate == []
    events = parser.flush()
    assert len(events) == 1
    assert events[0].event_type == EVENT_JAVA_CRASH
