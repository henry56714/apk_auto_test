from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from sat import selfmon as selfmon_module
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


def test_empty_summary_has_zero_peaks(tmp_path: Path):
    assert SelfMonitor(tmp_path).summary() == {
        "samples": [],
        "rss_growth_kb": 0,
        "rss_peak_kb": 0,
        "threads_peak": 0,
        "fds_peak": 0,
        "queue_peak": 0,
    }


def test_sample_normalizes_macos_rss_and_dev_fd_count(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        selfmon_module.resource,
        "getrusage",
        lambda _: SimpleNamespace(ru_maxrss=8 * 1024),
    )
    monkeypatch.setattr(selfmon_module.sys, "platform", "darwin")
    monkeypatch.setattr(selfmon_module.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(selfmon_module.os, "listdir", lambda path: ["0", "1", "2"])
    monitor = SelfMonitor(tmp_path, queue_depth_fn=lambda: 7, now_fn=lambda: 123.0)

    sample = monitor._sample()

    assert sample["ts"] == 123.0
    assert sample["rss_kb"] == 8
    assert sample["fds"] == 3
    assert sample["queue_depth"] == 7


def test_sample_marks_fd_probe_failure_without_losing_other_metrics(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(selfmon_module.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(
        selfmon_module.os,
        "listdir",
        lambda path: (_ for _ in ()).throw(OSError("denied")),
    )
    sample = SelfMonitor(tmp_path)._sample()
    assert sample["fds"] == -1
    assert sample["rss_kb"] >= 0
    assert sample["threads"] >= 1


def test_summary_clamps_negative_rss_growth(tmp_path: Path):
    monitor = SelfMonitor(tmp_path)
    monitor._samples = [
        {"rss_kb": 200, "threads": 2, "fds": 4, "queue_depth": 1},
        {"rss_kb": 100, "threads": 3, "fds": 5, "queue_depth": 2},
    ]
    summary = monitor.summary()
    assert summary["rss_growth_kb"] == 0
    assert summary["rss_peak_kb"] == 200
    assert summary["threads_peak"] == 3
    assert summary["fds_peak"] == 5
    assert summary["queue_peak"] == 2
