"""Parser-level tests for discovery (no adb / no device required)."""

from __future__ import annotations

import pathlib
from typing import Dict
from unittest.mock import MagicMock

import pytest

from pat import discovery
from pat.adb import AdbResult
from pat.discovery import (
    Process,
    parse_dumpsys_processes,
    parse_ps_old_output,
    parse_ps_output,
)

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# -----------------------------------------------------------------------------
# parse_ps_output  (modern Android 8+: `ps -A -o PID,NAME`)
# -----------------------------------------------------------------------------

class TestParsePsOutput:
    def test_finds_main_and_remote(self):
        cands = parse_ps_output(_read("ps_a_android12.txt"), "com.example.app")
        names = sorted(n for _, n in cands)
        assert names == ["com.example.app", "com.example.app:push", "com.example.app:remote"]

    def test_returns_pids_as_ints(self):
        cands = parse_ps_output(_read("ps_a_android10.txt"), "com.example.app")
        pids = sorted(p for p, _ in cands)
        assert pids == [1234, 1235]
        assert all(isinstance(p, int) for p, _ in cands)

    def test_no_match_returns_empty(self):
        cands = parse_ps_output(_read("ps_a_android12.txt"), "com.no.such.app")
        assert cands == []

    def test_chrome_sandbox_processes(self):
        cands = parse_ps_output(_read("ps_a_android12.txt"), "com.android.chrome")
        names = sorted(n for _, n in cands)
        assert names == [
            "com.android.chrome",
            "com.android.chrome:sandboxed_process0",
            "com.android.chrome:sandboxed_process1",
        ]

    def test_does_not_match_unrelated_prefix(self):
        """`com.example` must not match `com.example.app` (no `:` boundary)."""
        cands = parse_ps_output(_read("ps_a_android12.txt"), "com.example")
        assert cands == []

    def test_skips_header_line(self):
        cands = parse_ps_output("PID NAME\n1234 com.example.app\n", "com.example.app")
        assert cands == [(1234, "com.example.app")]

    def test_empty_input(self):
        assert parse_ps_output("", "com.foo") == []

    def test_truncated_name_returned_as_candidate(self):
        """COMM is limited to 15 chars; long package names get truncated in `ps`.
        Parser should still return them as candidates (caller will verify via cmdline)."""
        cands = parse_ps_output(_read("ps_a_truncated.txt"), "com.example.myapp")
        pids = sorted(p for p, _ in cands)
        assert pids == [1234, 1235]


# -----------------------------------------------------------------------------
# parse_ps_old_output  (Android <8 toolbox ps)
# -----------------------------------------------------------------------------

class TestParsePsOldOutput:
    def test_finds_app_processes(self):
        cands = parse_ps_old_output(_read("ps_old_android7.txt"), "com.example.app")
        names = sorted(n for _, n in cands)
        assert names == ["com.example.app", "com.example.app:remote"]

    def test_correct_pids(self):
        cands = parse_ps_old_output(_read("ps_old_android7.txt"), "com.example.app")
        pids = sorted(p for p, _ in cands)
        assert pids == [1234, 1235]

    def test_no_header_returns_empty(self):
        assert parse_ps_old_output("1234 com.foo\n", "com.foo") == []


# -----------------------------------------------------------------------------
# parse_dumpsys_processes  (fallback path)
# -----------------------------------------------------------------------------

class TestParseDumpsysProcesses:
    def test_finds_main_and_remote(self):
        cands = parse_dumpsys_processes(_read("dumpsys_processes.txt"), "com.example.app")
        names = sorted(n for _, n in cands)
        assert names == ["com.example.app", "com.example.app:remote"]

    def test_correct_pids(self):
        cands = parse_dumpsys_processes(_read("dumpsys_processes.txt"), "com.example.app")
        pids = sorted(p for p, _ in cands)
        assert pids == [1234, 1235]

    def test_dedup_across_sections(self):
        """dumpsys lists the same pid in multiple sections; should be deduped."""
        text = """  ProcessRecord{abc 1234:com.foo/u0a1}
  Proc # 0: trm: 0 1234:com.foo/u0a1 (started-services)
"""
        cands = parse_dumpsys_processes(text, "com.foo")
        assert cands == [(1234, "com.foo")]


# -----------------------------------------------------------------------------
# discover()  — integration with mocked Adb
# -----------------------------------------------------------------------------

def _mk_result(stdout: str = "", rc: int = 0, stderr: str = "") -> AdbResult:
    return AdbResult(returncode=rc, stdout=stdout, stderr=stderr, duration_sec=0.0)


