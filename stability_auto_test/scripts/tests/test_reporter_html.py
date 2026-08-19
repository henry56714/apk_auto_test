from __future__ import annotations

from pathlib import Path

from sat.reporter import html


def test_html_render_includes_all_sections(tmp_path: Path):
    result = {
        "schema_version": "1.0",
        "run": {
            "package": "com.example.app",
            "started_at": "2026-05-21 10:00:00.000",
            "ended_at": "2026-05-21 10:05:00.000",
            "duration_sec": 300.0,
            "exit_code": 0,
            "exit_reason": "duration_elapsed",
            "device": {"serial": "x", "android_version": "14"},
            "config_effective": {
                "package": "com.example.app",
                "device": "x",
                "output_dir": "/tmp/report",
                "wait_timeout_sec": 60.0,
                "enable_java_crash": True,
                "pre_context_sec": 45.0,
                "redaction_regexes": [],
                "config_sources": {
                    "package": "cli",
                    "device": "cli",
                    "output_dir": "cli",
                    "pre_context_sec": "yaml",
                    "redaction_regexes": "cli",
                },
            },
        },
        "processes": [
            {
                "name": "com.example.app",
                "uptime_ratio": 1.0,
                "restart_count": 0,
                "events": {"java_crash": 1, "native_crash": 0, "anr": 0, "process_death": 0},
            }
        ],
        "incident_summary": {
            "record_count": 2,
            "root_problem_count": 1,
            "correlated_termination_count": 1,
            "by_type": {"java_crash": 1, "process_death": 1},
        },
        "collection_health": "healthy",
        "coverage_ratio": 1.0,
        "verdict": "unstable",
        "verdict_reason": ["java_crash"],
        "verdict_confidence": "high",
        "event_pipeline": {"detected_count": 2, "persisted_count": 2, "failed_count": 0},
        "resource_risk": [
            {
                "pid": 1234,
                "ts": 1.0,
                "metric": "fd_count",
                "baseline": 10,
                "value": 100,
                "message": "fd_count grew",
            }
        ],
        "self_resource": {"samples": [{"rss_kb": 100}], "rss_peak_kb": 100},
        "exit_info": [
            {
                "pid": 1234,
                "process": "com.example.app",
                "exit_reason": "crashed",
                "is_stability_failure": True,
                "correlated_incident_id": "incident-001",
            }
        ],
        "capabilities": [{"name": "anr_trace_dir", "status": "available"}],
        "collectors": {"logcat": {"lines_read": 10}},
        "disk_audit": [],
        "policy": {"passed": False},
        "recovery_warnings": [],
        "device_events": [
            {
                "event_type": "reboot",
                "started_at": 1700000000.0,
                "ended_at": 1700000030.0,
                "detail": "boot_id changed",
            }
        ],
        "incidents": [
            {
                "id": "incident-001",
                "type": "java_crash",
                "process": "com.example.app",
                "pid": 1234,
                "triggered_at": "2026-05-21 10:01:00.000",
                "severity": "fatal",
                "summary": "boom",
                "evidence": {
                    "logcat_slice_file": "f.txt",
                    "trace_file": None,
                    "top_frames": ["at X.y(X.java:1)"],
                    "source": "logcat",
                },
            },
            {
                "id": "incident-002",
                "type": "process_death",
                "process": "com.example.app",
                "pid": 1234,
                "triggered_at": "2026-05-21 10:01:01.000",
                "severity": "error",
                "summary": "process disappeared",
                "evidence": {
                    "source": "watcher",
                    "secondary_to_incident_id": "incident-001",
                    "root_cause_type": "java_crash",
                },
            },
        ],
        "lifecycle_events": [],
        "bookmarks": [{"timestamp": "2026-05-21 10:02:00.000", "label": "b"}],
        "data_files": {"events": [], "lifecycle": [], "logcat": []},
    }
    compact = html._compact_config(result["run"]["config_effective"])
    assert compact["values"] == {
        "package": "com.example.app",
        "device": "x",
        "output_dir": "/tmp/report",
        "pre_context_sec": 45.0,
    }
    assert compact["hidden_count"] == 3
    written = html.write(result, tmp_path)
    text = written.read_text()
    assert "Stability report" in text
    assert "com.example.app" in text
    assert "Plotly.newPlot" in text
    assert "device-events-data" in text
    assert "设备事件" in text
    assert 'id="additional-diagnostics"' in text
    assert "fd_count grew" in text
    assert "secondary_to_incident_id" in text
    assert "ApplicationExitInfo" in text
    assert "defaults are hidden" in text
    assert '"hidden_count": 3' in text
    # Counters block + incident details rendered
    assert "Java crash" in text
    assert "boom" in text
