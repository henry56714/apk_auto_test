from __future__ import annotations

from types import SimpleNamespace

import pytest
from sat.adb import AdbError, AdbResult
from sat.discovery import (
    COMM_MAX,
    Process,
    _gather_candidates,
    discover,
    parse_dumpsys_processes,
    parse_ps_old_output,
    parse_ps_output,
    read_cmdline,
    wait_for_processes,
)

PACKAGE = "com.example.reallylongapp"


class _FakeAdb:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.commands = []

    def shell(self, command, **kwargs):
        self.commands.append((command, kwargs))
        value = self.responses.get(command, AdbResult(1, "", "", 0.0))
        if isinstance(value, Exception):
            raise value
        return value


def test_parse_modern_ps_accepts_exact_subprocess_and_truncated_candidate():
    truncated = PACKAGE[:COMM_MAX]
    text = (
        "PID NAME\n"
        f"101 {PACKAGE}\n"
        f"102 {PACKAGE}:remote\n"
        f"103 {truncated}\n"
        f"103 {truncated}\n"
        "104 com.example.other\n"
        "malformed\n"
    )

    assert parse_ps_output(text, PACKAGE) == [
        (101, PACKAGE),
        (102, f"{PACKAGE}:remote"),
        (103, truncated),
    ]


@pytest.mark.parametrize("text", ["", "USER PPID NAME\nroot 1 x"])
def test_parse_old_ps_requires_pid_header(text):
    assert parse_ps_old_output(text, PACKAGE) == []


def test_parse_old_ps_handles_bad_rows_and_deduplicates():
    text = (
        "USER PID PPID VSIZE RSS NAME\n"
        f"u0_a1 201 1 2 3 {PACKAGE}\n"
        f"u0_a1 nope 1 2 3 {PACKAGE}\n"
        f"u0_a1 201 1 2 3 {PACKAGE}:remote\n"
        "short\n"
    )
    assert parse_ps_old_output(text, PACKAGE) == [(201, PACKAGE)]


def test_parse_dumpsys_preserves_full_names_and_ignores_other_packages():
    text = (
        f"ProcessRecord{{x 301:{PACKAGE}/u0a12}} 302:{PACKAGE}:remote "
        "303:com.example.other 301:duplicate\n"
    )
    assert parse_dumpsys_processes(text, PACKAGE) == [
        (301, PACKAGE),
        (302, f"{PACKAGE}:remote"),
    ]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (AdbResult(0, f"{PACKAGE}:remote\x00ignored", "", 0), f"{PACKAGE}:remote"),
        (AdbResult(1, "", "denied", 0), ""),
        (AdbError("offline"), ""),
    ],
)
def test_read_cmdline_returns_canonical_name_or_empty(response, expected):
    adb = _FakeAdb({"cat /proc/9/cmdline 2>/dev/null": response})
    assert read_cmdline(adb, 9) == expected


def test_discover_verifies_truncation_and_trusts_only_safe_unreadable_names(monkeypatch):
    truncated = PACKAGE[:COMM_MAX]
    monkeypatch.setattr(
        "sat.discovery._gather_candidates",
        lambda adb, package: [
            (1, truncated),
            (2, truncated),
            (3, PACKAGE),
            (3, PACKAGE),
            (4, PACKAGE),
        ],
    )
    adb = _FakeAdb(
        {
            "cat /proc/1/cmdline 2>/dev/null": AdbResult(0, f"{PACKAGE}:remote\x00", "", 0),
            "cat /proc/2/cmdline 2>/dev/null": AdbResult(0, "com.example.other\x00", "", 0),
            "cat /proc/3/cmdline 2>/dev/null": AdbResult(1, "", "gone", 0),
            "cat /proc/4/cmdline 2>/dev/null": AdbResult(0, "com.example.other\x00", "", 0),
        }
    )

    processes = discover(adb, PACKAGE)

    assert [(p.pid, p.name) for p in processes] == [
        (1, f"{PACKAGE}:remote"),
    ]


def test_discover_trusts_unreadable_short_nontruncated_ps_name(monkeypatch):
    short_package = "com.short.app"
    monkeypatch.setattr(
        "sat.discovery._gather_candidates",
        lambda adb, package: [(7, short_package)],
    )
    adb = _FakeAdb(
        {
            "cat /proc/7/cmdline 2>/dev/null": AdbResult(1, "", "gone", 0),
        }
    )
    assert [(p.pid, p.name) for p in discover(adb, short_package)] == [
        (7, short_package),
    ]


def test_candidate_gathering_falls_back_to_dumpsys_after_ps_failures():
    adb = _FakeAdb(
        {
            "ps -A -o PID,NAME": AdbError("unsupported"),
            "ps": AdbResult(0, "USER PPID NAME\n", "", 0),
            "dumpsys activity processes": AdbResult(0, f"401:{PACKAGE}\n", "", 0),
        }
    )

    assert _gather_candidates(adb, PACKAGE) == [(401, PACKAGE)]
    assert [command for command, _ in adb.commands] == [
        "ps -A -o PID,NAME",
        "ps",
        "dumpsys activity processes",
    ]


def test_candidate_gathering_returns_empty_when_all_sources_fail():
    adb = _FakeAdb(
        {
            "ps -A -o PID,NAME": AdbError("offline"),
            "ps": AdbError("offline"),
            "dumpsys activity processes": AdbError("offline"),
        }
    )
    assert _gather_candidates(adb, PACKAGE) == []


def test_wait_for_processes_polls_until_process_appears(monkeypatch):
    found = [[], [Process(pid=501, name=PACKAGE)]]
    monkeypatch.setattr("sat.discovery.discover", lambda adb, package: found.pop(0))
    monkeypatch.setattr("sat.discovery.time.monotonic", lambda: 10.0)
    sleeps = []
    monkeypatch.setattr("sat.discovery.time.sleep", sleeps.append)

    result = wait_for_processes(SimpleNamespace(), PACKAGE, timeout_sec=3, poll_interval_sec=0.2)

    assert [p.pid for p in result] == [501]
    assert sleeps == [0.2]


def test_wait_for_processes_honors_timeout(monkeypatch):
    times = iter([10.0, 10.5, 11.1])
    monkeypatch.setattr("sat.discovery.discover", lambda adb, package: [])
    monkeypatch.setattr("sat.discovery.time.monotonic", lambda: next(times))
    monkeypatch.setattr("sat.discovery.time.sleep", lambda _: None)

    assert wait_for_processes(SimpleNamespace(), PACKAGE, timeout_sec=1) == []
