"""Tests for markdown and html renderers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pat.reporter import html as html_mod
from pat.reporter import markdown as md_mod
from pat.reporter import result as result_mod
from pat.storage import (
    CPU_COLUMNS,
    CPU_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    MEM_COLUMNS,
    MEM_SCHEMA_TAG,
    CsvStreamWriter,
)


def _utc(year=2026, month=5, day=15, hour=10, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture
def populated(tmp_path: Path) -> tuple:
    """Build a synthetic run dir and the built result."""
    clk = _utc()
    cpu = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG, clock=lambda: clk)
    mem = CsvStreamWriter(tmp_path, "mem", MEM_COLUMNS, MEM_SCHEMA_TAG, clock=lambda: clk)
    life = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG, clock=lambda: clk)
    life.write_row({"timestamp": _iso(_utc()), "process_name": "com.foo",
                    "event": "new", "old_pid": 0, "new_pid": 100, "gap_sec": 0.0})
    life.write_row({"timestamp": _iso(_utc(minute=5)), "process_name": "com.foo",
                    "event": "restart", "old_pid": 100, "new_pid": 101, "gap_sec": 0.0})
    for i in range(5):
        cpu.write_row({"timestamp": _iso(_utc(minute=i)), "process_name": "com.foo",
                       "pid": 100, "cpu_pct": 90.0 if i < 2 else 5.0})
        mem.write_row({"timestamp": _iso(_utc(minute=i)), "process_name": "com.foo",
                       "pid": 100, "pss_mb": 600.0 if i < 2 else 150.0,
                       "java_heap_mb": 50, "native_heap_mb": 60, "graphics_mb": 100,
                       "code_mb": 80, "stack_mb": 10})
    cpu.close()
    mem.close()
    life.close()

    incidents_dir = tmp_path / "incidents"
    incidents_dir.mkdir()
    import json
    (incidents_dir / "cpu_2026-05-15T10-01-00.000Z_com.foo_pid100.json").write_text(json.dumps({
        "type": "cpu_threshold", "process": "com.foo", "pid": 100,
        "triggered_at": _iso(_utc(minute=1)),
        "threshold": {"metric": "cpu_pct", "value": 80, "sustain_sec": 60, "cooldown_sec": 300},
        "observed": {"value_at_trigger": 90.0, "duration_above_sec": 60.0, "peak": 95.0},
        "evidence": {
            "raw_file": "cpu_..._pid100.txt",
            "top_threads": [{"tid": 101, "name": "GLThread-21", "cpu_pct": 70.0}],
            "top_threads_count": 5,
        },
    }))
    (incidents_dir / "heap_2026-05-15T10-02-00.000Z_com.foo_pid100.json").write_text(json.dumps({
        "type": "mem_threshold", "process": "com.foo", "pid": 100,
        "triggered_at": _iso(_utc(minute=2)),
        "threshold": {"metric": "mem_pss_mb", "value": 500, "sustain_sec": 120, "cooldown_sec": 600},
        "observed": {"value_at_trigger": 600.0, "duration_above_sec": 120.0, "peak": 650.0},
        "evidence": {
            "heap_status": "fallback",
            "fallback_reason": "non-debuggable",
            "hprof_file": None, "hprof_size_bytes": 0,
            "meminfo_file": "heap_..._.meminfo.txt",
            "meminfo_parsed_file": "heap_..._.meminfo.json",
            "top_categories": [{"name": "Graphics", "pss_mb": 200.0}],
        },
    }))

    result = result_mod.build(
        output_dir=tmp_path, package="com.foo",
        started_at=_utc(), ended_at=_utc(minute=10),
        device={"serial": "ABC", "android_version": "12", "sdk_int": 31, "cpu_cores": 8},
        config_effective={"thresholds": {"cpu": {"percent": 80}, "mem": {"pss_mb": 500}}},
        exit_code=1, exit_reason="alerts_fired",
    )
    return tmp_path, result


# -----------------------------------------------------------------------------
# Markdown
# -----------------------------------------------------------------------------

class TestMarkdown:
    def test_has_all_sections(self, populated):
        _, result = populated
        md = md_mod.render(result)
        for header in ("# Perf Report", "## Run", "## Discovered Processes",
                       "## Stats per Process", "## Incidents",
                       "## Lifecycle Events", "## Bookmarks", "## Files"):
            assert header in md, f"missing section: {header}"

    def test_run_metadata_shown(self, populated):
        _, result = populated
        md = md_mod.render(result)
        assert "com.foo" in md
        assert "ABC" in md
        assert "Android=12" in md
        assert "Exit code" in md

    def test_incident_ids_present(self, populated):
        _, result = populated
        md = md_mod.render(result)
        assert "incident-001" in md
        assert "incident-002" in md

    def test_top_thread_name_in_incident_detail(self, populated):
        _, result = populated
        md = md_mod.render(result)
        # AI should be able to grep for the suspect thread.
        assert "GLThread-21" in md

    def test_heap_fallback_reason_in_incident_detail(self, populated):
        _, result = populated
        md = md_mod.render(result)
        assert "non-debuggable" in md
        assert "fallback" in md

    def test_lifecycle_restart_event_shown(self, populated):
        _, result = populated
        md = md_mod.render(result)
        assert "restart" in md.lower()

    def test_empty_result_renders_without_crash(self, tmp_path):
        result = result_mod.build(
            output_dir=tmp_path, package="com.empty",
            started_at=_utc(), ended_at=_utc(minute=1),
            device={"serial": "X", "android_version": "10", "sdk_int": 29, "cpu_cons": 4},
            config_effective={}, exit_code=2, exit_reason="setup_failed",
        )
        md = md_mod.render(result)
        assert "## Run" in md
        assert "(no processes)" in md

    def test_write_produces_file(self, populated):
        tmp_path, result = populated
        path = md_mod.write(result, tmp_path)
        assert path.name == "summary.md"
        assert path.exists()
        assert "Perf Report" in path.read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# HTML
# -----------------------------------------------------------------------------

class TestHtml:
    def test_render_produces_html(self, populated):
        tmp_path, result = populated
        html = html_mod.render(result, tmp_path)
        assert "<html" in html.lower()
        # Plotly CDN reference is present
        assert "plotly" in html.lower()

    def test_render_contains_process_name(self, populated):
        tmp_path, result = populated
        html = html_mod.render(result, tmp_path)
        assert "com.foo" in html

    def test_render_with_empty_run_does_not_crash(self, tmp_path):
        result = result_mod.build(
            output_dir=tmp_path, package="com.empty",
            started_at=_utc(), ended_at=_utc(minute=1),
            device={"serial": "X", "android_version": "10", "sdk_int": 29, "cpu_cores": 4},
            config_effective={}, exit_code=0, exit_reason="duration_elapsed",
        )
        html = html_mod.render(result, tmp_path)
        assert "<html" in html.lower()

    def test_write_produces_file(self, populated):
        tmp_path, result = populated
        path = html_mod.write(result, tmp_path)
        assert path.name == "report.html"
        assert path.exists()
        assert path.stat().st_size > 1000  # Plotly bundle reference + chart data
