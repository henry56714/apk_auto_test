"""Tests for reporter.result.build — synthetic output dir → report dict."""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

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

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "report.schema.json"
DEVICE = {"serial": "ABC", "android_version": "12", "sdk_int": 31, "cpu_cores": 8}
CONFIG = {"thresholds": {"cpu": {"percent": 80}}}


def _utc(year=2026, month=5, day=15, hour=10, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def populated_dir(tmp_path: Path) -> Path:
    """An output dir that mimics the layout produced by a real run."""
    fixed_clock = _utc()
    cpu = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG,
                          clock=lambda: fixed_clock)
    mem = CsvStreamWriter(tmp_path, "mem", MEM_COLUMNS, MEM_SCHEMA_TAG,
                          clock=lambda: fixed_clock)
    life = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG,
                           clock=lambda: fixed_clock)

    # Main process lifecycle: new → still alive at run end
    life.write_row({
        "timestamp": _ts(_utc(minute=0)), "process_name": "com.foo",
        "event": "new", "old_pid": 0, "new_pid": 100, "gap_sec": 0.0,
    })
    # Remote process lifecycle: new → restart → gone
    life.write_row({
        "timestamp": _ts(_utc(minute=1)), "process_name": "com.foo:remote",
        "event": "new", "old_pid": 0, "new_pid": 200, "gap_sec": 0.0,
    })
    life.write_row({
        "timestamp": _ts(_utc(minute=3)), "process_name": "com.foo:remote",
        "event": "restart", "old_pid": 200, "new_pid": 201, "gap_sec": 0.0,
    })
    life.write_row({
        "timestamp": _ts(_utc(minute=8)), "process_name": "com.foo:remote",
        "event": "gone", "old_pid": 0, "new_pid": 201, "gap_sec": 0.0,
    })

    # CPU samples for main process: a spike, a steady tail
    for i in range(10):
        cpu.write_row({
            "timestamp": _ts(_utc(minute=i)),
            "process_name": "com.foo", "pid": 100,
            "cpu_pct": 95.0 if i < 3 else 10.0,
        })

    # Mem samples for main process: steady ~300MB
    for i in range(10):
        mem.write_row({
            "timestamp": _ts(_utc(minute=i)),
            "process_name": "com.foo", "pid": 100,
            "pss_mb": 300.0, "java_heap_mb": 50.0, "native_heap_mb": 60.0,
            "graphics_mb": 100.0, "code_mb": 80.0, "stack_mb": 10.0,
        })

    cpu.close()
    mem.close()
    life.close()

    # One CPU incident for main process
    incidents_dir = tmp_path / "incidents"
    incidents_dir.mkdir()
    (incidents_dir / "cpu_2026-05-15T10-02-00.000Z_com.foo_pid100.json").write_text(
        json.dumps({
            "type": "cpu_threshold",
            "process": "com.foo",
            "pid": 100,
            "triggered_at": _ts(_utc(minute=2)),
            "threshold": {"metric": "cpu_pct", "value": 80, "sustain_sec": 60, "cooldown_sec": 300},
            "observed": {"value_at_trigger": 95.0, "duration_above_sec": 60.0, "peak": 95.0},
            "evidence": {
                "raw_file": "cpu_2026-05-15T10-02-00.000Z_com.foo_pid100.txt",
                "top_threads": [{"tid": 101, "name": "RenderThread", "cpu_pct": 75.0}],
                "top_threads_count": 5,
            },
        }, indent=2),
        encoding="utf-8",
    )
    # And a sibling meminfo.json that should be IGNORED by the reporter
    (incidents_dir / "heap_2026-05-15T10-03-00.000Z_com.foo_pid100.meminfo.json").write_text(
        json.dumps({"captured_at": "...", "top_categories": []}), encoding="utf-8",
    )
    # ... but the *actual* heap incident json should be picked up.
    (incidents_dir / "heap_2026-05-15T10-03-00.000Z_com.foo_pid100.json").write_text(
        json.dumps({
            "type": "mem_threshold",
            "process": "com.foo",
            "pid": 100,
            "triggered_at": _ts(_utc(minute=3)),
            "threshold": {"metric": "mem_pss_mb", "value": 250, "sustain_sec": 120, "cooldown_sec": 600},
            "observed": {"value_at_trigger": 305.0, "duration_above_sec": 120.0, "peak": 305.0},
            "evidence": {
                "heap_status": "fallback",
                "fallback_reason": "non-debuggable",
                "hprof_file": None,
                "hprof_size_bytes": 0,
                "meminfo_file": "heap_..._.meminfo.txt",
                "meminfo_parsed_file": "heap_..._.meminfo.json",
                "top_categories": [{"name": "Graphics", "pss_mb": 100.0}],
            },
        }, indent=2),
        encoding="utf-8",
    )

    return tmp_path


