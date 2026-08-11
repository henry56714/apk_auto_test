from __future__ import annotations

import subprocess
from pathlib import Path

from sat.matrix import run_matrix


def _fake_run(rc: int = 0):
    def run_one(*, device: str, output_dir: Path):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / "report.json").write_text(
            f'{{"device": "{device}", "rc": {rc}}}',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess([], rc, "", "")

    return run_one


def test_matrix_runs_each_device_in_own_dir(tmp_path: Path):
    results = run_matrix(
        package="com.example.app",
        devices=["serial-1", "serial-2"],
        output_root=tmp_path / "matrix",
        duration_sec=10,
        max_parallel=2,
        run_one=_fake_run(0),
    )
    assert len(results) == 2
    assert results[0].output_dir != results[1].output_dir
    assert all(r.returncode == 0 for r in results)
    assert all((r.output_dir / "report.json").exists() for r in results)


def test_matrix_isolates_failures(tmp_path: Path):
    def run_one(*, device: str, output_dir: Path):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess([], 1 if device == "a" else 0, "", "")

    results = run_matrix(
        package="com.example.app",
        devices=["a", "b"],
        output_root=tmp_path / "matrix",
        duration_sec=10,
        max_parallel=2,
        run_one=run_one,
    )
    assert len(results) == 2
    assert sum(1 for r in results if r.returncode == 0) == 1
    assert sum(1 for r in results if r.returncode == 1) == 1


def test_matrix_timeout_recorded(tmp_path: Path):
    def run_one(*, device: str, output_dir: Path):
        raise subprocess.TimeoutExpired(["sat"], timeout=1)

    results = run_matrix(
        package="com.example.app",
        devices=["a"],
        output_root=tmp_path / "matrix",
        duration_sec=10,
        device_timeout_sec=1,
        run_one=run_one,
    )
    assert results[0].timed_out is True


def test_missing_worker_report_still_degrades_aggregate(tmp_path: Path):
    """When one device's worker produces no report, aggregate must list both
    devices with degraded health (not pretend the missing device doesn't exist)."""
    from sat.aggregate import aggregate_reports

    reports = [
        {
            "run": {
                "device": {"serial": "device-1", "android_version": "14"},
                "package": "com.x",
            },
            "verdict": "stable",
            "collection_health": "healthy",
            "incidents": [],
            "issue_groups": [],
            "device_events": [],
            "coverage_ratio": 0.99,
        },
        # device-2 was offline — worker never produced a report.
        # Pass empty dict to simulate the missing report.
        {
            "run": {
                "device": {"serial": "device-2", "android_version": "?"},
            },
        },
    ]
    agg = aggregate_reports(reports)
    assert agg["device_count"] == 2, (
        f"aggregate must list all {2} devices, got {agg['device_count']}"
    )
    assert agg["aggregate_health"] == "degraded", (
        f"health must be degraded when a device is missing, got {agg['aggregate_health']}"
    )
    serials = [d["serial"] for d in agg["devices"]]
    assert "device-1" in serials
    assert "device-2" in serials
    # device-2 should show as missing.
    missing = [d for d in agg["devices"] if d["serial"] == "device-2"]
    assert len(missing) == 1
    assert missing[0]["status"] != "ok"
