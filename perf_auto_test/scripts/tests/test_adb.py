"""Basic tests for the Adb wrapper — mock subprocess, no real adb."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pat.adb import (
    Adb,
    AdbError,
    AdbNotFound,
    AdbTimeout,
    DEFAULT_BACKOFF_BASE,
)


def _completed(rc: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


class TestAdbRun:
    def test_returns_result_on_success(self):
        adb = Adb()
        with patch("subprocess.run", return_value=_completed(stdout="ok")):
            r = adb.run(["devices"])
        assert r.returncode == 0
        assert r.stdout == "ok"

    def test_serial_passed_through(self):
        adb = Adb(serial="ABC123")
        with patch("subprocess.run", return_value=_completed()) as m:
            adb.run(["devices"])
        cmd = m.call_args[0][0]
        assert cmd[:3] == ["adb", "-s", "ABC123"]

    def test_no_serial_omits_flag(self):
        adb = Adb()
        with patch("subprocess.run", return_value=_completed()) as m:
            adb.run(["devices"])
        cmd = m.call_args[0][0]
        assert cmd == ["adb", "devices"]

    def test_check_true_raises_on_nonzero_rc(self):
        adb = Adb(retries=0)
        with patch("subprocess.run", return_value=_completed(rc=1, stderr="oops")):
            with pytest.raises(AdbError, match="rc=1"):
                adb.run(["devices"])

    def test_check_false_returns_failed_result(self):
        adb = Adb(retries=0)
        with patch("subprocess.run", return_value=_completed(rc=1, stderr="oops")):
            r = adb.run(["devices"], check=False)
        assert r.returncode == 1

    def test_timeout_raises_adb_timeout(self):
        adb = Adb(retries=0, timeout=0.1)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 0.1)):
            with pytest.raises(AdbTimeout):
                adb.run(["devices"])

    def test_missing_adb_raises_adb_not_found(self):
        adb = Adb(retries=0)
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(AdbNotFound):
                adb.run(["devices"])


class TestAdbRetry:
    def test_retries_then_succeeds(self):
        adb = Adb(retries=2)
        results = [
            _completed(rc=1, stderr="transient"),
            _completed(rc=1, stderr="transient"),
            _completed(rc=0, stdout="ok"),
        ]
        with patch("subprocess.run", side_effect=results), \
             patch("time.sleep") as sleep_mock:
            r = adb.run(["devices"])
        assert r.returncode == 0
        # Slept between attempts: 2 sleeps for 3 attempts.
        assert sleep_mock.call_count == 2

    def test_retries_exhausted_raises(self):
        adb = Adb(retries=2)
        with patch("subprocess.run", return_value=_completed(rc=1, stderr="dead")), \
             patch("time.sleep"):
            with pytest.raises(AdbError):
                adb.run(["devices"])

    def test_backoff_doubles(self):
        adb = Adb(retries=3)
        with patch("subprocess.run", return_value=_completed(rc=1)), \
             patch("time.sleep") as sleep_mock:
            with pytest.raises(AdbError):
                adb.run(["devices"])
        delays = [c.args[0] for c in sleep_mock.call_args_list]
        # base * 2**attempt for attempt = 0,1,2
        assert delays == [
            DEFAULT_BACKOFF_BASE,
            DEFAULT_BACKOFF_BASE * 2,
            DEFAULT_BACKOFF_BASE * 4,
        ]

    def test_zero_retries_means_one_attempt(self):
        adb = Adb(retries=0)
        with patch("subprocess.run", return_value=_completed(rc=1)) as m, \
             patch("time.sleep"):
            with pytest.raises(AdbError):
                adb.run(["devices"])
        assert m.call_count == 1


class TestAdbHelpers:
    def test_shell_wraps_args(self):
        adb = Adb()
        with patch("subprocess.run", return_value=_completed()) as m:
            adb.shell("getprop ro.product.model")
        cmd = m.call_args[0][0]
        assert cmd == ["adb", "shell", "getprop ro.product.model"]

    def test_pull_wraps_args(self):
        adb = Adb()
        with patch("subprocess.run", return_value=_completed()) as m:
            adb.pull("/data/local/tmp/x", "./x")
        cmd = m.call_args[0][0]
        assert cmd == ["adb", "pull", "/data/local/tmp/x", "./x"]

    def test_list_devices_strips_header(self):
        adb = Adb()
        out = "List of devices attached\nABC123\tdevice\nXYZ789\toffline\n"
        with patch("subprocess.run", return_value=_completed(stdout=out)):
            devices = adb.list_devices()
        assert devices == ["ABC123\tdevice", "XYZ789\toffline"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
