from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest
from sat.journal import STATUS_DETECTED, STATUS_FAILED, STATUS_PERSISTED
from sat.reporter import result as result_builder

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "report.schema.json"


def _make_csvs(output_dir: Path):
    (output_dir / "events_2026-05-21_10.csv").write_text(
        "# stability_auto_test/events/v1\n"
        "timestamp,event_type,process_name,pid,severity,summary\n"
        "2026-05-21 10:00:00.000,java_crash,com.example.app,1234,fatal,boom\n"
    )
    (output_dir / "lifecycle_2026-05-21_10.csv").write_text(
        "# stability_auto_test/lifecycle/v1\n"
        "timestamp,process_name,event,old_pid,new_pid,gap_sec\n"
        "2026-05-21 10:00:00.000,com.example.app,new,0,1234,0.0\n"
        "2026-05-21 10:01:00.000,com.example.app,restart,1234,1235,2.0\n"
        "2026-05-21 10:02:00.000,com.example.app,gone,1235,0,0.0\n"
    )


def _make_incidents(output_dir: Path):
    inc_dir = output_dir / "incidents"
    inc_dir.mkdir()
    (inc_dir / "java_crash_001.json").write_text(json.dumps({
        "type": "java_crash",
        "process": "com.example.app",
        "pid": 1234,
        "triggered_at": "2026-05-21 10:00:00.000",
        "severity": "fatal",
        "summary": "boom",
        "evidence": {
            "logcat_slice_file": "java_crash_001.txt",
            "source": "logcat",
            "dedup_count": 1,
            "top_frames": ["at X.y(X.java:1)"],
        },
    }))


def test_build_and_schema_validate(tmp_path: Path):
    _make_csvs(tmp_path)
    _make_incidents(tmp_path)
    started = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 21, 10, 5, 0, tzinfo=timezone.utc)
    result = result_builder.build(
        output_dir=tmp_path,
        package="com.example.app",
        started_at=started,
        ended_at=ended,
        device={"serial": "x", "android_version": "14", "sdk_int": 34, "cpu_cores": 4},
        config_effective={"package": "com.example.app"},
        exit_code=0,
        exit_reason="duration_elapsed",
        bookmarks=[{"timestamp": "2026-05-21 10:02:00.000", "label": "x"}],
        sample_failures={"logcat": 0, "dropbox": 1},
    )
    # Process has the java_crash counted
    proc = next(p for p in result["processes"] if p["name"] == "com.example.app")
    assert proc["events"]["java_crash"] == 1
    assert proc["restart_count"] == 1
    assert 0.0 < proc["uptime_ratio"] <= 1.0
    # Schema check
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(result, schema)
    # Write + read back
    written = result_builder.write(result, tmp_path)
    assert written.exists()
    on_disk = json.loads(written.read_text())
    assert on_disk["schema_version"] == "1.14"
    assert on_disk["event_pipeline"]["detected_count"] == 0


