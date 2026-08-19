from __future__ import annotations

import subprocess
from pathlib import Path

from sat import matrix as matrix_module
from sat.matrix import _sanitize, launch_package_on, run_matrix


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


def test_serial_is_sanitized_for_output_directory():
    assert _sanitize("10.0.2.2:5555/device name") == "10.0.2.2_5555_device_name"


def test_launch_package_uses_resolved_activity(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if "resolve-activity" in command[-1]:
            return subprocess.CompletedProcess(command, 0, "com.example.app/.Main\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(matrix_module.subprocess, "run", fake_run)
    monkeypatch.setattr(matrix_module.time, "sleep", lambda _: None)

    launch_package_on("serial-1", "com.example.app")

    assert len(calls) == 2
    assert calls[1][0] == [
        "adb", "-s", "serial-1", "shell", "am", "start", "-n", "com.example.app/.Main",
    ]


def test_launch_package_falls_back_to_monkey(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "not found")

    monkeypatch.setattr(matrix_module.subprocess, "run", fake_run)
    monkeypatch.setattr(matrix_module.time, "sleep", lambda _: None)

    launch_package_on("serial-1", "com.example.app")

    assert calls[-1] == [
        "adb", "-s", "serial-1", "shell", "monkey", "-p", "com.example.app",
        "-c", "android.intent.category.LAUNCHER", "1",
    ]


def test_matrix_default_worker_builds_isolated_sat_command(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(matrix_module.subprocess, "run", fake_run)
    results = run_matrix(
        package="com.example.app",
        devices=["emulator:5554"],
        output_root=tmp_path,
        duration_sec=17,
        extra_args=["--ci"],
    )

    assert results[0].returncode == 0
    assert results[0].output_dir == tmp_path / "device_emulator_5554"
    command, kwargs = calls[0]
    assert command[1:4] == ["-m", "sat", "--package"]
    assert command[-1] == "--ci"
    assert "17s" in command
    assert kwargs["timeout"] == 3600.0


def test_matrix_records_unexpected_worker_exception(tmp_path: Path):
    def fail(**kwargs):
        raise RuntimeError("worker exploded")

    result = run_matrix(
        package="com.example.app",
        devices=["serial-1"],
        output_root=tmp_path,
        duration_sec=1,
        run_one=fail,
    )[0]
    assert result.returncode == 2
    assert result.timed_out is False
    assert result.error == "worker exploded"
