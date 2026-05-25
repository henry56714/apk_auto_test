from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sat.api import StabilityConfig, StabilityTest
from sat.device import DeviceInfo, DeviceSetupError
from sat.discovery import Process


def _cfg(tmp_path: Path) -> StabilityConfig:
    return StabilityConfig(
        package="com.example.app",
        output_dir=tmp_path / "out",
        wait_timeout_sec=1.0,
        rescan_interval_sec=10.0,
        logcat_enabled=False,
        emit_html=False,
        status_interval_sec=10.0,
    )


def _fake_adb():
    adb = MagicMock()
    adb.serial = "test-serial"
    return adb


def _patch_preflight(monkeypatch):
    monkeypatch.setattr(
        "sat.api.preflight",
        lambda adb, *, serial, package: DeviceInfo(
            serial=serial or "test-serial",
            android_version="14",
            sdk_int=34,
            cpu_cores=4,
        ),
    )


def test_context_manager_writes_report(tmp_path: Path, monkeypatch):
    _patch_preflight(monkeypatch)
    discover = lambda adb, pkg: [Process(pid=1234, name=pkg)]
    monkeypatch.setattr("sat.api.wait_for_processes",
                        lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)])

    with StabilityTest(_cfg(tmp_path), adb=_fake_adb(), discover_fn=discover) as t:
        t.bookmark("scenario_a_done")

    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["schema_version"] == "1.0"
    assert report["run"]["package"] == "com.example.app"
    assert report["run"]["exit_code"] == 0
    assert any(b["label"] == "scenario_a_done" for b in report["bookmarks"])


def test_setup_failure_writes_minimal_report_and_raises(tmp_path: Path, monkeypatch):
    def fail(adb, *, serial, package):
        raise DeviceSetupError("no device")
    monkeypatch.setattr("sat.api.preflight", fail)

    cfg = _cfg(tmp_path)
    with pytest.raises(DeviceSetupError):
        StabilityTest(cfg, adb=_fake_adb()).start()
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["run"]["exit_code"] == 2
    assert report["run"]["exit_reason"] == "setup_failed"


def test_wait_timeout_writes_report_and_raises(tmp_path: Path, monkeypatch):
    _patch_preflight(monkeypatch)
    monkeypatch.setattr("sat.api.wait_for_processes",
                        lambda adb, pkg, *, timeout_sec: [])
    cfg = _cfg(tmp_path)
    with pytest.raises(TimeoutError):
        StabilityTest(cfg, adb=_fake_adb()).start()
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["run"]["exit_code"] == 3
    assert report["run"]["exit_reason"] == "wait_timeout"


def test_duration_sec_reflects_monotonic_not_wall_clock(tmp_path: Path, monkeypatch):
    """When system sleeps, wall clock advances but `time.monotonic` does not.

    The reported `duration_sec` must follow monotonic so it matches the
    configured run budget, never the inflated wall-clock delta.
    """
    _patch_preflight(monkeypatch)
    discover = lambda adb, pkg: [Process(pid=1234, name=pkg)]
    monkeypatch.setattr("sat.api.wait_for_processes",
                        lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)])

    # Fake monotonic: start at 100.0, advance by 3600s ("1h of active runtime")
    # Wall clock (datetime.now) is untouched, so it will look like much less
    # or more time than 1h — the report must trust monotonic.
    values = iter([100.0, 3700.0])
    monkeypatch.setattr("sat.api.time.monotonic", lambda: next(values))

    with StabilityTest(_cfg(tmp_path), adb=_fake_adb(), discover_fn=discover):
        pass

    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["run"]["duration_sec"] == pytest.approx(3600.0, abs=0.01)


def test_exception_in_with_block_marks_exit(tmp_path: Path, monkeypatch):
    _patch_preflight(monkeypatch)
    discover = lambda adb, pkg: [Process(pid=1234, name=pkg)]
    monkeypatch.setattr("sat.api.wait_for_processes",
                        lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)])
    with pytest.raises(RuntimeError):
        with StabilityTest(_cfg(tmp_path), adb=_fake_adb(), discover_fn=discover) as t:
            raise RuntimeError("user code blew up")
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["run"]["exit_reason"] == "exception"
    assert report["run"]["exit_code"] >= 1
