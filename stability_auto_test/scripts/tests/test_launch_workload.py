from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sat.adb import AdbError, AdbResult
from sat.workloads.launch import LaunchWorkload, resolve_launcher_activity

PACKAGE = "com.example.app"


def _adb(response=None):
    adb = MagicMock()
    adb.shell.return_value = response or AdbResult(0, "", "", 0)
    return adb


def test_resolve_launcher_activity_selects_only_target_package():
    adb = _adb(
        AdbResult(
            0,
            "priority=0 preferredOrder=0\ncom.other/.Main\ncom.example.app/.HomeActivity\n",
            "",
            0,
        )
    )
    assert resolve_launcher_activity(adb, PACKAGE) == f"{PACKAGE}/.HomeActivity"
    adb.shell.assert_called_once_with(
        "cmd package resolve-activity --brief -c android.intent.category.LAUNCHER com.example.app",
        check=False,
        timeout=8.0,
    )


@pytest.mark.parametrize(
    "response",
    [
        AdbResult(1, "", "not found", 0),
        AdbResult(0, "com.other/.Main\n", "", 0),
    ],
)
def test_resolve_launcher_activity_returns_none_for_unusable_output(response):
    assert resolve_launcher_activity(_adb(response), PACKAGE) is None


def test_resolve_launcher_activity_handles_adb_error():
    adb = _adb()
    adb.shell.side_effect = AdbError("offline")
    assert resolve_launcher_activity(adb, PACKAGE) is None


def test_launch_workload_uses_explicit_activity_and_records_manifest():
    adb = _adb()
    workload = LaunchWorkload(adb, PACKAGE, activity=f"{PACKAGE}/.Explicit")

    result = workload.run()

    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.started_at
    assert result.ended_at
    adb.shell.assert_called_once_with(
        "am start -W -n com.example.app/.Explicit",
        check=False,
        timeout=30.0,
    )
    assert workload.manifest() == {
        "type": "launch",
        "package": PACKAGE,
        "activity": f"{PACKAGE}/.Explicit",
    }


def test_launch_workload_falls_back_to_monkey_when_activity_unresolved():
    adb = _adb(AdbResult(0, "No activity found\n", "", 0))

    result = LaunchWorkload(adb, PACKAGE).run()

    assert result.status == "ok"
    assert adb.shell.call_count == 2
    assert adb.shell.call_args_list[-1] == (
        (
            "monkey -p com.example.app -c android.intent.category.LAUNCHER 1",
        ),
        {"check": False, "timeout": 30.0},
    )
