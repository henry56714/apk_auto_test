"""Unit tests for dumpers — top -H parser + thread_cpu.run + heap.run (mocked adb)."""

from __future__ import annotations

import json
import pathlib
from typing import Dict
from unittest.mock import MagicMock

import pytest

from perf_auto_test.adb import AdbResult
from perf_auto_test.alerting import AlertEvent
from perf_auto_test.discovery import Process
from perf_auto_test.dumpers import heap as heap_dumper
from perf_auto_test.dumpers import thread_cpu as thread_cpu_dumper
from perf_auto_test.dumpers.thread_cpu import parse_top_h

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _mk_result(stdout="", rc=0, stderr="") -> AdbResult:
    return AdbResult(returncode=rc, stdout=stdout, stderr=stderr, duration_sec=0.0)


def _mk_adb(shell_responses: Dict[str, AdbResult] = None,
            run_responses: Dict[str, AdbResult] = None) -> MagicMock:
    shell_responses = shell_responses or {}
    run_responses = run_responses or {}
    adb = MagicMock()
    shell_keys = sorted(shell_responses.keys(), key=len, reverse=True)
    run_keys = sorted(run_responses.keys(), key=len, reverse=True)

    def fake_shell(cmd, **kwargs):
        for k in shell_keys:
            if k in cmd:
                return shell_responses[k]
        return _mk_result(rc=1, stderr=f"unmatched shell: {cmd}")

    def fake_pull(remote, local, **kwargs):
        return run_responses.get(f"pull:{remote}", _mk_result(rc=1, stderr="no pull stub"))

    adb.shell.side_effect = fake_shell
    adb.pull.side_effect = fake_pull
    return adb


def _alert(value=95.0, metric="cpu_pct") -> AlertEvent:
    return AlertEvent(
        metric=metric, triggered_at=1234567890.0,
        value_at_trigger=value, duration_above_sec=72.0, peak=98.1,
        threshold_value=80.0, sustain_sec=60.0, cooldown_sec=300.0,
    )


# -----------------------------------------------------------------------------
# parse_top_h
# -----------------------------------------------------------------------------

class TestParseTopH:
    def test_returns_sorted_by_cpu_desc(self):
        threads = parse_top_h(_read("top_h_sample.txt"))
        assert threads, "expected non-empty"
        cpus = [t["cpu_pct"] for t in threads]
        assert cpus == sorted(cpus, reverse=True)

    def test_keeps_max_cpu_across_snapshots(self):
        """The same tid appears in multiple snapshots; we should keep the
        highest CPU% seen."""
        threads = parse_top_h(_read("top_h_sample.txt"))
        glthread = next(t for t in threads if t["tid"] == 12350)
        # Fixture has GLThread-21 at 45.2 in snapshot 1, 52.7 in snapshot 2.
        assert glthread["cpu_pct"] == 52.7

    def test_thread_names_captured(self):
        threads = parse_top_h(_read("top_h_sample.txt"))
        names = {t["name"] for t in threads}
        assert "GLThread-21" in names
        assert "RenderThread" in names

    def test_top_thread_is_glthread(self):
        threads = parse_top_h(_read("top_h_sample.txt"))
        assert threads[0]["name"] == "GLThread-21"
        assert threads[0]["cpu_pct"] == 52.7

    def test_empty_input(self):
        assert parse_top_h("") == []

    def test_no_header_returns_empty(self):
        text = "  1234 some random text without a header\n"
        assert parse_top_h(text) == []

    def test_android15_layout_with_tid_and_thread_columns(self):
        """Real-device regression: Android 15 emulator uses `TID` (not `PID`)
        and a `THREAD PROCESS` layout where the last column is the parent
        process name, not the thread name."""
        threads = parse_top_h(_read("top_h_android15.txt"))
        assert threads, "expected non-empty"
        # Top thread on idle sample app: RenderThread @ 8%
        assert threads[0]["name"] == "RenderThread"
        assert threads[0]["cpu_pct"] > 0
        # Must NOT use the PROCESS column as the thread name.
        names = {t["name"] for t in threads}
        assert "com.example.testapp" not in names


