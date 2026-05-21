"""Unit tests for device.py — pre-flight checks (mock adb)."""

from __future__ import annotations

from typing import Dict
from unittest.mock import MagicMock

import pytest

from pat import device
from pat.adb import AdbResult
from pat.device import DeviceSetupError


def _mk_result(stdout: str = "", rc: int = 0, stderr: str = "") -> AdbResult:
    return AdbResult(returncode=rc, stdout=stdout, stderr=stderr, duration_sec=0.0)


def _mk_adb(shell_responses: Dict[str, AdbResult] = None,
            run_responses: Dict[str, AdbResult] = None,
            serial=None) -> MagicMock:
    """Mock Adb. Match by longest-substring on cmd."""
    shell_responses = shell_responses or {}
    run_responses = run_responses or {}
    adb = MagicMock()
    adb.serial = serial

    shell_keys = sorted(shell_responses.keys(), key=len, reverse=True)
    run_keys = sorted(run_responses.keys(), key=len, reverse=True)

    def fake_shell(cmd: str, **kwargs) -> AdbResult:
        for k in shell_keys:
            if k in cmd:
                return shell_responses[k]
        return _mk_result(rc=1, stderr=f"unmatched shell: {cmd}")

    def fake_run(args, **kwargs) -> AdbResult:
        cmd = " ".join(args)
        for k in run_keys:
            if k in cmd:
                return run_responses[k]
        return _mk_result(rc=1, stderr=f"unmatched run: {cmd}")

    adb.shell.side_effect = fake_shell
    adb.run.side_effect = fake_run
    return adb


class TestListDevices:
    def test_parses_tab_separated(self):
        out = "List of devices attached\nABC123\tdevice\nXYZ789\toffline\n"
        adb = _mk_adb(run_responses={"devices": _mk_result(out)})
        devs = device.list_devices(adb)
        assert devs == [("ABC123", "device"), ("XYZ789", "offline")]

    def test_empty_list(self):
        adb = _mk_adb(run_responses={"devices": _mk_result("List of devices attached\n")})
        assert device.list_devices(adb) == []

    def test_skips_header_and_blanks(self):
        out = "List of devices attached\n\nABC123\tdevice\n\n"
        adb = _mk_adb(run_responses={"devices": _mk_result(out)})
        assert device.list_devices(adb) == [("ABC123", "device")]


class TestSelectDevice:
    def test_single_online_auto_select(self):
        assert device.select_device(None, [("ABC", "device")]) == "ABC"

    def test_no_online_raises(self):
        with pytest.raises(DeviceSetupError, match="no online"):
            device.select_device(None, [("ABC", "offline")])

    def test_empty_list_raises(self):
        with pytest.raises(DeviceSetupError):
            device.select_device(None, [])

    def test_explicit_serial_match(self):
        assert device.select_device("ABC", [("ABC", "device"), ("XYZ", "device")]) == "ABC"

    def test_explicit_serial_not_online_raises(self):
        with pytest.raises(DeviceSetupError, match="not online"):
            device.select_device("XYZ", [("ABC", "device")])

    def test_multiple_online_no_serial_raises(self):
        with pytest.raises(DeviceSetupError, match="multiple|pass --device"):
            device.select_device(None, [("ABC", "device"), ("XYZ", "device")])


class TestIsPackageInstalled:
    def test_installed(self):
        adb = _mk_adb(shell_responses={
            "pm list packages com.example.app": _mk_result("package:com.example.app\n"),
        })
        assert device.is_package_installed(adb, "com.example.app") is True

    def test_not_installed(self):
        adb = _mk_adb(shell_responses={
            "pm list packages com.example.app": _mk_result(""),
        })
        assert device.is_package_installed(adb, "com.example.app") is False

    def test_prefix_collision_not_a_match(self):
        """`pm list packages com.foo` returns BOTH `com.foo` AND `com.foo.bar`
        if both are installed. We must only count exact `package:com.foo`."""
        adb = _mk_adb(shell_responses={
            "pm list packages com.foo": _mk_result(
                "package:com.foo.bar\npackage:com.foo.baz\n"
            ),
        })
        assert device.is_package_installed(adb, "com.foo") is False

    def test_rc_nonzero_returns_false(self):
        adb = _mk_adb(shell_responses={
            "pm list packages": _mk_result(rc=1, stderr="oops"),
        })
        assert device.is_package_installed(adb, "com.example.app") is False


class TestGetDeviceInfo:
    def test_collects_basics(self):
        adb = _mk_adb(shell_responses={
            "getprop ro.build.version.release": _mk_result("12\n"),
            "getprop ro.build.version.sdk": _mk_result("31\n"),
            "nproc": _mk_result("8\n"),
        })
        info = device.get_device_info(adb, serial="ABC")
        assert info.serial == "ABC"
        assert info.android_version == "12"
        assert info.sdk_int == 31
        assert info.cpu_cores == 8

    def test_nproc_fallback_to_cpuinfo(self):
        adb = _mk_adb(shell_responses={
            "getprop ro.build.version.release": _mk_result("10"),
            "getprop ro.build.version.sdk": _mk_result("29"),
            "nproc": _mk_result(rc=1),
            "/proc/cpuinfo": _mk_result(
                "processor\t: 0\nprocessor\t: 1\nprocessor\t: 2\nprocessor\t: 3\n"
            ),
        })
        info = device.get_device_info(adb)
        assert info.cpu_cores == 4

    def test_unknown_cores_defaults_to_one(self):
        adb = _mk_adb(shell_responses={
            "getprop ro.build.version.release": _mk_result("10"),
            "getprop ro.build.version.sdk": _mk_result("29"),
            "nproc": _mk_result(rc=1),
            "/proc/cpuinfo": _mk_result(rc=1),
        })
        info = device.get_device_info(adb)
        assert info.cpu_cores == 1


class TestPreflight:
    def _adb_full_happy(self, package="com.example.app", devices_out=None):
        devices_out = devices_out or "List of devices attached\nABC\tdevice\n"
        adb = _mk_adb(
            run_responses={
                "version": _mk_result("Android Debug Bridge version 1.0\n"),
                "devices": _mk_result(devices_out),
            },
            shell_responses={
                f"pm list packages {package}": _mk_result(f"package:{package}\n"),
                "getprop ro.build.version.release": _mk_result("12"),
                "getprop ro.build.version.sdk": _mk_result("31"),
                "nproc": _mk_result("4"),
            },
        )
        return adb

    def test_happy_path(self):
        adb = self._adb_full_happy()
        info = device.preflight(adb, serial=None, package="com.example.app")
        assert info.serial == "ABC"
        assert info.android_version == "12"

    def test_package_missing_raises(self):
        adb = _mk_adb(
            run_responses={
                "version": _mk_result(),
                "devices": _mk_result("List of devices attached\nABC\tdevice\n"),
            },
            shell_responses={
                "pm list packages com.no.such": _mk_result(""),
            },
        )
        with pytest.raises(DeviceSetupError, match="not installed"):
            device.preflight(adb, serial=None, package="com.no.such")

    def test_multiple_devices_without_serial_raises(self):
        adb = _mk_adb(
            run_responses={
                "version": _mk_result(),
                "devices": _mk_result(
                    "List of devices attached\nABC\tdevice\nXYZ\tdevice\n"
                ),
            },
        )
        with pytest.raises(DeviceSetupError, match="pass --device"):
            device.preflight(adb, serial=None, package="com.foo")
