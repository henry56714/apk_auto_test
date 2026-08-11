from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from sat.api import StabilityConfig, StabilityTest
from sat.device import DeviceInfo
from sat.discovery import Process


def _cfg(tmp_path: Path, dashboard: bool) -> StabilityConfig:
    return StabilityConfig(
        package="com.example.app",
        output_dir=tmp_path / "out",
        wait_timeout_sec=1.0,
        rescan_interval_sec=10.0,
        logcat_enabled=False,
        emit_html=False,
        status_interval_sec=10.0,
        dashboard=dashboard,
    )


def _patch(monkeypatch):
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


def test_dashboard_enabled_creates_localhost_server(tmp_path: Path, monkeypatch):
    _patch(monkeypatch)

    class FakeLive:
        def __init__(self, **kw):
            self.host = kw.get("host")
            self._status_query = kw.get("status_query")

        def start(self):
            pass

        def stop(self):
            pass

        @property
        def bound_port(self):
            return 0

        def handle(self, path, **kw):
            if path == "/api/status":
                data = json.dumps(self._status_query() or {})
                return 200, data.encode(), "application/json"
            return 404, b"", "text/plain"

    monkeypatch.setattr("sat.api.LiveServer", FakeLive)
    t = StabilityTest(_cfg(tmp_path, True), adb=MagicMock(),
                      discover_fn=lambda adb, pkg: [Process(pid=1234, name=pkg)])
    t.start()
    assert t._live is not None
    assert t._live.host == "127.0.0.1"
    code, body, _ = t._live.handle("/api/status")
    assert code == 200
    assert json.loads(body)["run_id"] == t._run_id
    t.stop()


def test_dashboard_disabled_has_no_server(tmp_path: Path, monkeypatch):
    _patch(monkeypatch)
    t = StabilityTest(_cfg(tmp_path, False), adb=MagicMock(),
                      discover_fn=lambda adb, pkg: [Process(pid=1234, name=pkg)])
    t.start()
    assert t._live is None
    t.stop()
