from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from sat.backpressure import BackpressureController, EvidenceSampler
from sat.collectors.logcat import LogcatStream
from sat.detection import EVENT_JAVA_CRASH, StabilityEvent
from sat.discovery import Process
from sat.journal import STATUS_DROPPED_BY_BACKPRESSURE, read_journal
from sat.pool import CollectorPool, CollectorsConfig, DumpsConfig
from sat.storage import (
    EVENTS_COLUMNS,
    EVENTS_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    CsvStreamWriter,
)


def test_backpressure_controller_limits_queue():
    ctrl = BackpressureController(max_queue_size=2)
    assert ctrl.try_acquire() is True
    assert ctrl.try_acquire() is True
    assert ctrl.try_acquire() is False
    assert ctrl.dropped_count() == 1
    ctrl.release()
    assert ctrl.try_acquire() is True


def test_evidence_sampler_first_and_every_n_full():
    sampler = EvidenceSampler(every_n=5)
    decisions = [sampler.decide("fp") for _ in range(6)]
    assert decisions == ["full", "occurrence_only", "occurrence_only",
                         "occurrence_only", "full", "occurrence_only"]


def test_pool_drops_when_queue_full_and_records_journal(tmp_path: Path):
    ev = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    life = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG)
    gate = MagicMock()

    def slow_dump(adb, event, d):
        time.sleep(0.3)
        return {"type": event.event_type}

    pool = CollectorPool(
        gate,
        "com.example.app",
        events_writer=ev,
        lifecycle_writer=life,
        incidents_dir=tmp_path / "incidents",
        collectors=CollectorsConfig(logcat_enabled=False),
        dumps=DumpsConfig(max_queue_size=1, max_concurrent=1),
        discover_fn=lambda adb, pkg: [],
        java_crash_dump_fn=slow_dump,
    )
    pool.start()
    pool._dispatch(StabilityEvent(
        event_type=EVENT_JAVA_CRASH, process="com.example.app",
        pid=1, triggered_at="t1", summary="a",
    ))
    time.sleep(0.05)
    pool._dispatch(StabilityEvent(
        event_type=EVENT_JAVA_CRASH, process="com.example.app",
        pid=2, triggered_at="t2", summary="b",
    ))
    pool.stop(join_timeout=1.0, dump_shutdown_timeout_sec=3.0)
    ev.close()
    life.close()

    assert pool.dropped_by_backpressure_count() == 1
    records, _ = read_journal(tmp_path / "incident_journal.jsonl")
    statuses = [r["status"] for r in records]
    assert STATUS_DROPPED_BY_BACKPRESSURE in statuses


def test_hard_quota_skips_context_but_keeps_journal(tmp_path: Path):
    lines = [
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: Process: com.example.app, PID: 1234",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: java.lang.RuntimeException: boom",
        "05-21 10:00:00.100  1234  1234 E AndroidRuntime: \tat X.y(X.java:1)",
        "05-21 10:00:00.200  9999  9999 I OtherTag: end",
    ]
    stream = LogcatStream(serial=None, buffers=["main"], reconnect_backoff_sec=0.0,
                          popen_fn=lambda *a, **k: None)
    stream.lines = lambda: iter(lines)  # type: ignore[method-assign]

    ev = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    life = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG)
    pool = CollectorPool(
        MagicMock(),
        "com.example.app",
        events_writer=ev,
        lifecycle_writer=life,
        incidents_dir=tmp_path / "incidents",
        collectors=CollectorsConfig(logcat_enabled=True),
        dumps=DumpsConfig(post_context_sec=0.01, max_disk_bytes=1024),
        discover_fn=lambda adb, pkg: [],
        logcat_stream_factory=lambda: stream,
    )
    pool._quota._disk_usage = lambda p: type("U", (), {"free": 100})()
    pool.start(initial_processes=[Process(pid=1234, name="com.example.app")])
    time.sleep(0.4)
    pool.stop(join_timeout=1.0, dump_shutdown_timeout_sec=3.0)
    ev.close()
    life.close()

    assert not list((tmp_path / "incidents").glob("*_context.txt"))
    assert (tmp_path / "incident_journal.jsonl").exists()
    incident_files = list((tmp_path / "incidents").glob("*.json"))
    assert incident_files
    incident = json.loads(incident_files[0].read_text())
    assert incident["evidence"].get("disk_quota_skipped") is True
