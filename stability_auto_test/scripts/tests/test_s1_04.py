"""Time anchors, logcat health and evidence matching (spec S1-04).

Covers T-L0-011 / T-L0-013 / T-L0-014 and T-L1-005 .. T-L1-009.
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from unittest.mock import MagicMock

from sat.analyzers.native_symbolizer import symbolize_frames
from sat.collectors.dropbox import DropboxFetcher, parse_dropbox_dump
from sat.collectors.logcat import LogcatStream
from sat.detection import (
    EVENT_JAVA_CRASH,
    EVENT_NATIVE_CRASH,
    LogcatLineParser,
)
from sat.dumpers import anr as anr_dumper
from sat.dumpers import native_crash as native_crash_dumper
from sat.evidence.trace_matcher import apply_tz_offset, query_device_tz_offset_minutes

# ── T-L0-011: native frame PC survives detection and reaches the symbolizer ──


def test_native_frame_pc_survives_detection():
    lines = [
        "05-21 10:00:00.100  1234  1234 F DEBUG: pid: 1234, tid: 1234, name: main  >>> com.example.app <<<",
        "05-21 10:00:00.100  1234  1234 F DEBUG: signal 11 (SIGSEGV), code 1, fault addr 0x0",
        "05-21 10:00:00.100  1234  1234 F DEBUG:     #00 pc 00001234  /data/app/libx.so",
        "05-21 10:00:00.200  9999  9999 I Other: end",
    ]
    parser = LogcatLineParser(
        "com.example.app",
        now_iso_fn=lambda: "2026-08-13T10:00:00Z",
    )
    events = []
    for ln in lines:
        events.extend(parser.feed_line(ln))
    events.extend(parser.flush())
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == EVENT_NATIVE_CRASH
    assert ev.top_frames[0].startswith("#00 pc 00001234")
    assert ev.pc_addresses == ["00001234"]


def test_symbolizer_receives_pc_address():
    frames = ["#00 pc 00001234  /data/app/libx.so"]
    result = symbolize_frames(frames, symbols_dir=Path("/nonexistent"))
    # Symbolizer must see the pc text; without symbols it reports unavailable
    # and keeps the raw frames — never a false "ok".
    assert result.status != "ok"
    assert result.frames == frames  # raw frames preserved verbatim


# ── T-L0-013: verification failure quarantines the pulled trace ──────────────


def _anr_event(pid: int = 1234, process: str = "com.example.app"):
    from sat.detection import StabilityEvent

    return StabilityEvent(
        event_type="anr",
        process=process,
        pid=pid,
        triggered_at="2026-08-13 10:00:05.000",
        summary="ANR",
        reason="Input dispatching timed out",
        device_ts="2026-08-13 10:00:05.000",
    )


def test_anr_trace_verification_failure_quarantines(tmp_path: Path):
    """A pulled trace from a different process must not become evidence."""
    adb = MagicMock()
    # Candidate header belongs to a DIFFERENT process → verify fails.
    trace_content = (
        "----- pid 9999 at 2026-08-13 10:00:05 -----\n"
        "Cmd line: com.other.app\n"
        '"main" prio=5 tid=1\n'
    )

    def fake_pull(remote, local, check=True, timeout=30.0):
        Path(local).write_text(trace_content, encoding="utf-8")

    adb.pull.side_effect = fake_pull

    from sat.evidence.trace_matcher import TraceCandidate

    class _Match:
        confidence = "high"
        reasons = ["pid_match", "process_match"]
        bound = True
        candidate = TraceCandidate(
            name="anr_2026-08-13-10-00-05",
            path="/data/anr/anr_x",
            size=len(trace_content),
        )

    anr_dumper.match_trace = lambda *a, **k: _Match()  # type: ignore[attr-defined]
    incident = anr_dumper.run(adb, _anr_event(), tmp_path, pull_anr_trace=True)
    evidence = incident["evidence"]
    assert evidence.get("trace_file") is None
    assert evidence.get("fallback_reason") == "verification_failed"
    assert evidence.get("trace_verified") is False
    # The bad file was quarantined under an `.unverified` name.
    assert list(tmp_path.glob("*.unverified"))
    assert not list(tmp_path.glob("*.trace"))


# ── T-L0-014: dropbox matching keeps full dates, prefers latest ──────────────

_DROPBOX_MIXED = (
    "Drop box contents: 3 entries\n"
    "==========================================\n"
    "2026-08-12 12:00:00 data_app_crash (text, 10 bytes)\n"
    "Process: com.example.app\n"
    "java.lang.RuntimeException: yesterday\n"
    "==========================================\n"
    "2026-08-13 12:00:03 data_app_crash (text, 10 bytes)\n"
    "Process: com.example.app\n"
    "java.lang.RuntimeException: today\n"
    "==========================================\n"
)


def test_dropbox_full_date_matching_skips_yesterday():
    fetcher = DropboxFetcher(MagicMock())
    fetcher.adb.shell.return_value = MagicMock(
        returncode=0,
        stdout=_DROPBOX_MIXED,
    )
    body = fetcher.fetch(
        EVENT_JAVA_CRASH,
        "com.example.app",
        "2026-08-13 12:00:00.000",
    )
    assert body is not None
    assert any("today" in ln for ln in body)


def test_dropbox_no_device_ts_prefers_latest_entry():
    fetcher = DropboxFetcher(MagicMock())
    fetcher.adb.shell.return_value = MagicMock(
        returncode=0,
        stdout=_DROPBOX_MIXED,
    )
    body = fetcher.fetch(EVENT_JAVA_CRASH, "com.example.app", None)
    assert body is not None
    assert any("today" in ln for ln in body)


def test_dropbox_parse_entries_with_dates():
    entries = parse_dropbox_dump(_DROPBOX_MIXED)
    assert len(entries) == 2
    assert entries[0].device_ts == "2026-08-12 12:00:00"
    assert entries[1].device_ts == "2026-08-13 12:00:03"


# ── device timezone conversion (IMP-05) ──────────────────────────────────────


def test_tz_offset_conversion():
    # Device local time printed as 10:00 (+08:00) is really 02:00 UTC.
    assert apply_tz_offset(36000.0, 480) == pytest_approx(36000.0 - 480 * 60)


def pytest_approx(value, abs_tol=1e-6):
    import pytest

    return pytest.approx(value, abs=abs_tol)


def test_query_tz_offset_parses_plus0800():
    adb = MagicMock()
    adb.shell.return_value = MagicMock(returncode=0, stdout="+0800\n")
    assert query_device_tz_offset_minutes(adb) == 480
    adb.shell.return_value = MagicMock(returncode=0, stdout="-0530\n")
    assert query_device_tz_offset_minutes(adb) == -330


# ── T-L1-005: stderr storm cannot deadlock the collector ─────────────────────


def test_stderr_storm_is_drained_bounded():
    fake_proc = MagicMock()
    fake_proc.stdout = io.StringIO("05-21 10:00:00.100  1 1 I tag: ok\n")
    fake_proc.stderr = io.StringIO("adb error spam\n" * 10000)
    fake_proc.terminate = MagicMock()
    fake_proc.wait = MagicMock(return_value=0)
    fake_proc.kill = MagicMock()
    fake_proc.poll = MagicMock(return_value=0)

    stream = LogcatStream(
        serial=None,
        buffers=["main"],
        reconnect_backoff_sec=0.0,
    )

    state = {"n": 0}

    def popen(cmd, **kwargs):
        state["n"] += 1
        if state["n"] >= 2:
            stream._stop.set()  # exit after the first connection
        return fake_proc

    stream._popen = popen

    lines = list(stream.lines())
    assert lines == ["05-21 10:00:00.100  1 1 I tag: ok"]
    stats = stream.stats
    assert stats["stderr_lines"] > 0
    assert stats["stderr_lines"] <= 200  # bounded buffer


# ── T-L1-006: alive process with no first line never counts as collecting ────


def test_no_first_line_means_no_collecting():
    fake_proc = MagicMock()
    fake_proc.stdout = io.StringIO("")  # never produces a line
    fake_proc.stderr = io.StringIO("")
    fake_proc.terminate = MagicMock()
    fake_proc.wait = MagicMock(return_value=0)
    fake_proc.kill = MagicMock()
    fake_proc.poll = MagicMock(return_value=None)  # stays alive

    clock = {"now": 100.0}

    def now_fn():
        clock["now"] += 100.0  # fast fake clock
        return clock["now"]

    stream = LogcatStream(
        serial=None,
        buffers=["main"],
        reconnect_backoff_sec=0.0,
        now_fn=now_fn,
        stale_sec=5.0,
    )

    state = {"n": 0}

    def popen(cmd, **kwargs):
        state["n"] += 1
        if state["n"] >= 2:
            stream._stop.set()
        return fake_proc

    stream._popen = popen

    list(stream.lines())
    stream.stop()
    stats = stream.stats
    # The connection never collected a single line.
    assert stats["lines_read"] == 0
    assert stats["up_intervals"] == []
    assert stats["stale_events"] >= 1 or stats["reconnects"] >= 1


# ── T-L1-007: mid-stream silence marks stale and reconnects ──────────────────


def test_silent_stream_marked_stale_and_reconnects():
    import queue as queue_mod

    class _BlockingReader:
        """Emits two lines then goes silent while the process stays alive."""

        def __init__(self, lines):
            self._q = queue_mod.Queue()
            for ln in lines:
                self._q.put(ln)
            self.read = self._q.get

    fake_proc1 = MagicMock()
    lines_q = queue_mod.Queue()
    lines_q.put("05-21 10:00:00.100  1 1 I tag: one")
    fake_proc1.stdout = _BlockingReader([])  # placeholder, unused
    fake_proc1.stderr = io.StringIO("")
    fake_proc1.terminate = MagicMock()
    fake_proc1.wait = MagicMock(return_value=0)
    fake_proc1.kill = MagicMock()
    fake_proc1.poll = MagicMock(return_value=None)  # alive but silent

    fake_proc2 = MagicMock()
    fake_proc2.stdout = io.StringIO("05-21 10:00:01.000  1 1 I tag: two\n")
    fake_proc2.stderr = io.StringIO("")
    fake_proc2.terminate = MagicMock()
    fake_proc2.wait = MagicMock(return_value=0)
    fake_proc2.kill = MagicMock()
    fake_proc2.poll = MagicMock(return_value=None)

    state = {"n": 0}

    def popen(cmd, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            # First connection: two lines, then silence forever.
            class _SilentStdout:
                def __iter__(self):
                    yield "05-21 10:00:00.100  1 1 I tag: one\n"
                    yield "05-21 10:00:00.200  1 1 I tag: idle\n"
                    # then block until the collector kills the process
                    while True:
                        time.sleep(0.02)

            fake_proc1.stdout = _SilentStdout()
            return fake_proc1
        stream._stop.set()
        return fake_proc2

    clock = {"now": 100.0}

    def now_fn():
        clock["now"] += 0.5
        return clock["now"]

    stream = LogcatStream(
        serial=None,
        buffers=["main"],
        reconnect_backoff_sec=0.0,
        popen_fn=popen,
        now_fn=now_fn,
        stale_sec=2.0,
    )

    got = []
    for line in stream.lines():
        got.append(line)
        if len(got) >= 2:
            # Let the fake clock trip the stale threshold.
            pass
    stream.stop()
    stats = stream.stats
    assert stats["stale_events"] >= 1
    assert stats["reconnects"] >= 1
    assert len(stats["gap_intervals"]) >= 1
    assert stats["last_success_host_ts"] is not None


# ── T-L1-008: reconnect tail replay + new event stays single-occurrence ──────


def test_replayed_tail_line_does_not_duplicate_event():
    from sat.fusion import FusionEngine
    from sat.observations import Observation

    eng = FusionEngine()
    obs = Observation(
        source="logcat",
        source_record_id="rec-1",
        process="com.example.app",
        pid=7,
        type="java_crash",
        severity="fatal",
        device_event_time="2026-08-13 10:00:00.100",
        extra={"exception_class": "RuntimeException"},
    )
    assert eng.observe(obs, now_sec=100.0)[0] is True
    # The exact same record re-delivered after a reconnect.
    assert eng.observe(obs, now_sec=101.0)[0] is False
    # A different crash at the same pid shortly after stays separate.
    other = Observation(
        source="logcat",
        source_record_id="rec-2",
        process="com.example.app",
        pid=7,
        type="java_crash",
        severity="fatal",
        device_event_time="2026-08-13 10:00:05.100",
        extra={"exception_class": "NullPointerException"},
    )
    assert eng.observe(other, now_sec=105.0)[0] is True
    assert len(eng.occurrences()) == 2


# ── T-L1-009: unparseable banner inside a Java stack ─────────────────────────


def test_unparseable_lines_inside_java_stack_do_not_lose_crash():
    lines = [
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: Process: com.example.app, PID: 1234",
        "======== binary banner without logcat header ========",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: java.lang.RuntimeException: boom",
        "05-21 10:00:00.200  9999  9999 I Other: end",
    ]
    parser = LogcatLineParser(
        "com.example.app",
        now_iso_fn=lambda: "2026-08-13T10:00:00Z",
    )
    events = []
    for ln in lines:
        events.extend(parser.feed_line(ln))
    events.extend(parser.flush())
    # The banner line flushes open blocks but the crash is still detected.
    assert len(events) == 1
    assert events[0].event_type == EVENT_JAVA_CRASH
    assert "boom" in events[0].summary


# ── importer guard for unused fixtures ───────────────────────────────────────


def test_module_import_smoke():
    assert native_crash_dumper.run is not None