def _build(populated_dir: Path) -> dict:
    return result_mod.build(
        output_dir=populated_dir,
        package="com.foo",
        started_at=_utc(minute=0),
        ended_at=_utc(minute=10),
        device=DEVICE,
        config_effective=CONFIG,
        exit_code=0,
        exit_reason="duration_elapsed",
    )


class TestBuild:
    def test_schema_version(self, populated_dir):
        r = _build(populated_dir)
        assert r["schema_version"] == "1.0"

    def test_run_block(self, populated_dir):
        r = _build(populated_dir)
        run = r["run"]
        assert run["package"] == "com.foo"
        assert run["exit_code"] == 0
        assert run["exit_reason"] == "duration_elapsed"
        assert run["duration_sec"] == 600.0
        assert run["device"]["serial"] == "ABC"
        assert run["config_effective"] == CONFIG

    def test_processes_discovered_from_all_sources(self, populated_dir):
        r = _build(populated_dir)
        names = sorted(p["name"] for p in r["processes"])
        assert names == ["com.foo", "com.foo:remote"]

    def test_cpu_stats_computed(self, populated_dir):
        r = _build(populated_dir)
        main = next(p for p in r["processes"] if p["name"] == "com.foo")
        cpu_stats = main["stats"]["cpu_pct"]
        # 3 spikes at 95 + 7 steady at 10 → max=95, samples=10
        assert cpu_stats["max"] == 95.0
        assert cpu_stats["samples"] == 10

    def test_mem_stats_computed(self, populated_dir):
        r = _build(populated_dir)
        main = next(p for p in r["processes"] if p["name"] == "com.foo")
        mem_stats = main["stats"]["mem_pss_mb"]
        assert mem_stats["mean"] == 300.0
        assert mem_stats["samples"] == 10

    def test_remote_process_no_samples_yields_null_stats(self, populated_dir):
        r = _build(populated_dir)
        remote = next(p for p in r["processes"] if p["name"] == "com.foo:remote")
        assert remote["stats"]["cpu_pct"] is None
        assert remote["stats"]["mem_pss_mb"] is None

    def test_uptime_ratio_main_process(self, populated_dir):
        r = _build(populated_dir)
        main = next(p for p in r["processes"] if p["name"] == "com.foo")
        # Main process was alive entire run (new at minute 0, never went gone).
        assert main["uptime_ratio"] == 1.0
        assert main["restart_count"] == 0

    def test_uptime_ratio_remote_partial(self, populated_dir):
        r = _build(populated_dir)
        remote = next(p for p in r["processes"] if p["name"] == "com.foo:remote")
        # Alive from minute 1 to minute 8 (with a restart in between) = ~7/10 of run.
        assert 0.6 <= remote["uptime_ratio"] <= 0.8
        assert remote["restart_count"] == 1

    def test_incidents_assigned_ids(self, populated_dir):
        r = _build(populated_dir)
        ids = [i["id"] for i in r["incidents"]]
        assert ids == ["incident-001", "incident-002"]

    def test_incidents_sorted_by_trigger_time(self, populated_dir):
        r = _build(populated_dir)
        times = [i["triggered_at"] for i in r["incidents"]]
        assert times == sorted(times)

    def test_meminfo_json_not_treated_as_incident(self, populated_dir):
        r = _build(populated_dir)
        # The sibling `.meminfo.json` should be skipped (it lacks "type" anyway,
        # but we filter by name suffix too).
        assert len(r["incidents"]) == 2

    def test_alerts_count_per_process(self, populated_dir):
        r = _build(populated_dir)
        main = next(p for p in r["processes"] if p["name"] == "com.foo")
        assert main["alerts"] == {"cpu": 1, "mem": 1}
        remote = next(p for p in r["processes"] if p["name"] == "com.foo:remote")
        assert remote["alerts"] == {"cpu": 0, "mem": 0}

    def test_sample_failures_default_zero(self, populated_dir):
        r = _build(populated_dir)
        for p in r["processes"]:
            assert p["sample_failures"] == {"cpu": 0, "mem": 0}

    def test_sample_failures_populated_from_pool(self, populated_dir):
        r = result_mod.build(
            output_dir=populated_dir,
            package="com.foo",
            started_at=_utc(minute=0),
            ended_at=_utc(minute=10),
            device=DEVICE,
            config_effective=CONFIG,
            exit_code=0,
            exit_reason="duration_elapsed",
            sample_failures={"com.foo": {"cpu": 0, "mem": 14000}},
        )
        main = next(p for p in r["processes"] if p["name"] == "com.foo")
        assert main["sample_failures"] == {"cpu": 0, "mem": 14000}

    def test_sample_failures_creates_process_with_no_other_data(self, tmp_path: Path):
        """A process that only ever produced sample failures (no CSV rows,
        no lifecycle, no incidents) should still appear in the report so the
        operator can see something went wrong."""
        r = result_mod.build(
            output_dir=tmp_path,
            package="com.ghost",
            started_at=_utc(minute=0),
            ended_at=_utc(minute=5),
            device=DEVICE,
            config_effective={},
            exit_code=0,
            exit_reason="duration_elapsed",
            sample_failures={"com.ghost": {"cpu": 0, "mem": 60}},
        )
        ghost = next(p for p in r["processes"] if p["name"] == "com.ghost")
        assert ghost["sample_failures"] == {"cpu": 0, "mem": 60}
        assert ghost["stats"]["mem_pss_mb"] is None

    def test_lifecycle_events_round_trip(self, populated_dir):
        r = _build(populated_dir)
        events = r["lifecycle_events"]
        assert len(events) == 4
        # All required keys present and types correct
        for e in events:
            assert isinstance(e["timestamp"], str)
            assert e["event"] in ("new", "gone", "restart")
            assert isinstance(e["old_pid"], int)
            assert isinstance(e["new_pid"], int)

    def test_data_files_listed(self, populated_dir):
        r = _build(populated_dir)
        assert any(f.startswith("cpu_") for f in r["data_files"]["cpu"])
        assert any(f.startswith("mem_") for f in r["data_files"]["mem"])
        assert any(f.startswith("lifecycle_") for f in r["data_files"]["lifecycle"])


class TestSchemaValidation:
    def test_built_result_matches_schema(self, populated_dir, schema):
        r = _build(populated_dir)
        jsonschema.validate(r, schema)

    def test_empty_run_matches_schema(self, tmp_path, schema):
        # No CSVs, no incidents — should still produce a valid (mostly-empty) result.
        r = result_mod.build(
            output_dir=tmp_path,
            package="com.empty",
            started_at=_utc(),
            ended_at=_utc(minute=5),
            device=DEVICE,
            config_effective={},
            exit_code=2,
            exit_reason="setup_failed",
        )
        jsonschema.validate(r, schema)
        assert r["processes"] == []
        assert r["incidents"] == []

    def test_write_creates_report_json(self, populated_dir, schema):
        r = _build(populated_dir)
        path = result_mod.write(r, populated_dir)
        assert path.name == "report.json"
        assert path.exists()
        round_trip = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.validate(round_trip, schema)
