from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from sat.discovery import Process
from sat.pool import CollectorPool, CollectorsConfig
from sat.storage import (
    EVENTS_COLUMNS,
    EVENTS_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    CsvStreamWriter,
)


def _pool(tmp_path: Path, policy: str, logcat_enabled: bool = False):
    ev = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    life = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG)
    return CollectorPool(
        MagicMock(),
        "com.example.app",
        events_writer=ev,
        lifecycle_writer=life,
        collectors=CollectorsConfig(
            logcat_enabled=logcat_enabled,
            device_reboot_policy=policy,
        ),
        discover_fn=lambda adb, pkg: [],
    )


def test_fail_fast_policy_stops_pool_on_device_gap(tmp_path: Path):
    pool = _pool(tmp_path, "fail-fast")
    pool._on_device_gap("reboot")
    assert pool._global_stop.is_set()
    assert pool._accepting is False


def test_wait_and_resume_restarts_logcat_and_clears_pids(tmp_path: Path):
    spawns = {"n": 0}

    class StatsStream:
        def __init__(self):
            spawns["n"] += 1

        def stop(self):
            pass

        def lines(self):
            return iter([])

        @property
        def stats(self):
            return {
                "lines_read": 0, "reconnects": 0, "read_failures": 0,
                "last_device_ts": None, "started_at": None, "ended_at": None,
                "up_intervals": [], "gap_intervals": [], "backlog_peak": 0,
            }

    pool = _pool(tmp_path, "wait-and-resume", logcat_enabled=True)
    pool._discover = lambda adb, pkg: [Process(pid=111, name="com.example.app")]
    pool._logcat_stream_factory = StatsStream
    pool.start(initial_processes=[Process(pid=111, name="com.example.app")])
    time.sleep(0.1)
    assert spawns["n"] == 1
    assert len(pool.current_processes()) == 1

    pool._on_device_gap("reboot")
    pool._on_device_recovered()
    time.sleep(0.15)
    assert spawns["n"] == 2
    assert pool.current_processes() == []
    pool.stop(join_timeout=1.0)


def test_pid_not_mixed_after_recovery(tmp_path: Path):
    states = [[Process(pid=111, name="com.example.app")], []]
    it = iter(states)
    pool = CollectorPool(
        MagicMock(),
        "com.example.app",
        events_writer=CsvStreamWriter(
            tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG,
        ),
        lifecycle_writer=CsvStreamWriter(
            tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG,
        ),
        collectors=CollectorsConfig(logcat_enabled=False),
        discover_fn=lambda adb, pkg: next(it, []),
    )
    pool.start()
    time.sleep(0.05)
    assert [p.pid for p in pool.current_processes()] == [111]
    pool._on_device_recovered()
    assert pool.current_processes() == []
    pool.stop(join_timeout=1.0)
