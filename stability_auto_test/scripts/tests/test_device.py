from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sat import device as device_module
from sat.adb import AdbError, AdbNotFound, AdbResult
from sat.device import (
    DeviceInfo,
    DeviceSetupError,
    _read_cpu_cores,
    get_device_info,
    is_package_installed,
    list_devices,
    preflight,
    select_device,
)


class _FakeAdb:
    def __init__(self, shell_responses=None, run_error=None):
        self.serial = None
        self.shell_responses = shell_responses or {}
        self.run_error = run_error
        self.run_calls = []

    def run(self, args, **kwargs):
        self.run_calls.append((args, kwargs))
        if self.run_error:
            raise self.run_error
        return AdbResult(0, "", "", 0)

    def shell(self, command, **kwargs):
        value = self.shell_responses.get(command, AdbResult(1, "", "", 0))
        if isinstance(value, Exception):
            raise value
        return value


def test_list_devices_accepts_tab_and_whitespace_formats():
    adb = _FakeAdb()
    adb.run = MagicMock(
        return_value=AdbResult(
            0,
            "List of devices attached\nserial-1\tdevice\nserial-2 offline\nbad-row\n",
            "",
            0,
        )
    )
    assert list_devices(adb) == [("serial-1", "device"), ("serial-2", "offline")]
    adb.run.assert_called_once_with(["devices"], retries=0)


@pytest.mark.parametrize(
    ("serial", "devices", "expected"),
    [
        (None, [("only", "device")], "only"),
        ("chosen", [("other", "offline"), ("chosen", "device")], "chosen"),
    ],
)
def test_select_device_returns_only_or_explicit_online_device(serial, devices, expected):
    assert select_device(serial, devices) == expected


@pytest.mark.parametrize(
    ("serial", "devices", "message"),
    [
        (None, [], "no online devices"),
        ("missing", [("online", "device")], "not online"),
        (None, [("a", "device"), ("b", "device")], "pass --device"),
    ],
)
def test_select_device_rejects_ambiguous_or_unavailable_targets(serial, devices, message):
    with pytest.raises(DeviceSetupError, match=message):
        select_device(serial, devices)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (AdbResult(0, "package:com.example.app\n", "", 0), True),
        (AdbResult(0, "package:com.example.app.debug\n", "", 0), False),
        (AdbResult(1, "", "failure", 0), False),
        (AdbError("offline"), False),
    ],
)
def test_package_check_requires_exact_successful_match(response, expected):
    adb = _FakeAdb({"pm list packages com.example.app": response})
    assert is_package_installed(adb, "com.example.app") is expected


def test_device_info_uses_cpuinfo_fallback_and_tolerates_bad_sdk():
    adb = _FakeAdb(
        {
            "getprop ro.build.version.release": AdbResult(0, "VanillaIceCream\n", "", 0),
            "getprop ro.build.version.sdk": AdbResult(0, "unknown\n", "", 0),
            "nproc": AdbResult(0, "not-a-number\n", "", 0),
            "cat /proc/cpuinfo": AdbResult(
                0,
                "processor\t: 0\nmodel name: x\nprocessor\t: 1\n",
                "",
                0,
            ),
        }
    )

    assert get_device_info(adb, serial="emulator-5554") == DeviceInfo(
        serial="emulator-5554",
        android_version="VanillaIceCream",
        sdk_int=0,
        cpu_cores=2,
    )


@pytest.mark.parametrize(
    "responses",
    [
        {"nproc": AdbError("unsupported"), "cat /proc/cpuinfo": AdbError("offline")},
        {
            "nproc": AdbResult(0, "unavailable", "", 0),
            "cat /proc/cpuinfo": AdbResult(1, "", "", 0),
        },
    ],
)
def test_cpu_core_probe_has_safe_minimum(responses):
    assert _read_cpu_cores(_FakeAdb(responses)) == 1


def test_preflight_binds_auto_selected_device_and_returns_info(monkeypatch):
    adb = _FakeAdb()
    expected = DeviceInfo("serial-1", "14", 34, 8)
    monkeypatch.setattr(device_module, "list_devices", lambda value: [("serial-1", "device")])
    monkeypatch.setattr(device_module, "is_package_installed", lambda value, package: True)
    monkeypatch.setattr(device_module, "get_device_info", lambda value, serial: expected)

    assert preflight(adb, serial=None, package="com.example.app") == expected
    assert adb.serial == "serial-1"
    assert adb.run_calls == [(["version"], {"retries": 0, "timeout": 3.0})]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (AdbNotFound("missing"), "adb not found"),
        (AdbError("server unavailable"), "adb not usable"),
    ],
)
def test_preflight_translates_adb_startup_failures(error, message):
    with pytest.raises(DeviceSetupError, match=message):
        preflight(_FakeAdb(run_error=error), serial=None, package="com.example.app")


def test_preflight_rejects_missing_package(monkeypatch):
    adb = _FakeAdb()
    monkeypatch.setattr(device_module, "list_devices", lambda value: [("serial-1", "device")])
    monkeypatch.setattr(device_module, "is_package_installed", lambda value, package: False)

    with pytest.raises(DeviceSetupError, match="not installed"):
        preflight(adb, serial=None, package="com.example.missing")

    assert adb.serial == "serial-1"
