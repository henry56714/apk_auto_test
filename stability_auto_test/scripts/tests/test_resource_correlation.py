from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
from sat.collectors.resource_risk import correlate_resource_risk
from sat.reporter import result as result_builder

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "report.schema.json"


def test_risk_event_attached_to_nearby_incident():
    incident = {
        "id": "incident-001",
        "type": "java_crash",
        "process": "com.example.app",
        "pid": 1234,
        "triggered_at": "2026-05-21 10:00:00.000",
        "evidence": {},
    }
    events = [{
        "pid": 1234,
        "ts": datetime(2026, 5, 21, 10, 0, 20, tzinfo=timezone.utc).timestamp(),
        "metric": "fd_count",
        "value": 300,
        "baseline": 100,
        "message": "fd grew",
    }]
    correlate_resource_risk([incident], events)
    assert incident["evidence"]["resource_risk"]["metric"] == "fd_count"

    other = {
        "id": "incident-002",
        "type": "anr",
        "process": "com.example.app",
        "pid": 999,
        "triggered_at": "2026-05-21 10:00:00.000",
        "evidence": {},
    }
    correlate_resource_risk([other], events)
    assert "resource_risk" not in other["evidence"]


def test_resource_risk_in_report_schema(tmp_path: Path):
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
        resource_risk=[{
            "pid": 1, "ts": 1000.0, "metric": "fd_count", "value": 300,
            "baseline": 100, "message": "grew",
        }],
    )
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(result, schema)
    assert result["resource_risk"][0]["metric"] == "fd_count"
