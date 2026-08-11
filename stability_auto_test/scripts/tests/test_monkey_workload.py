from __future__ import annotations

from unittest.mock import MagicMock

from sat.workloads.monkey import MonkeyWorkload


def _adb(rc: int = 0):
    adb = MagicMock()
    adb.shell.return_value = MagicMock(returncode=rc, stdout="")
    return adb


def test_same_seed_generates_same_command():
    a = MonkeyWorkload(_adb(), "com.example.app", seed=7, event_count=100,
                       throttle_ms=20)
    b = MonkeyWorkload(_adb(), "com.example.app", seed=7, event_count=100,
                       throttle_ms=20)
    assert a.manifest()["command"] == b.manifest()["command"]
    assert "-s 7" in a._cmd()
    assert "--throttle 20" in a._cmd()


def test_monkey_failure_is_reported():
    w = MonkeyWorkload(_adb(rc=1), "com.example.app")
    result = w.run()
    assert result.status == "failed"
    assert result.exit_code == 1
