"""End-to-end pool wiring test with all external IO mocked.

Validates that:
- watcher reconcile emits new/gone lifecycle rows (no longer dispatches events)
- logcat lines that contain a Java crash trigger a java_crash dumper call
- logcat am_proc_died / am_kill lines trigger a process_death dumper call
- deduper suppresses duplicate events within the same source
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

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
    stream = LogcatStream(
        serial=None, buffers=["main"], reconnect_backoff_sec=0.0, popen_fn=lambda *a, **k: None
    )

    def fake_lines():
        for ln in lines:
            yield ln

    stream.lines = lambda: fake_lines()  # type: ignore[method-assign]
    return stream


def test_watcher_emits_lifecycle_rows_only(tmp_path: Path):
    """Watcher reconcile writes new/gone rows but does NOT dispatch events."""
    ev_w, life_w = _writers(tmp_path)

    states = [
        [Process(pid=1234, name=PACKAGE)],  # initial discover
        [],  # gone next reconcile
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
    time.sleep(0.3)  # > 2 × rescan_interval_sec
    pool.stop(join_timeout=1.0)
    ev_w.close()
    life_w.close()

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
    ev_w.close()
    life_w.close()

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
    ev_w.close()
    life_w.close()

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
        pool._dispatch(
            StabilityEvent(
                event_type=EVENT_JAVA_CRASH,
                process=PACKAGE,
                pid=pid,
                triggered_at=f"t{pid}",
                summary=f"e{pid}",
            )
        )
    time.sleep(0.2)
    pool.stop(join_timeout=1.0)
    ev_w.close()
    life_w.close()
    assert len(java_dumps) == 2
    assert pool.dropped_by_cap_count() == 1


def test_stop_drains_in_flight_dump(tmp_path: Path):
    """stop() must wait for a slow dumper so the incident is not lost."""
    ev_w, life_w = _writers(tmp_path)
    incidents_dir = tmp_path / "incidents"
    ran = []

    def slow_dump(adb, ev, d):
        time.sleep(0.4)
        ran.append(ev)
        return {"type": ev.event_type}

    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=incidents_dir,
        collectors=CollectorsConfig(logcat_enabled=False),
        discover_fn=lambda adb, pkg: [],
        java_crash_dump_fn=slow_dump,
    )
    pool.start()
    pool._dispatch(
        StabilityEvent(
            event_type=EVENT_JAVA_CRASH,
            process=PACKAGE,
            pid=1,
            triggered_at="t1",
            summary="e1",
        )
    )
    pool.stop(join_timeout=1.0, dump_shutdown_timeout_sec=5.0)
    ev_w.close()
    life_w.close()

    assert len(ran) == 1
    states = pool.dump_task_states()
    assert states["persisted"] == 1
    assert states["queued"] == 0
    assert states["running"] == 0
    assert not [t for t in threading.enumerate() if t.name.startswith("dump-")]


def test_dump_failure_marks_failed_and_stop_does_not_deadlock(tmp_path: Path):
    ev_w, life_w = _writers(tmp_path)
    incidents_dir = tmp_path / "incidents"

    def failing_dump(adb, ev, d):
        raise RuntimeError("dumper exploded")

    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=incidents_dir,
        collectors=CollectorsConfig(logcat_enabled=False),
        discover_fn=lambda adb, pkg: [],
        java_crash_dump_fn=failing_dump,
    )
    pool.start()
    pool._dispatch(
        StabilityEvent(
            event_type=EVENT_JAVA_CRASH,
            process=PACKAGE,
            pid=2,
            triggered_at="t2",
            summary="e2",
        )
    )
    pool.stop(join_timeout=1.0, dump_shutdown_timeout_sec=2.0)
    ev_w.close()
    life_w.close()

    assert pool.dump_task_states()["failed"] == 1
    assert not [t for t in threading.enumerate() if t.name.startswith("dump-")]


def test_dump_shutdown_timeout_marks_timed_out_and_returns(tmp_path: Path):
    ev_w, life_w = _writers(tmp_path)
    incidents_dir = tmp_path / "incidents"
    release = threading.Event()

    def blocked_dump(adb, ev, d):
        release.wait(10.0)
        return {"type": ev.event_type}

    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=incidents_dir,
        collectors=CollectorsConfig(logcat_enabled=False),
        dumps=DumpsConfig(max_concurrent=1, dump_shutdown_timeout_sec=0.1),
        discover_fn=lambda adb, pkg: [],
        java_crash_dump_fn=blocked_dump,
    )
    pool.start()
    pool._dispatch(
        StabilityEvent(
            event_type=EVENT_JAVA_CRASH,
            process=PACKAGE,
            pid=3,
            triggered_at="t3",
            summary="e3",
        )
    )

    started = time.monotonic()
    pool.stop(join_timeout=0.2, dump_shutdown_timeout_sec=0.1)
    elapsed = time.monotonic() - started
    ev_w.close()
    life_w.close()

    assert elapsed < 2.0
    assert pool.dump_task_states()["timed_out"] == 1

    # Release the worker and confirm it exits without leaving a dump thread.
    release.set()
    deadline = time.monotonic() + 2.0
    while any(t.name.startswith("dump-") for t in threading.enumerate()):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    assert not [t for t in threading.enumerate() if t.name.startswith("dump-")]


def test_logcat_pre_and_post_context_written_for_incident(tmp_path: Path):
    ev_w, life_w = _writers(tmp_path)
    incidents_dir = tmp_path / "incidents"
    pre_lines = [f"05-21 09:59:{i:02d}.000  1234  1234 I App: pre-{i}" for i in range(20)]
    crash_lines = [
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: Process: com.example.app, PID: 1234",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: java.lang.RuntimeException: boom",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: \tat X.y(X.java:1)",
    ]
    post_lines = [f"05-21 10:00:0{i}.000  1234  1234 I App: post-{i}" for i in range(10)]
    lines = (
        pre_lines
        + crash_lines
        + post_lines
        + [
            "05-21 10:00:11.000  9999  9999 I OtherTag: end",
        ]
    )

    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=incidents_dir,
        rescan_interval_sec=10.0,
        collectors=CollectorsConfig(logcat_enabled=True),
        dumps=DumpsConfig(pre_context_sec=30.0, post_context_sec=0.05),
        discover_fn=lambda adb, pkg: [],
        logcat_stream_factory=lambda: _scripted_logcat_stream(lines),
    )
    pool.start(initial_processes=[Process(pid=1234, name=PACKAGE)])
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if pool.event_counts().get(EVENT_JAVA_CRASH, 0) >= 1:
            break
        time.sleep(0.05)
    time.sleep(0.3)  # let the post-context wait + dumper finish
    pool.stop(join_timeout=1.0, dump_shutdown_timeout_sec=3.0)
    ev_w.close()
    life_w.close()

    context_files = sorted(incidents_dir.glob("*_context.txt"))
    assert len(context_files) == 1
    text = context_files[0].read_text(encoding="utf-8")
    assert "=== PRE_CONTEXT ===" in text
    assert "=== EVENT_BLOCK ===" in text
    assert "=== POST_CONTEXT ===" in text
    assert "pre-0" in text and "pre-19" in text
    assert "post-0" in text and "post-9" in text
    incident = json.loads(next(incidents_dir.glob("*.json")).read_text())
    assert incident["evidence"]["context_file"] == context_files[0].name
    assert incident["evidence"]["pre_context_sec_actual"] >= 0


def test_stop_early_marks_post_context_incomplete(tmp_path: Path):
    ev_w, life_w = _writers(tmp_path)
    incidents_dir = tmp_path / "incidents"
    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=incidents_dir,
        collectors=CollectorsConfig(logcat_enabled=False),
        dumps=DumpsConfig(
            pre_context_sec=30.0,
            post_context_sec=10.0,
            dump_shutdown_timeout_sec=2.0,
        ),
        discover_fn=lambda adb, pkg: [],
    )
    pool.start()
    pool._dispatch(
        StabilityEvent(
            event_type=EVENT_JAVA_CRASH,
            process=PACKAGE,
            pid=1234,
            triggered_at="2026-05-21 10:00:00.000",
            summary="boom",
            raw_lines=["raw"],
        )
    )
    pool.stop(join_timeout=0.5, dump_shutdown_timeout_sec=2.0)
    ev_w.close()
    life_w.close()

    incident = json.loads(next(incidents_dir.glob("*.json")).read_text())
    assert incident["evidence"]["post_context_missing_reason"] == "run_stopped_early"
    assert incident["evidence"]["post_context_sec_actual"] < 10.0


def test_pre_context_zero_has_empty_pre_section(tmp_path: Path):
    ev_w, life_w = _writers(tmp_path)
    incidents_dir = tmp_path / "incidents"
    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=incidents_dir,
        collectors=CollectorsConfig(logcat_enabled=False),
        dumps=DumpsConfig(
            pre_context_sec=0.0,
            post_context_sec=0.05,
            dump_shutdown_timeout_sec=2.0,
        ),
        discover_fn=lambda adb, pkg: [],
    )
    pool.start()
    pool._dispatch(
        StabilityEvent(
            event_type=EVENT_JAVA_CRASH,
            process=PACKAGE,
            pid=1234,
            triggered_at="2026-05-21 10:00:00.000",
            summary="boom",
            raw_lines=["raw"],
        )
    )
    pool.stop(join_timeout=0.5, dump_shutdown_timeout_sec=2.0)
    ev_w.close()
    life_w.close()

    context_file = next(incidents_dir.glob("*_context.txt"))
    text = context_file.read_text(encoding="utf-8")
    assert "=== PRE_CONTEXT ===\n=== EVENT_BLOCK ===" in text
    incident = json.loads(next(incidents_dir.glob("*.json")).read_text())
    assert incident["evidence"]["pre_context_sec_actual"] == 0.0


def test_logcat_stats_preserved_after_stop(tmp_path: Path):
    """Coverage needs logcat stats even after the collector thread exits."""
    ev_w, life_w = _writers(tmp_path)

    class StatsStream:
        def __init__(self):
            self._stats = {
                "lines_read": 3,
                "reconnects": 1,
                "read_failures": 0,
                "last_device_ts": "05-21 10:00:00.003",
                "started_at": 1.0,
                "ended_at": 2.0,
                "up_intervals": [[1.0, 2.0]],
                "gap_intervals": [],
                "backlog_peak": 0,
            }

        def stop(self):
            pass

        def lines(self):
            yield "05-21 10:00:00.001  1 1 I x: a"
            yield "05-21 10:00:00.002  1 1 I x: b"
            yield "05-21 10:00:00.003  1 1 I x: c"

        @property
        def stats(self):
            return self._stats

    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        collectors=CollectorsConfig(logcat_enabled=True),
        discover_fn=lambda adb, pkg: [],
        logcat_stream_factory=StatsStream,
    )
    pool.start()
    pool.stop(join_timeout=1.0)
    ev_w.close()
    life_w.close()

    logcat = pool.collector_status()["logcat"]
    assert logcat["lines_read"] == 3
    assert logcat["up_intervals"] == [[1.0, 2.0]]


def test_workload_restart_exit_marked_expected(tmp_path: Path):
    # IMP-08: only an action window declaring the fault id marks the exit
    # expected — a bare manifest must NOT blanket-mark all deaths.
    (tmp_path / "workload_manifest.json").write_text(
        json.dumps(
            {
                "type": "self_exit",
                "status": "ok",
                "started_at": "2026-05-21 10:00:00.000Z",
                "actions": [
                    {
                        "id": "self-exit-1",
                        "fault_id": "fault-self-001",
                        "expected_exit": True,
                        "window_sec": 60,
                        "started_at": "2026-05-21 10:00:00.000Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ev_w, life_w = _writers(tmp_path)
    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=tmp_path / "incidents",
        collectors=CollectorsConfig(logcat_enabled=False),
        discover_fn=lambda adb, pkg: [],
    )
    pool.start()

    # In-window exit with the declared fault id → expected.
    pool._dispatch(
        StabilityEvent(
            event_type=EVENT_PROCESS_DEATH,
            process=PACKAGE,
            pid=1234,
            triggered_at="2026-05-21 10:00:10.000",
            summary="process_death: self exit",
            reason="exit_self",
            fault_id="fault-self-001",
        )
    )
    # Out-of-window exit without a fault id → NOT expected.
    pool._dispatch(
        StabilityEvent(
            event_type=EVENT_PROCESS_DEATH,
            process=PACKAGE,
            pid=1235,
            triggered_at="2026-05-21 11:00:00.000",
            summary="process_death: unknown",
            reason="unknown",
        )
    )
    pool.stop(join_timeout=1.0, dump_shutdown_timeout_sec=3.0)
    ev_w.close()
    life_w.close()
    incidents_by_pid = {
        inc["pid"]: inc
        for inc in [
            json.loads(p.read_text()) for p in sorted((tmp_path / "incidents").glob("*.json"))
        ]
    }
    assert incidents_by_pid[1234]["evidence"]["workload_expected"] is True
    assert "workload_expected" not in incidents_by_pid[1235]["evidence"]


def test_timed_out_task_has_exactly_one_terminal_state(tmp_path: Path):
    """A timed-out task must never later transition to persisted or failed."""
    ev_w, life_w = _writers(tmp_path)
    incidents_dir = tmp_path / "incidents"
    release = threading.Event()
    state_log = []

    def blocked_then_succeed(adb, ev, d):
        if release.wait(10.0):
            state_log.append("dumper_completed")
            return {"type": ev.event_type}
        raise RuntimeError("unexpected")

    pool = CollectorPool(
        MagicMock(),
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=incidents_dir,
        collectors=CollectorsConfig(logcat_enabled=False),
        dumps=DumpsConfig(max_concurrent=1, dump_shutdown_timeout_sec=0.1),
        discover_fn=lambda adb, pkg: [],
        java_crash_dump_fn=blocked_then_succeed,
    )
    pool.start()
    pool._dispatch(
        StabilityEvent(
            event_type=EVENT_JAVA_CRASH,
            process=PACKAGE,
            pid=4,
            triggered_at="t4",
            summary="e4",
        )
    )

    pool.stop(join_timeout=0.2, dump_shutdown_timeout_sec=0.1)
    ev_w.close()
    life_w.close()

    # Task must be timed_out, not persisted or failed.
    states = pool.dump_task_states()
    assert states["timed_out"] == 1
    assert states["persisted"] == 0
    assert states["failed"] == 0

    # Now release the dumper — it completes but must NOT change state.
    release.set()
    deadline = time.monotonic() + 3.0
    while any(t.name.startswith("dump-") for t in threading.enumerate()):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    # State must still be timed_out (not flipped to persisted).
    states_after = pool.dump_task_states()
    assert states_after["timed_out"] == 1
    assert states_after["persisted"] == 0
    assert states_after["failed"] == 0
