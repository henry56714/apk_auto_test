from __future__ import annotations

import json
from pathlib import Path

import pytest
from sat import cli
from sat.device import DeviceSetupError
from sat.doctor import run_doctor


class _R:
    def __init__(self, rc: int, stdout: str):
        self.returncode = rc
        self.stdout = stdout


class _FakeAdb:
    def __init__(self, devices_text: str, package: str = "com.example.app"):
        self.serial = None
        self._devices_text = devices_text
        self._package = package

    def run(self, args, **kw):
        if args and args[0] == "devices":
            return _R(0, self._devices_text)
        return _R(0, "")

    def shell(self, cmd, **kw):
        if cmd.startswith("getprop ro.build.version.release"):
            return _R(0, "14")
        if cmd.startswith("getprop ro.build.version.sdk"):
            return _R(0, "34")
        if cmd.startswith("nproc"):
            return _R(0, "8")
        if cmd.startswith("pm list packages"):
            return _R(0, f"package:{self._package}\n")
        if cmd.startswith("pidof"):
            return _R(0, "1234")
        if cmd.startswith("logcat -d"):
            return _R(0, "05-21 10:00:00.000 1 1 I x: y\n")
        if cmd.startswith("dumpsys dropbox"):
            return _R(0, "Drop box contents: 1 entries\n")
        if cmd.startswith("ls /data/tombstones") or cmd.startswith("ls /data/anr"):
            return _R(1, "")
        return _R(0, "")


def _devices_text(*entries):
    return "List of devices attached\n" + "".join(
        f"{serial}\t{state}\n" for serial, state in entries
    )


def test_doctor_reports_ok_with_capability_unavailable(tmp_path: Path):
    adb = _FakeAdb(_devices_text(("serial-1", "device")))
    result = run_doctor(adb, "com.example.app", output_dir=tmp_path / "out")
    assert result["ok"] is True
    statuses = {c["name"]: c["status"] for c in result["checks"]}
    assert statuses["tombstone_permission"] == "unavailable"
    assert statuses["anr_trace_permission"] == "unavailable"
    assert statuses["logcat_buffer"] == "ok"


def test_doctor_json_stdout_parseable(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "Adb",
        lambda serial=None, **kw: _FakeAdb(
            _devices_text(("serial-1", "device")),
        ),
    )
    rc = cli.main([
        "doctor", "--package", "com.example.app", "--json",
        "--output-dir", str(tmp_path / "out"),
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["package"] == "com.example.app"


def test_doctor_no_devices():
    adb = _FakeAdb("List of devices attached\n")
    with pytest.raises(DeviceSetupError, match="no devices"):
        run_doctor(adb, "com.example.app")


def test_doctor_unauthorized_device():
    adb = _FakeAdb(_devices_text(("serial-1", "unauthorized")))
    with pytest.raises(DeviceSetupError, match="unauthorized"):
        run_doctor(adb, "com.example.app", device="serial-1")


def test_doctor_offline_device():
    adb = _FakeAdb(_devices_text(("serial-1", "offline")))
    with pytest.raises(DeviceSetupError, match="offline"):
        run_doctor(adb, "com.example.app", device="serial-1")


def test_doctor_multiple_devices_requires_serial():
    adb = _FakeAdb(_devices_text(
        ("serial-1", "device"), ("serial-2", "device"),
    ))
    with pytest.raises(DeviceSetupError, match="multiple devices"):
        run_doctor(adb, "com.example.app")


def test_doctor_device_error_json_stdout(monkeypatch, capsys):
    def boom(adb, package, *, device=None, output_dir=None):
        raise DeviceSetupError("no online devices")

    monkeypatch.setattr(cli, "run_doctor", boom)
    rc = cli.main(["doctor", "--package", "com.example.app", "--json"])
    assert rc == cli.EXIT_SETUP
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["error"]
