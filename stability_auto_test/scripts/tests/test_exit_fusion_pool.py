"""ExitInfo fusion at the pool level (spec S1-03 / IMP-03).

- A crash that only ExitInfo saw (logcat gap) becomes a real incident.
- A crash seen by both logcat and ExitInfo counts exactly once, with
  `supporting_sources` listing both.
- Run-start watermark keeps old records out and the probe runs once.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from sat.detection import EVENT_JAVA_CRASH, StabilityEvent
from sat.pool import (
    CollectorPool,
    CollectorsConfig,
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

EXIT_INFO_EMPTY = "Historical process exit information:\n"

EXIT_INFO_CRASHED = (
    "Historical process exit information:\n"
    "Package: com.example.app (u0a123)\n"
    "  Process: com.example.app (pid 1234)\n"
    "  Timestamp: 2026-08-13T10:00:05.000\n"
    "  Reason: CRASHED\n"
    "  Subreason: java.lang.RuntimeException\n"
    "  Status: 5\n"
    "  Importance: 100\n"
    "  PSS: 12345 kB\n"
    "  RSS: 23456 kB\n"
    "  Description: java.lang.RuntimeException: boom\n"
)


class _FakeAdb:
    """Scripted adb: different stdout per command; a value may be a list of
    responses consumed in order (so run-start and run-end queries differ)."""

    def __init__(self, responses, serial="emulator-5554"):
        self._responses = responses
        self._counters = {}
        self.serial = serial
        self.calls = []

    def shell(self, cmd, check=False, timeout=8.0):
        self.calls.append(cmd)
        for key, value in self._responses.items():
            if key in cmd:
                if isinstance(value, list):
                    idx = self._counters.get(key, 0)
                    self._counters[key] = idx + 1
                    value = value[min(idx, len(value) - 1)]
                if isinstance(value, Exception):
                    raise value
                return MagicMock(returncode=0, stdout=value)
        return MagicMock(returncode=0, stdout="")

    def pull(self, *a, **k):
        return MagicMock(returncode=0)

    def push(self, *a, **k):
        return MagicMock(returncode=0)


def _writers(tmp_path: Path):
    ev = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    life = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG)
    return ev, life


def _pool(tmp_path: Path, adb: _FakeAdb, dumps=None):
    ev_w, life_w = _writers(tmp_path)
    pool = CollectorPool(
        adb,
        PACKAGE,
        events_writer=ev_w,
        lifecycle_writer=life_w,
        incidents_dir=tmp_path / "incidents",
        rescan_interval_sec=10.0,
        collectors=CollectorsConfig(
            logcat_enabled=False,
            resource_risk_enabled=False,
            device_health_interval_sec=10.0,
        ),
        discover_fn=lambda a, p: [],
        dumps=dumps or DumpsConfig(post_context_sec=0.0),
    )
    return pool, ev_w, life_w


def _crash_event(pid: int = 1234, device_ts: str = "2026-08-13 10:00:05.000"):
    return StabilityEvent(
        event_type=EVENT_JAVA_CRASH,
        process=PACKAGE,
        pid=pid,
        triggered_at="2026-08-13 10:00:05.100",
        summary="java.lang.RuntimeException: boom",
        exception_class="java.lang.RuntimeException",
        device_ts=device_ts,
    )


def test_exit_info_only_crash_becomes_incident(tmp_path: Path):
    """T-L2-019 host twin: logcat gap'd, ExitInfo-only crash still surfaces."""
    adb = _FakeAdb(
        {
            # start probe + watermark query see no history; the crash record
            # only exists when stop() queries.
            "dumpsys activity exit-info": [EXIT_INFO_EMPTY, EXIT_INFO_EMPTY, EXIT_INFO_CRASHED],
            "date +%s": "1786615200",  # 2026-08-13 10:00:00Z (before crash ts)
        }
    )
    pool, ev_w, life_w = _pool(tmp_path, adb)
    pool.start()
    # No logcat event at all: the crash only exists in ExitInfo.
    pool.stop(join_timeout=1.0)
    ev_w.close()
    life_w.close()

    journal = (tmp_path / "incident_journal.jsonl").read_text()
    assert journal.count('"status": "detected"') == 1
    assert journal.count('"status": "persisted"') == 1
    incident_files = list((tmp_path / "incidents").glob("*.json"))
    assert len(incident_files) == 1
    incident = json.loads(incident_files[0].read_text())
    assert incident["type"] == EVENT_JAVA_CRASH
    assert incident["evidence"]["source"] == "exit_info"
    assert incident["evidence"]["exit_info_reason"] == "crashed"
    assert "exit_info" in incident["evidence"]["supporting_sources"]
    # Soak finding: a late-flushed record must land on the timeline at the
    # real exit time (device record ts, tz-corrected), not at the poll time.
    assert incident["triggered_at"] == "2026-08-13 10:00:05.000"
    assert incident["evidence"]["device_ts"] == "2026-08-13T10:00:05.000"


def test_logcat_and_exit_info_count_once(tmp_path: Path):
    adb = _FakeAdb(
        {
            "dumpsys activity exit-info": [EXIT_INFO_EMPTY, EXIT_INFO_EMPTY, EXIT_INFO_CRASHED],
            "date +%s": "1786615200",
        }
    )
    pool, ev_w, life_w = _pool(tmp_path, adb)
    pool.start()
    # Logcat sees the crash first.
    pool._dispatch(_crash_event())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if pool.dump_task_states()["persisted"] == 1:
            break
        time.sleep(0.05)
    pool.stop(join_timeout=1.0, dump_shutdown_timeout_sec=5.0)
    ev_w.close()
    life_w.close()

    states = pool.dump_task_states()
    assert states["persisted"] == 1
    journal = (tmp_path / "incident_journal.jsonl").read_text()
    assert journal.count('"status": "detected"') == 1, "must not double-count"
    incident_files = list((tmp_path / "incidents").glob("*.json"))
    assert len(incident_files) == 1
    incident = json.loads(incident_files[0].read_text())
    sources = incident["evidence"].get("supporting_sources") or []
    assert "exit_info" in sources
    assert incident["evidence"].get("exit_info_reason") == "crashed"


def test_old_exit_info_records_filtered_by_run_start_epoch(tmp_path: Path):
    """Run start with no history: watermark = device epoch; old records die."""
    old_only = (
        "Historical process exit information:\n"
        "Package: com.example.app (u0a123)\n"
        "  Process: com.example.app (pid 11)\n"
        "  Timestamp: 2026-08-10T09:00:00.000\n"  # before run start
        "  Reason: CRASHED\n"
    )
    old_and_new = old_only + (
        "Package: com.example.app (u0a123)\n"
        "  Process: com.example.app (pid 22)\n"
        "  Timestamp: 2026-08-13T10:05:00.000\n"  # during run
        "  Reason: ANR\n"
    )
    adb = _FakeAdb(
        {
            "dumpsys activity exit-info": [old_only, old_only, old_and_new],
            "date +%s": "1786615200",  # 2026-08-13 10:00:00Z (run start)
        }
    )
    pool, ev_w, life_w = _pool(tmp_path, adb)
    pool.start()
    pool.stop(join_timeout=1.0)
    ev_w.close()
    life_w.close()
    # Only the ANR (during-run) record survives.
    records = pool.exit_info_records()
    assert len(records) == 1
    assert records[0]["pid"] == 22
    assert records[0]["exit_reason"] == "anr"
    journal = (tmp_path / "incident_journal.jsonl").read_text()
    assert journal.count('"status": "detected"') == 1
