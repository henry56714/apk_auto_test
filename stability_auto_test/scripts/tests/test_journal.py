from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sat.detection import StabilityEvent
from sat.journal import (
    STATUS_DETECTED,
    STATUS_DROPPED_BY_CAP,
    STATUS_FAILED,
    STATUS_PERSISTED,
    STATUS_TIMED_OUT,
    IncidentJournal,
    read_journal,
)
from sat.reporter import result as result_builder


def _event(event_id: str = "e1"):
    return SimpleNamespace(
        event_type="java_crash",
        process="com.example.app",
        pid=1234,
        triggered_at="2026-05-21 10:00:00.000",
        severity="fatal",
        summary="boom",
        source="logcat",
        device_ts="05-21 10:00:00.000",
    )


def _write_journal(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _build(tmp_path: Path) -> dict:
    started = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 21, 10, 5, 0, tzinfo=timezone.utc)
    return result_builder.build(
        output_dir=tmp_path,
        package="com.example.app",
        started_at=started,
        ended_at=ended,
        device={"serial": "x", "android_version": "14", "sdk_int": 34, "cpu_cores": 4},
        config_effective={"package": "com.example.app"},
        exit_code=0,
        exit_reason="duration_elapsed",
    )


def test_journal_round_trip_detected_persisted(tmp_path: Path):
    journal = IncidentJournal(tmp_path / "incident_journal.jsonl")
    journal.detected("e1", _event("e1"))
    journal.terminal("e1", STATUS_PERSISTED)
    journal.close()

    records, warnings = read_journal(tmp_path / "incident_journal.jsonl")
    assert [r["status"] for r in records] == [STATUS_DETECTED, STATUS_PERSISTED]
    assert records[0]["event_id"] == "e1"
    assert records[1]["event_id"] == "e1"
    assert warnings == []


def test_journal_failed_and_dropped_records(tmp_path: Path):
    journal = IncidentJournal(tmp_path / "incident_journal.jsonl")
    journal.detected("e1", _event("e1"))
    journal.terminal("e1", STATUS_FAILED, error_type="RuntimeError", error="boom")
    journal.detected("e2", _event("e2"))
    journal.terminal("e2", STATUS_DROPPED_BY_CAP, error_type="cap")
    journal.close()

    records, _ = read_journal(tmp_path / "incident_journal.jsonl")
    statuses = [(r["event_id"], r["status"]) for r in records]
    assert ("e1", STATUS_FAILED) in statuses
    assert ("e2", STATUS_DROPPED_BY_CAP) in statuses


def test_truncated_tail_ignored_with_warning(tmp_path: Path):
    path = tmp_path / "incident_journal.jsonl"
    _write_journal(path, [
        {"event_id": "e1", "status": STATUS_DETECTED},
        {"event_id": "e1", "status": STATUS_PERSISTED},
    ])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"event_id": "e2", "status": "detected", "truncat')

    records, warnings = read_journal(path)
    assert [r["event_id"] for r in records] == ["e1", "e1"]
    assert any("truncated journal line 3" in w for w in warnings)


def test_report_keeps_placeholder_for_failed_dump(tmp_path: Path):
    _write_journal(tmp_path / "incident_journal.jsonl", [
        {
            "journal_version": 1,
            "event_id": "e1",
            "status": STATUS_DETECTED,
            "event_type": "java_crash",
            "process": "com.example.app",
            "pid": 1234,
            "triggered_at": "2026-05-21 10:00:00.000",
            "severity": "fatal",
            "summary": "boom",
            "source": "logcat",
        },
        {
            "journal_version": 1,
            "event_id": "e1",
            "status": STATUS_FAILED,
            "error_type": "RuntimeError",
            "error": "dumper exploded",
        },
    ])
    result = _build(tmp_path)

    assert result["event_pipeline"]["detected_count"] == 1
    assert result["event_pipeline"]["persisted_count"] == 0
    assert result["event_pipeline"]["failed_count"] == 1
    assert len(result["incidents"]) == 1
    incident = result["incidents"][0]
    assert incident["event_id"] == "e1"
    assert incident["evidence"]["evidence_status"] == STATUS_FAILED
    assert incident["evidence"]["error_type"] == "RuntimeError"
    assert result["collection_health"] == "degraded"