# -----------------------------------------------------------------------------
# thread_cpu.run
# -----------------------------------------------------------------------------

class TestThreadCpuRun:
    def test_writes_three_files_and_returns_metadata(self, tmp_path: pathlib.Path):
        adb = _mk_adb(shell_responses={
            "top -H -p": _mk_result(_read("top_h_sample.txt")),
            "/proc/12345/task": _mk_result("12345 (com.foo) S ...\n"),
        })
        proc = Process(pid=12345, name="com.example.app")
        result = thread_cpu_dumper.run(adb, proc, _alert(), tmp_path)
        assert result is not None
        assert result["type"] == "cpu_threshold"
        assert result["process"] == "com.example.app"
        assert result["pid"] == 12345
        # Files exist
        files = sorted(p.name for p in tmp_path.iterdir())
        assert any(f.startswith("cpu_") and f.endswith(".txt") and "task_stat" not in f
                   for f in files)
        assert any(f.startswith("cpu_") and f.endswith(".json") for f in files)
        assert any("task_stat.txt" in f for f in files)

    def test_json_has_top_threads(self, tmp_path: pathlib.Path):
        adb = _mk_adb(shell_responses={
            "top -H -p": _mk_result(_read("top_h_sample.txt")),
            "/proc/12345/task": _mk_result(""),
        })
        proc = Process(pid=12345, name="com.example.app")
        thread_cpu_dumper.run(adb, proc, _alert(), tmp_path)
        json_files = list(tmp_path.glob("cpu_*pid*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert "top_threads" in data["evidence"]
        assert data["evidence"]["top_threads"][0]["name"] == "GLThread-21"
        assert data["observed"]["value_at_trigger"] == 95.0
        assert data["threshold"]["value"] == 80.0

    def test_returns_none_on_top_failure(self, tmp_path: pathlib.Path):
        adb = MagicMock()
        adb.shell.side_effect = lambda cmd, **kw: _mk_result(rc=1, stderr="oops")
        proc = Process(pid=12345, name="com.foo")
        # top failure: still writes the (empty) raw file, returns metadata with
        # empty top_threads.
        result = thread_cpu_dumper.run(adb, proc, _alert(), tmp_path)
        assert result is not None
        assert result["evidence"]["top_threads"] == []


# -----------------------------------------------------------------------------
# heap.run
# -----------------------------------------------------------------------------

class TestHeapRun:
    def _setup_meminfo(self):
        return _read("meminfo_android12.txt")

    def test_happy_path_hprof_ok(self, tmp_path: pathlib.Path, monkeypatch):
        # Make stable-file waiting return immediately.
        monkeypatch.setattr(heap_dumper, "_wait_for_stable_file",
                            lambda *a, **kw: 12345)
        adb = _mk_adb(shell_responses={
            "dumpsys meminfo": _mk_result(self._setup_meminfo()),
            "am dumpheap": _mk_result(rc=0),
            "rm -f": _mk_result(rc=0),
        })
        # Simulate successful pull by having the .hprof appear locally.
        def fake_pull(remote, local, **kw):
            pathlib.Path(local).write_bytes(b"\x00" * 100)
            return _mk_result(rc=0)
        adb.pull.side_effect = fake_pull

        proc = Process(pid=12345, name="com.example.app")
        result = heap_dumper.run(adb, proc, _alert(metric="mem_pss_mb"),
                                 tmp_path, enable_heap=True)
        assert result is not None
        assert result["evidence"]["heap_status"] == "ok"
        assert result["evidence"]["hprof_file"] is not None
        # All four files exist.
        assert any(f.endswith(".meminfo.txt") for f in (p.name for p in tmp_path.iterdir()))
        assert any(f.endswith(".meminfo.json") for f in (p.name for p in tmp_path.iterdir()))
        assert any(f.endswith(".hprof") for f in (p.name for p in tmp_path.iterdir()))
        assert any(f.endswith("_pid12345.json") for f in (p.name for p in tmp_path.iterdir()))

    def test_dumpheap_failure_falls_back(self, tmp_path: pathlib.Path):
        adb = _mk_adb(shell_responses={
            "dumpsys meminfo": _mk_result(self._setup_meminfo()),
            "am dumpheap": _mk_result(rc=255, stderr="cannot dump heap of non-debuggable process"),
            "rm -f": _mk_result(rc=0),
        })
        proc = Process(pid=12345, name="com.example.app")
        result = heap_dumper.run(adb, proc, _alert(metric="mem_pss_mb"),
                                 tmp_path, enable_heap=True)
        assert result["evidence"]["heap_status"] == "fallback"
        assert "non-debuggable" in result["evidence"]["fallback_reason"]
        assert result["evidence"]["hprof_file"] is None
        # meminfo files still produced
        assert any(f.endswith(".meminfo.txt") for f in (p.name for p in tmp_path.iterdir()))

    def test_no_stable_file_falls_back(self, tmp_path: pathlib.Path, monkeypatch):
        """If am dumpheap returns 0 but no file appears, that's a fallback too."""
        monkeypatch.setattr(heap_dumper, "_wait_for_stable_file",
                            lambda *a, **kw: 0)
        adb = _mk_adb(shell_responses={
            "dumpsys meminfo": _mk_result(self._setup_meminfo()),
            "am dumpheap": _mk_result(rc=0),
            "rm -f": _mk_result(rc=0),
        })
        proc = Process(pid=12345, name="com.example.app")
        result = heap_dumper.run(adb, proc, _alert(metric="mem_pss_mb"),
                                 tmp_path, enable_heap=True)
        assert result["evidence"]["heap_status"] == "fallback"
        assert "not produced" in result["evidence"]["fallback_reason"]

    def test_enable_heap_false_skips(self, tmp_path: pathlib.Path):
        adb = _mk_adb(shell_responses={
            "dumpsys meminfo": _mk_result(self._setup_meminfo()),
        })
        proc = Process(pid=12345, name="com.example.app")
        result = heap_dumper.run(adb, proc, _alert(metric="mem_pss_mb"),
                                 tmp_path, enable_heap=False)
        assert result["evidence"]["heap_status"] == "skipped"
        assert "disabled" in result["evidence"]["fallback_reason"]
        # meminfo still captured
        assert any(f.endswith(".meminfo.txt") for f in (p.name for p in tmp_path.iterdir()))

    def test_meminfo_parsed_top_categories(self, tmp_path: pathlib.Path):
        adb = _mk_adb(shell_responses={
            "dumpsys meminfo": _mk_result(self._setup_meminfo()),
        })
        proc = Process(pid=12345, name="com.example.app")
        result = heap_dumper.run(adb, proc, _alert(metric="mem_pss_mb"),
                                 tmp_path, enable_heap=False)
        cats = result["evidence"]["top_categories"]
        assert cats, "expected non-empty top_categories"
        # Sorted descending by pss_mb
        pss_vals = [c["pss_mb"] for c in cats]
        assert pss_vals == sorted(pss_vals, reverse=True)
        # Android 12 fixture: Graphics dominates (35000 KB)
        assert cats[0]["name"] == "Graphics"

    def test_meminfo_failure_no_top_categories(self, tmp_path: pathlib.Path):
        adb = _mk_adb(shell_responses={
            "dumpsys meminfo": _mk_result(rc=1),
        })
        proc = Process(pid=12345, name="com.foo")
        result = heap_dumper.run(adb, proc, _alert(metric="mem_pss_mb"),
                                 tmp_path, enable_heap=False)
        # Without meminfo, top_categories should be empty.
        assert result["evidence"]["top_categories"] == []
