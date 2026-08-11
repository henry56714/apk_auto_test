from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from sat.pool import CollectorPool, CollectorsConfig, DumpsConfig
from sat.selfmon import SelfMonitor
from sat.storage import (
    EVENTS_COLUMNS,
    EVENTS_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    CsvStreamWriter,
)


def test_self_monitor_records_resource_curve(tmp_path: Path):
    depths = iter([0, 2, 5, 1])
    monitor = SelfMonitor(
        tmp_path,
        interval_sec=0.01,
        queue_depth_fn=lambda: next(depths, 0),
    )
    monitor.start()
    time.sleep(0.15)
    monitor.stop()
    summary = monitor.summary()
    assert summary["samples"]
    assert summary["rss_peak_kb"] > 0
    assert summary["threads_peak"] >= 1
    assert summary["queue_peak"] == 5
    assert (tmp_path / "self_resource.jsonl").exists()


def test_pool_self_resource_summary(tmp_path: Path):
    ev = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    life = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG)
    pool = CollectorPool(
        MagicMock(),
        "com.example.app",
        events_writer=ev,
        lifecycle_writer=life,
        incidents_dir=tmp_path / "incidents",
        collectors=CollectorsConfig(logcat_enabled=False),
        dumps=DumpsConfig(self_monitor_interval_sec=0.01),
        discover_fn=lambda adb, pkg: [],
    )
    pool.start()
    time.sleep(0.1)
    pool.stop(join_timeout=1.0)
    ev.close()
    life.close()
    summary = pool.self_resource_summary()
    assert summary["samples"]
    assert summary["rss_peak_kb"] > 0