def test_report_counts_obey_journal_equation(tmp_path: Path):
    records = []
    for i in range(2):
        records.append({"event_id": f"p{i}", "status": STATUS_DETECTED, "event_type": "anr",
                        "process": "com.example.app", "pid": 1})
        records.append({"event_id": f"p{i}", "status": STATUS_PERSISTED})
    records.append({"event_id": "f1", "status": STATUS_DETECTED, "event_type": "anr",
                    "process": "com.example.app", "pid": 1})
    records.append({"event_id": "f1", "status": STATUS_FAILED})
    records.append({"event_id": "t1", "status": STATUS_DETECTED, "event_type": "anr",
                    "process": "com.example.app", "pid": 1})
    records.append({"event_id": "t1", "status": STATUS_TIMED_OUT})
    for i in range(2):
        records.append({"event_id": f"d{i}", "status": STATUS_DETECTED, "event_type": "anr",
                        "process": "com.example.app", "pid": 1})
        records.append({"event_id": f"d{i}", "status": STATUS_DROPPED_BY_CAP})
    _write_journal(tmp_path / "incident_journal.jsonl", records)

    result = _build(tmp_path)
    ep = result["event_pipeline"]
    assert ep["detected_count"] == 6
    assert ep["persisted_count"] == 2
    assert ep["failed_count"] == 1
    assert ep["timed_out_count"] == 1
    assert ep["dropped_by_cap_count"] == 2
    assert ep["detected_count"] == (
        ep["persisted_count"] + ep["failed_count"] + ep["timed_out_count"]
        + ep["dropped_by_cap_count"]
    )


def test_truncated_journal_report_marks_degraded(tmp_path: Path):
    path = tmp_path / "incident_journal.jsonl"
    _write_journal(path, [
        {"event_id": "e1", "status": STATUS_DETECTED, "event_type": "java_crash",
         "process": "com.example.app", "pid": 1, "triggered_at": "2026-05-21 10:00:00.000",
         "severity": "fatal", "summary": "x"},
        {"event_id": "e1", "status": STATUS_PERSISTED},
    ])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"event_id": "e2", "status": "de')

    result = _build(tmp_path)
    assert result["collection_health"] == "degraded"
    assert result["recovery_warnings"]
    assert result["event_pipeline"]["persisted_count"] == 1
    assert result["incidents"][0]["event_id"] == "e1"


def test_pool_dispatch_event_id_consistent_across_csv_journal_report(
    tmp_path: Path,
    monkeypatch,
):
    from unittest.mock import MagicMock

    from sat.api import StabilityConfig, StabilityTest
    from sat.device import DeviceInfo
    from sat.discovery import Process

    monkeypatch.setattr(
        "sat.api.preflight",
        lambda adb, *, serial, package: DeviceInfo(
            serial="test-serial", android_version="14", sdk_int=34, cpu_cores=4,
        ),
    )
    monkeypatch.setattr("sat.api.wait_for_processes",
                        lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)])

    cfg = StabilityConfig(
        package="com.example.app",
        output_dir=tmp_path / "out",
        wait_timeout_sec=1.0,
        rescan_interval_sec=10.0,
        logcat_enabled=False,
        emit_html=False,
        status_interval_sec=10.0,
    )
    adb = MagicMock()
    adb.serial = "test-serial"
    t = StabilityTest(cfg, adb=adb, discover_fn=lambda adb, pkg: [Process(pid=1234, name=pkg)])
    t.start()
    t._pool._dispatch(StabilityEvent(
        event_type="java_crash",
        process="com.example.app",
        pid=1234,
        triggered_at="2026-05-21 10:00:00.000",
        severity="fatal",
        summary="boom",
        source="logcat",
        raw_lines=["05-21 10:00:00.000  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main"],
    ))
    t.stop()

    events_file = next((cfg.output_dir).glob("events_*.csv"))
    with open(events_file, encoding="utf-8") as fh:
        lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    rows = list(csv.DictReader(lines))
    csv_event_id = rows[0]["event_id"]
    csv_run_id = rows[0]["run_id"]

    journal_records, _ = read_journal(cfg.output_dir / "incident_journal.jsonl")
    journal_event_id = journal_records[0]["event_id"]
    journal_run_id = journal_records[0]["run_id"]

    report = json.loads((cfg.output_dir / "report.json").read_text())
    report_event_id = report["incidents"][0]["event_id"]
    report_run_id = report["run"]["run_id"]

    assert csv_event_id == journal_event_id == report_event_id
    assert csv_run_id == journal_run_id == report_run_id
    assert [r["status"] for r in journal_records] == [STATUS_DETECTED, STATUS_PERSISTED]
    assert report["event_pipeline"]["detected_count"] == 1
    assert report["event_pipeline"]["persisted_count"] == 1