def test_journal_failed_evidence_keeps_incident_in_report(tmp_path: Path):
    (tmp_path / "incident_journal.jsonl").write_text(
        "\n".join([
            json.dumps({
                "journal_version": 1,
                "event_id": "e-fail",
                "status": STATUS_DETECTED,
                "event_type": "anr",
                "process": "com.example.app",
                "pid": 1234,
                "triggered_at": "2026-05-21 10:00:00.000",
                "severity": "error",
                "summary": "ANR: input dispatching timed out",
                "source": "logcat",
            }),
            json.dumps({
                "journal_version": 1,
                "event_id": "e-fail",
                "status": STATUS_FAILED,
                "error_type": "RuntimeError",
                "error": "trace pull failed",
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    started = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 21, 10, 5, 0, tzinfo=timezone.utc)
    result = result_builder.build(
        output_dir=tmp_path,
        package="com.example.app",
        started_at=started,
        ended_at=ended,
        device={"serial": "x", "android_version": "14", "sdk_int": 34, "cpu_cores": 4},
        config_effective={"package": "com.example.app"},
        exit_code=0,
        exit_reason="duration_elapsed",
    )

    assert len(result["incidents"]) == 1
    inc = result["incidents"][0]
    assert inc["event_id"] == "e-fail"
    assert inc["evidence"]["evidence_status"] == STATUS_FAILED
    assert result["event_pipeline"]["failed_count"] == 1
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(result, schema)


def test_journal_truncated_tail_marks_report_degraded(tmp_path: Path):
    journal = tmp_path / "incident_journal.jsonl"
    journal.write_text(
        "\n".join([
            json.dumps({
                "journal_version": 1,
                "event_id": "e1",
                "status": STATUS_DETECTED,
                "event_type": "java_crash",
                "process": "com.example.app",
                "pid": 1,
                "triggered_at": "2026-05-21 10:00:00.000",
                "severity": "fatal",
                "summary": "x",
            }),
            json.dumps({"journal_version": 1, "event_id": "e1", "status": STATUS_PERSISTED}),
            '{"journal_version": 1, "event_id": "e2", "status": "de',
        ]) + "\n",
        encoding="utf-8",
    )
    started = datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 21, 10, 5, 0, tzinfo=timezone.utc)
    result = result_builder.build(
        output_dir=tmp_path,
        package="com.example.app",
        started_at=started,
        ended_at=ended,
        device={"serial": "x", "android_version": "14", "sdk_int": 34, "cpu_cores": 4},
        config_effective={"package": "com.example.app"},
        exit_code=0,
        exit_reason="duration_elapsed",
    )
    assert result["collection_health"] == "degraded"
    assert any("truncated" in w for w in result["recovery_warnings"])
    assert result["event_pipeline"]["persisted_count"] == 1


def _build_with_health(tmp_path: Path, collector_health=None, collectors=None):
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
        collector_health=collector_health,
        collectors=collectors,
    )


def test_healthy_full_coverage_report_is_stable(tmp_path: Path):
    result = _build_with_health(
        tmp_path,
        collector_health={
            "health": "healthy",
            "coverage_ratio": 0.995,
            "reasons": [],
        },
    )
    assert result["collection_health"] == "healthy"
    assert result["coverage_ratio"] == pytest.approx(0.995)
    assert result["verdict"] == "stable"


def test_low_coverage_report_is_inconclusive(tmp_path: Path):
    result = _build_with_health(
        tmp_path,
        collector_health={
            "health": "degraded",
            "coverage_ratio": 0.8,
            "reasons": ["coverage 0.800 below threshold 0.99"],
        },
    )
    assert result["collection_health"] == "degraded"
    assert result["coverage_ratio"] == pytest.approx(0.8)
    assert result["verdict"] == "inconclusive"


def test_logcat_startup_failure_is_inconclusive(tmp_path: Path):
    result = _build_with_health(
        tmp_path,
        collector_health={
            "health": "inconclusive",
            "coverage_ratio": 0.0,
            "reasons": ["logcat collector never collected"],
        },
    )
    assert result["collection_health"] == "inconclusive"
    assert result["verdict"] == "inconclusive"
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(result, schema)


def test_reconnects_and_gaps_appear_in_report(tmp_path: Path):
    result = _build_with_health(
        tmp_path,
        collector_health={
            "health": "degraded",
            "coverage_ratio": 0.9,
            "reasons": ["logcat reconnected 1 time(s)"],
        },
        collectors={
            "logcat": {
                "lines_read": 123,
                "reconnects": 1,
                "read_failures": 0,
                "last_device_ts": "05-21 10:04:00.000",
                "up_intervals": [[0.0, 100.0], [110.0, 200.0]],
                "gap_intervals": [[100.0, 110.0]],
                "started_at": 0.0,
                "ended_at": 200.0,
                "queue_backlog_peak": 2,
            }
        },
    )
    logcat = result["collectors"]["logcat"]
    assert logcat["reconnects"] == 1
    assert logcat["gap_intervals"] == [[100.0, 110.0]]
    assert logcat["last_device_ts"] == "05-21 10:04:00.000"
    assert logcat["queue_backlog_peak"] == 2
    assert result["verdict"] == "inconclusive"