def _mk_adb(shell_responses: Dict[str, AdbResult]) -> MagicMock:
    """Build a fake Adb whose `shell(cmd, ...)` returns based on substring match.

    Matches the LONGEST key first, so `"ps"` doesn't shadow `"dumpsys ..."`.
    """
    adb = MagicMock()
    keys_by_length = sorted(shell_responses.keys(), key=len, reverse=True)

    def fake_shell(cmd: str, **kwargs) -> AdbResult:
        for key in keys_by_length:
            if key in cmd:
                return shell_responses[key]
        return _mk_result("", rc=1, stderr=f"unmatched: {cmd}")

    adb.shell.side_effect = fake_shell
    return adb


class TestDiscover:
    def test_modern_ps_with_cmdline_verification(self):
        ps_out = _read("ps_a_android12.txt")
        adb = _mk_adb({
            "ps -A -o PID,NAME": _mk_result(ps_out),
            "/proc/1234/cmdline": _mk_result("com.example.app\x00"),
            "/proc/1235/cmdline": _mk_result("com.example.app:remote\x00"),
            "/proc/1500/cmdline": _mk_result("com.example.app:push\x00"),
        })
        procs = discovery.discover(adb, "com.example.app")
        names = sorted(p.name for p in procs)
        assert names == ["com.example.app", "com.example.app:push", "com.example.app:remote"]

    def test_truncated_resolved_via_cmdline(self):
        """`ps` returns truncated 15-char names; cmdline reveals the full name."""
        ps_out = _read("ps_a_truncated.txt")
        adb = _mk_adb({
            "ps -A -o PID,NAME": _mk_result(ps_out),
            "/proc/1234/cmdline": _mk_result("com.example.myapp\x00"),
            "/proc/1235/cmdline": _mk_result("com.example.myapp:remote\x00"),
        })
        procs = discovery.discover(adb, "com.example.myapp")
        names = sorted(p.name for p in procs)
        assert names == ["com.example.myapp", "com.example.myapp:remote"]

    def test_cmdline_mismatch_excludes_pid(self):
        """If cmdline reveals the candidate isn't ours, exclude it."""
        adb = _mk_adb({
            "ps -A -o PID,NAME": _mk_result("PID NAME\n1234 com.example.mya\n"),
            "/proc/1234/cmdline": _mk_result("com.example.myappial\x00"),
        })
        assert discovery.discover(adb, "com.example.myapp") == []

    def test_falls_back_to_old_ps_when_modern_empty(self):
        adb = _mk_adb({
            "ps -A -o PID,NAME": _mk_result("", rc=1, stderr="bad flag"),
            "ps": _mk_result(_read("ps_old_android7.txt")),
            "/proc/1234/cmdline": _mk_result("com.example.app\x00"),
            "/proc/1235/cmdline": _mk_result("com.example.app:remote\x00"),
        })
        procs = discovery.discover(adb, "com.example.app")
        names = sorted(p.name for p in procs)
        assert names == ["com.example.app", "com.example.app:remote"]

    def test_falls_back_to_dumpsys_when_ps_unavailable(self):
        adb = _mk_adb({
            "ps -A -o PID,NAME": _mk_result("", rc=1),
            "ps": _mk_result("", rc=1),
            "dumpsys activity processes": _mk_result(_read("dumpsys_processes.txt")),
            "/proc/1234/cmdline": _mk_result("com.example.app\x00"),
            "/proc/1235/cmdline": _mk_result("com.example.app:remote\x00"),
        })
        procs = discovery.discover(adb, "com.example.app")
        names = sorted(p.name for p in procs)
        assert names == ["com.example.app", "com.example.app:remote"]

    def test_all_sources_fail_returns_empty(self):
        adb = _mk_adb({
            "ps -A -o PID,NAME": _mk_result("", rc=1),
            "ps": _mk_result("", rc=1),
            "dumpsys": _mk_result("", rc=1),
        })
        assert discovery.discover(adb, "com.example.app") == []

    def test_cmdline_unreadable_trusts_ps_when_not_truncated(self):
        """If cmdline can't be read but ps name is < 15 chars (not truncated),
        we should still trust it."""
        adb = _mk_adb({
            "ps -A -o PID,NAME": _mk_result("PID NAME\n1234 com.foo.bar\n"),
            "/proc/1234/cmdline": _mk_result("", rc=1),
        })
        procs = discovery.discover(adb, "com.foo.bar")
        assert len(procs) == 1
        assert procs[0].name == "com.foo.bar"

    def test_cmdline_unreadable_at_truncation_excludes(self):
        """If cmdline can't be read AND ps name is at the 15-char limit, we
        can't tell if it's our process — exclude."""
        adb = _mk_adb({
            "ps -A -o PID,NAME": _mk_result("PID NAME\n1234 com.example.mya\n"),
            "/proc/1234/cmdline": _mk_result("", rc=1),
        })
        assert discovery.discover(adb, "com.example.myapp") == []


class TestProcessDataclass:
    def test_started_at_auto_populated(self):
        p = Process(pid=1234, name="com.foo")
        assert p.pid == 1234
        assert p.name == "com.foo"
        assert isinstance(p.started_at, float)
        assert p.started_at > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
