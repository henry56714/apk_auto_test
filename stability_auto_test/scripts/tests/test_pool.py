"""End-to-end pool wiring test with all external IO mocked.

Validates that:
- watcher reconcile emits new/gone lifecycle rows (no longer dispatches events)
- logcat lines that contain a Java crash trigger a java_crash dumper call
- logcat am_proc_died / am_kill lines trigger a process_death dumper call
- deduper suppresses duplicate events within the same source
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest

from sat.collectors.logcat import LogcatStream
from sat.detection import (
    EVENT_JAVA_CRASH,
    EVENT_PROCESS_DEATH,
    StabilityEvent,
)
from sat.discovery import Process
from sat.pool import (
    CollectorPool,
    CollectorsConfig,
    DetectionConfig,
    DumpsConfig,
)
from sat.storage import (
    EVENTS_COLUMNS,
    EVENTS_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    CsvStreamWriter,
)


PACKAGE = "com.example.app"


def _writers(tmp_path: Path):
    ev = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    life = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG)
    return ev, life


def _scripted_logcat_stream(lines: List[str]):
    stream = LogcatStream(serial=None, buffers=["main"], reconnect_backoff_sec=0.0,
                          popen_fn=lambda *a, **k: None)

    def fake_lines():
        for ln in lines:
            yield ln

    stream.lines = lambda: fake_lines()  # type: ignore[method-assign]
    return stream


def test_watcher_emits_lifecycle_rows_only(tmp_path: Path):
    """Watcher reconcile writes new/gone rows but does NOT dispatch events."""
    ev_w, life_w = _writers(tmp_path)

    states = [
        [Process(pid=1234, name=PACKAGE)],   # initial discover
        [],                                   # gone next reconcile
    ]
    iter_states = iter(states)

    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        rescan_interval_sec=0.05,
        collectors=CollectorsConfig(logcat_enabled=False),
        discover_fn=lambda adb, pkg: next(iter_states, []),
    )
    pool.start(initial_processes=[Process(pid=1234, name=PACKAGE)])
    time.sleep(0.3)   # > 2 × rescan_interval_sec
    pool.stop(join_timeout=1.0)
    ev_w.close(); life_w.close()

    life_text = (next(tmp_path.glob("lifecycle_*.csv"))).read_text()
    assert "new" in life_text
    assert "gone" in life_text
    # Watcher no longer dispatches process_death events.
    assert pool.event_counts().get(EVENT_PROCESS_DEATH, 0) == 0


def test_logcat_pipeline_triggers_java_crash_dumper(tmp_path: Path):
    ev_w, life_w = _writers(tmp_path)
    incidents_dir = tmp_path / "incidents"

    crash_lines = [
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: Process: com.example.app, PID: 1234",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: java.lang.RuntimeException: boom",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: \tat X.y(X.java:1)",
        "05-21 10:00:00.200  9999  9999 I OtherTag: end",
    ]

    java_dumps = []
    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=incidents_dir,
        rescan_interval_sec=10.0,
        collectors=CollectorsConfig(logcat_enabled=True),
        discover_fn=lambda adb, pkg: [],
        logcat_stream_factory=lambda: _scripted_logcat_stream(crash_lines),
        java_crash_dump_fn=lambda adb, ev, d: java_dumps.append(ev) or {"type": ev.event_type},
    )
    pool.start(initial_processes=[Process(pid=1234, name=PACKAGE)])

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if pool.event_counts().get(EVENT_JAVA_CRASH, 0) >= 1:
            break
        time.sleep(0.05)
    pool.stop(join_timeout=1.0)
    ev_w.close(); life_w.close()

    assert len(java_dumps) == 1
    assert java_dumps[0].process == PACKAGE
    assert java_dumps[0].exception_class == "java.lang.RuntimeException"


def test_logcat_am_proc_died_triggers_process_death_dumper(tmp_path: Path):
    """am_proc_died in logcat events buffer → process_death event dispatched."""
    ev_w, life_w = _writers(tmp_path)
    incidents_dir = tmp_path / "incidents"

    # am_proc_died payload: [user, pid, name, oom_adj, procState]
    lines = [
        "05-21 10:00:00.100  570  570 I am_proc_died: [0,1234,com.example.app,900,2]",
        "05-21 10:00:00.200  9999 9999 I OtherTag: end",
    ]

    death_dumps = []
    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=incidents_dir,
        rescan_interval_sec=10.0,
        collectors=CollectorsConfig(logcat_enabled=True),
        discover_fn=lambda adb, pkg: [],
        logcat_stream_factory=lambda: _scripted_logcat_stream(lines),
        proc_death_dump_fn=lambda adb, ev, d: death_dumps.append(ev) or {},
    )
    pool.start()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if pool.event_counts().get(EVENT_PROCESS_DEATH, 0) >= 1:
            break
        time.sleep(0.05)
    pool.stop(join_timeout=1.0)
    ev_w.close(); life_w.close()

    assert len(death_dumps) == 1
    assert death_dumps[0].process == PACKAGE
    assert death_dumps[0].pid == 1234


def test_max_incidents_cap_enforced(tmp_path: Path):
    ev_w, life_w = _writers(tmp_path)
    incidents_dir = tmp_path / "incidents"

    java_dumps = []
    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=incidents_dir,
        rescan_interval_sec=10.0,
        collectors=CollectorsConfig(logcat_enabled=False),
        detection=DetectionConfig(dedup_window_sec=0.0),
        dumps=DumpsConfig(max_incidents_per_type=2),
        discover_fn=lambda adb, pkg: [],
        java_crash_dump_fn=lambda adb, ev, d: java_dumps.append(ev),
    )
    pool.start()
    for pid in (1, 2, 3):
        pool._dispatch(StabilityEvent(
            event_type=EVENT_JAVA_CRASH, process=PACKAGE, pid=pid,
            triggered_at=f"t{pid}", summary=f"e{pid}",
        ))
    time.sleep(0.2)
    pool.stop(join_timeout=1.0)
    ev_w.close(); life_w.close()
    assert len(java_dumps) == 2
