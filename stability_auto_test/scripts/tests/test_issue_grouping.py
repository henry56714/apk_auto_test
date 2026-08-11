from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
from sat.reporter import html as html_renderer
from sat.reporter import result as result_builder

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "report.schema.json"


def _incident(i: int) -> dict:
    return {
        "type": "java_crash",
        "process": "com.example.app",
        "pid": 1000 + i,
        "triggered_at": f"2026-05-21 10:00:{i:02d}.000",
        "severity": "fatal",
        "summary": "java.lang.RuntimeException: boom",
        "evidence": {
            "exception_class": "java.lang.RuntimeException",
            "top_frames": ["at com.example.Main.run(Main.java:1)"],
        },
    }


def test_report_contains_issue_groups(tmp_path: Path):
    incidents = [_incident(i) for i in range(10)]
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
    # Replace the empty incidents with our synthetic set and rebuild groups.
    result["incidents"] = incidents
    for i, inc in enumerate(result["incidents"], start=1):
        inc["id"] = f"incident-{i:03d}"
    from sat.analyzers.fingerprint import group_incidents
    result["issue_groups"] = group_incidents(result["incidents"])

    assert len(result["issue_groups"]) == 1
    group = result["issue_groups"][0]
    assert group["occurrence_count"] == 10
    assert len(group["occurrence_ids"]) == 10
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(result, schema)

    path = html_renderer.write(result, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "issue-groups" in text
    assert "<details>" in text
    assert "10" in text
    assert "incident-010" in text
