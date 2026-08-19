from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest
from sat import adb as adb_module
from sat.adb import Adb, AdbError, AdbNotFound, AdbResult, AdbTimeout


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_run_builds_serial_command_and_preserves_output(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(stdout="ok\ufffd")

    monkeypatch.setattr(adb_module.subprocess, "run", fake_run)
    adb = Adb("serial-1", adb_path="/opt/adb", timeout=7.0, retries=0)

    result = adb.run(["shell", "getprop ro.product.model"])

    assert result.stdout == "ok\ufffd"
    assert result.duration_sec >= 0
    assert calls == [
        (
            ["/opt/adb", "-s", "serial-1", "shell", "getprop ro.product.model"],
            {
                "capture_output": True,
                "text": True,
                "errors": "replace",
                "timeout": 7.0,
            },
        )
    ]


def test_run_retries_with_exponential_backoff(monkeypatch):
    outcomes = iter([
        _completed(returncode=1, stderr="transport closed"),
        _completed(stdout="recovered"),
    ])
    sleeps = []
    monkeypatch.setattr(adb_module.subprocess, "run", lambda *a, **kw: next(outcomes))
    monkeypatch.setattr(adb_module.time, "sleep", sleeps.append)

    adb = Adb(retries=1)
    result = adb.run(["devices"])

    assert result.stdout == "recovered"
    assert sleeps == [adb_module.DEFAULT_BACKOFF_BASE]
    assert adb.failure_count == 0


def test_run_exhaustion_raises_and_counts_one_failed_call(monkeypatch):
    monkeypatch.setattr(
        adb_module.subprocess,
        "run",
        lambda *a, **kw: _completed(returncode=17, stderr="permission denied"),
    )
    monkeypatch.setattr(adb_module.time, "sleep", lambda _: None)
    adb = Adb(retries=2)

    with pytest.raises(AdbError, match=r"rc=17.*permission denied"):
        adb.run(["pull", "/data/a", "/tmp/a"])

    assert adb.failure_count == 1


def test_run_timeout_is_typed_and_counted(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(adb_module.subprocess, "run", timeout)
    adb = Adb(timeout=1.25, retries=0)

    with pytest.raises(AdbTimeout, match=r"1\.25s"):
        adb.run(["shell", "sleep 60"])

    assert adb.failure_count == 1


def test_missing_adb_has_actionable_error(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(adb_module.subprocess, "run", missing)

    with pytest.raises(AdbNotFound, match="/missing/adb"):
        Adb(adb_path="/missing/adb", retries=3).run(["version"])


def test_check_false_returns_nonzero_result_without_retry(monkeypatch):
    run = MagicMock(return_value=_completed(returncode=1, stdout="not installed"))
    monkeypatch.setattr(adb_module.subprocess, "run", run)

    result = Adb(retries=3).run(["shell", "pm path com.missing"], check=False)

    assert result.returncode == 1
    assert result.stdout == "not installed"
    run.assert_called_once()


def test_shell_pull_and_device_listing_delegate_to_run():
    adb = Adb("serial-1")
    adb.run = MagicMock(
        side_effect=[
            AdbResult(0, "shell-out", "", 0.01),
            AdbResult(0, "pull-out", "", 0.01),
            AdbResult(
                0,
                "List of devices attached\nserial-1\tdevice\n\nserial-2\toffline\n",
                "",
                0.01,
            ),
        ]
    )

    assert adb.shell("id", timeout=2).stdout == "shell-out"
    assert adb.pull("/remote", "/local", check=False).stdout == "pull-out"
    assert adb.list_devices() == ["serial-1\tdevice", "serial-2\toffline"]
    assert adb.run.call_args_list[0].args == (["shell", "id"],)
    assert adb.run.call_args_list[1].args == (["pull", "/remote", "/local"],)
    assert adb.run.call_args_list[2] == ((["devices"],), {"retries": 0})


def test_default_command_has_no_empty_serial_argument():
    adb = Adb()
    assert adb._base_cmd() == ["adb"]
