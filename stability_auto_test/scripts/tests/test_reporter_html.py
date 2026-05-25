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
        },
        "processes": [{
            "name": "com.example.app", "uptime_ratio": 1.0, "restart_count": 0,
            "events": {"java_crash": 1, "native_crash": 0, "anr": 0, "process_death": 0},
        }],
        "incidents": [{
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
        }],
        "lifecycle_events": [],
        "bookmarks": [{"timestamp": "2026-05-21 10:02:00.000", "label": "b"}],
        "data_files": {"events": [], "lifecycle": [], "logcat": []},
    }
    written = html.write(result, tmp_path)
    text = written.read_text()
    assert "Stability report" in text
    assert "com.example.app" in text
    assert "Plotly.newPlot" in text
    # Counters block + incident details rendered
    assert "Java crash" in text
    assert "boom" in text
