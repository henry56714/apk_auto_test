from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

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
    assert on_disk["schema_version"] == "1.0"
