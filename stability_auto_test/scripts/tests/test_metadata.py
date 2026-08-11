from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from sat.api import StabilityConfig, StabilityTest
from sat.device import DeviceInfo
from sat.discovery import Process
from sat.metadata import collect_app_metadata


def _fake_adb():
    adb = MagicMock()

    def shell(cmd, **kw):
        if cmd.startswith("dumpsys package"):
            return MagicMock(
                returncode=0,
                stdout="versionName=2.3.1\nversionCode=456\n",
            )
        if cmd.startswith("getprop ro.build.id"):
            return MagicMock(returncode=0, stdout="AA1A.240531.001")
        return MagicMock(returncode=0, stdout="")

    adb.shell.side_effect = shell
    return adb


def test_collect_app_metadata(monkeypatch):
    monkeypatch.setenv("SAT_GIT_SHA", "deadbeef")
    meta = collect_app_metadata(_fake_adb(), "com.example.app")
    assert meta["app_version_name"] == "2.3.1"
    assert meta["app_version_code"] == "456"
    assert meta["build_id"] == "AA1A.240531.001"
    assert meta["git_sha"] == "deadbeef"


def test_report_contains_app_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "sat.api.preflight",
        lambda adb, *, serial, package: DeviceInfo(
            serial="test-serial", android_version="14", sdk_int=34, cpu_cores=4,
        ),
    )
    monkeypatch.setattr(
        "sat.api.wait_for_processes",
        lambda adb, pkg, *, timeout_sec: [Process(pid=1234, name=pkg)],
    )
    monkeypatch.setattr(
        "sat.api.collect_app_metadata",
        lambda adb, package: {
            "app_version_name": "2.3.1",
            "app_version_code": "456",
            "build_id": "AA1A.240531.001",
            "git_sha": "deadbeef",
        },
    )
    cfg = StabilityConfig(
        package="com.example.app",
        output_dir=tmp_path / "out",
        wait_timeout_sec=1.0,
        rescan_interval_sec=10.0,
        logcat_enabled=False,
        emit_html=False,
        status_interval_sec=10.0,
    )
    t = StabilityTest(cfg, adb=MagicMock(),
                      discover_fn=lambda adb, pkg: [Process(pid=1234, name=pkg)])
    t.start()
    t.stop()
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    run = report["run"]
    assert run["app_version_name"] == "2.3.1"
    assert run["app_version_code"] == "456"
    assert run["build_id"] == "AA1A.240531.001"
    assert run["git_sha"] == "deadbeef"
