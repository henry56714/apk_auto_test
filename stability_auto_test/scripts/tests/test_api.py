from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sat.api import StabilityConfig, StabilityTest
from sat.detection import EVENT_JAVA_CRASH, StabilityEvent
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

    def discover(adb, pkg):
        return [Process(pid=1234, name=pkg)]

    monkeypatch.setattr(
        "sat.api.wait_for_processes", lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)]
    )

    with StabilityTest(_cfg(tmp_path), adb=_fake_adb(), discover_fn=discover) as t:
        t.bookmark("scenario_a_done")

    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["schema_version"] == "1.15"
    assert report["run"]["package"] == "com.example.app"
    assert report["run"]["exit_code"] == 0
    assert any(b["label"] == "scenario_a_done" for b in report["bookmarks"])


def test_late_incident_is_drained_into_report(tmp_path: Path, monkeypatch):
    """An incident dispatched right before stop() must be persisted to report."""
    _patch_preflight(monkeypatch)
    monkeypatch.setattr(
        "sat.api.wait_for_processes", lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)]
    )

    cfg = _cfg(tmp_path)
    t = StabilityTest(
        cfg, adb=_fake_adb(), discover_fn=lambda adb, pkg: [Process(pid=1234, name=pkg)]
    )
    t.start()
    t._pool._dispatch(
        StabilityEvent(
            event_type=EVENT_JAVA_CRASH,
            process="com.example.app",
            pid=1234,
            triggered_at="2026-05-21 10:00:00.000",
            summary="late boom",
            raw_lines=[
                "05-21 10:00:00.000  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
            ],
        )
    )
    t.stop()

    report = json.loads((cfg.output_dir / "report.json").read_text())
    assert len(report["incidents"]) == 1
    assert report["incidents"][0]["summary"] == "late boom"
    assert report["event_pipeline"]["detected_count"] == 1
    assert report["event_pipeline"]["persisted_count"] == 1
    assert report["event_pipeline"]["failed_count"] == 0
    assert report["event_pipeline"]["timed_out_count"] == 0


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
    monkeypatch.setattr("sat.api.wait_for_processes", lambda adb, pkg, *, timeout_sec: [])
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

    def discover(adb, pkg):
        return [Process(pid=1234, name=pkg)]

    monkeypatch.setattr(
        "sat.api.wait_for_processes", lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)]
    )

    # Fake monotonic: 100.0 through start()+pool.start(), then 3700.0 for the
    # observation window ("1h of active runtime") — wall clock (datetime.now)
    # is untouched, so the report must trust monotonic for duration_sec.
    calls = iter([100.0, 100.0, 3700.0])
    state = {"last": 100.0}

    def fake_monotonic() -> float:
        try:
            state["last"] = next(calls)
        except StopIteration:
            pass
        return state["last"]

    monkeypatch.setattr("sat.api.time.monotonic", fake_monotonic)

    with StabilityTest(_cfg(tmp_path), adb=_fake_adb(), discover_fn=discover):
        pass

    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["run"]["duration_sec"] == pytest.approx(3600.0, abs=0.01)


def test_exception_in_with_block_marks_exit(tmp_path: Path, monkeypatch):
    _patch_preflight(monkeypatch)

    def discover(adb, pkg):
        return [Process(pid=1234, name=pkg)]

    monkeypatch.setattr(
        "sat.api.wait_for_processes", lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)]
    )
    with pytest.raises(RuntimeError):
        with StabilityTest(_cfg(tmp_path), adb=_fake_adb(), discover_fn=discover):
            raise RuntimeError("user code blew up")
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["run"]["exit_reason"] == "exception"
    assert report["run"]["exit_code"] >= 1
