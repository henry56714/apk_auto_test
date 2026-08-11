from __future__ import annotations

import os
import time
from pathlib import Path

from sat.quota import QuotaConfig, QuotaTracker


class _FakeUsage:
    def __init__(self, free: int):
        self.free = free


def test_hard_quota_detected(tmp_path: Path):
    tracker = QuotaTracker(
        tmp_path,
        QuotaConfig(max_disk_bytes=1024 * 1024),
        disk_usage_fn=lambda p: _FakeUsage(512 * 1024),
    )
    assert tracker.hard_reached is True
    state = tracker.disk_state()
    assert state["soft_warning"] is True


def test_no_quota_configured_is_not_hard(tmp_path: Path):
    tracker = QuotaTracker(
        tmp_path,
        QuotaConfig(max_disk_bytes=None),
        disk_usage_fn=lambda p: _FakeUsage(0),
    )
    assert tracker.hard_reached is False


def test_log_retention_removes_old_files(tmp_path: Path):
    now = time.time()
    old = tmp_path / "logcat_2026-08-09_00.log"
    new = tmp_path / "logcat_2026-08-10_00.log"
    old.write_text("x", encoding="utf-8")
    new.write_text("x", encoding="utf-8")
    os.utime(old, (now - 2 * 3600, now - 2 * 3600))
    os.utime(new, (now - 100, now - 100))
    tracker = QuotaTracker(
        tmp_path,
        QuotaConfig(log_retention_hours=1),
        now_sec_fn=lambda: now,
    )
    removed = tracker.enforce_log_retention()
    assert removed == 1
    assert not old.exists()
    assert new.exists()
