"""PerfTest library API tests — end-to-end with a mocked Adb."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List
from unittest.mock import MagicMock, patch

import pytest

from perf_auto_test import PerfConfig, PerfTest
from perf_auto_test.adb import AdbResult
from perf_auto_test.device import DeviceSetupError
from perf_auto_test.discovery import Process


def _mk_result(stdout="", rc=0, stderr="") -> AdbResult:
    return AdbResult(returncode=rc, stdout=stdout, stderr=stderr, duration_sec=0.0)


def _mk_happy_adb(package="com.foo") -> MagicMock:
    shell_map = {
        f"pm list packages {package}": _mk_result(f"package:{package}\n"),
        "getprop ro.build.version.release": _mk_result("12"),
        "getprop ro.build.version.sdk": _mk_result("31"),
        "nproc": _mk_result("4"),
    }
    run_map = {
        "version": _mk_result("Android Debug Bridge\n"),
        "devices": _mk_result("List of devices attached\nABC\tdevice\n"),
    }
    shell_keys = sorted(shell_map.keys(), key=len, reverse=True)
    run_keys = sorted(run_map.keys(), key=len, reverse=True)
    adb = MagicMock()
    adb.serial = None

    def fake_shell(cmd, **kwargs):
        for k in shell_keys:
            if k in cmd:
                return shell_map[k]
        return _mk_result(rc=1, stderr=f"unmatched shell: {cmd}")

    def fake_run(args, **kwargs):
        joined = " ".join(args)
        for k in run_keys:
            if k in joined:
                return run_map[k]
        return _mk_result(rc=1, stderr=f"unmatched run: {joined}")

    adb.shell.side_effect = fake_shell
    adb.run.side_effect = fake_run
    return adb


def _mk_failing_adb(missing_package=True) -> MagicMock:
    shell_map = {
        "pm list packages": _mk_result(""),  # package not installed
        "getprop ro.build.version.release": _mk_result("12"),
        "getprop ro.build.version.sdk": _mk_result("31"),
        "nproc": _mk_result("4"),
    }
    run_map = {
        "version": _mk_result(""),
        "devices": _mk_result("List of devices attached\nABC\tdevice\n"),
    }
    shell_keys = sorted(shell_map.keys(), key=len, reverse=True)
    run_keys = sorted(run_map.keys(), key=len, reverse=True)
    adb = MagicMock()
    adb.serial = None

    def fake_shell(cmd, **kwargs):
        for k in shell_keys:
            if k in cmd:
                return shell_map[k]
        return _mk_result(rc=1, stderr="unmatched")

    def fake_run(args, **kwargs):
        joined = " ".join(args)
        for k in run_keys:
            if k in joined:
                return run_map[k]
        return _mk_result(rc=1)

    adb.shell.side_effect = fake_shell
    adb.run.side_effect = fake_run
    return adb


class TestPerfConfig:
    def test_missing_package_raises(self, tmp_path):
        with pytest.raises(ValueError, match="package"):
            PerfConfig(package="", output_dir=tmp_path)

    def test_config_effective_is_json_safe(self, tmp_path):
        cfg = PerfConfig(package="com.foo", output_dir=tmp_path)
        d = cfg.config_effective()
        # Must round-trip through JSON without error
        json.loads(json.dumps(d))
        assert d["package"] == "com.foo"
        assert d["output_dir"] == str(tmp_path)


class TestPerfTestHappyPath:
    def test_produces_all_report_artifacts(self, tmp_path):
        proc = Process(pid=100, name="com.foo")
        adb = _mk_happy_adb()
        cfg = PerfConfig(
            package="com.foo",
            output_dir=tmp_path,
            wait_timeout_sec=2.0,
            cpu_interval_sec=0.05,
            mem_interval_sec=0.05,
            rescan_interval_sec=10,  # disable mid-run
            status_interval_sec=0.05,
            emit_junit=True,
        )
        with patch("perf_auto_test.discovery.discover", return_value=[proc]), \
             patch("perf_auto_test.pool.cpu_mod.sample", return_value=None), \
             patch("perf_auto_test.pool.mem_mod.sample", return_value=None):
            with PerfTest(cfg, adb=adb,
                          discover_fn=lambda a, p: [proc]) as t:
                time.sleep(0.20)

        for fname in ("report.json", "report.html",
                      "report.junit.xml", "bookmarks.jsonl", "status.json"):
            assert (tmp_path / fname).exists(), f"missing {fname}"

    def test_result_accessible_after_stop(self, tmp_path):
        proc = Process(pid=100, name="com.foo")
        adb = _mk_happy_adb()
        cfg = PerfConfig(package="com.foo", output_dir=tmp_path,
                         wait_timeout_sec=2.0,
                         cpu_interval_sec=0.05, mem_interval_sec=0.05,
                         rescan_interval_sec=10, status_interval_sec=10)
        with patch("perf_auto_test.discovery.discover", return_value=[proc]), \
             patch("perf_auto_test.pool.cpu_mod.sample", return_value=None), \
             patch("perf_auto_test.pool.mem_mod.sample", return_value=None):
            t = PerfTest(cfg, adb=adb, discover_fn=lambda a, p: [proc])
            t.start()
            time.sleep(0.10)
            t.stop()
        assert t.result["schema_version"] == "1.0"
        assert t.result["run"]["package"] == "com.foo"

    def test_bookmark_recorded_in_report(self, tmp_path):
        proc = Process(pid=100, name="com.foo")
        adb = _mk_happy_adb()
        cfg = PerfConfig(package="com.foo", output_dir=tmp_path,
                         wait_timeout_sec=2.0,
                         cpu_interval_sec=0.05, mem_interval_sec=0.05,
                         rescan_interval_sec=10, status_interval_sec=10)
        with patch("perf_auto_test.discovery.discover", return_value=[proc]), \
             patch("perf_auto_test.pool.cpu_mod.sample", return_value=None), \
             patch("perf_auto_test.pool.mem_mod.sample", return_value=None):
            with PerfTest(cfg, adb=adb, discover_fn=lambda a, p: [proc]) as t:
                t.bookmark("phase_1_start")
                time.sleep(0.10)
                t.bookmark("phase_1_end", metadata={"k": 1})

        bookmarks = t.result["bookmarks"]
        labels = [b["label"] for b in bookmarks]
        assert labels == ["phase_1_start", "phase_1_end"]
        assert bookmarks[1]["metadata"] == {"k": 1}

    def test_set_exit_recorded_in_report(self, tmp_path):
        proc = Process(pid=100, name="com.foo")
        adb = _mk_happy_adb()
        cfg = PerfConfig(package="com.foo", output_dir=tmp_path,
                         wait_timeout_sec=2.0,
                         cpu_interval_sec=0.05, mem_interval_sec=0.05,
                         rescan_interval_sec=10, status_interval_sec=10)
        with patch("perf_auto_test.discovery.discover", return_value=[proc]), \
             patch("perf_auto_test.pool.cpu_mod.sample", return_value=None), \
             patch("perf_auto_test.pool.mem_mod.sample", return_value=None):
            with PerfTest(cfg, adb=adb, discover_fn=lambda a, p: [proc]) as t:
                t.bookmark("x")
                t.set_exit(1, "fail_on_triggered")
                time.sleep(0.05)
        assert t.result["run"]["exit_code"] == 1
        assert t.result["run"]["exit_reason"] == "fail_on_triggered"

    def test_exception_in_with_block_recorded(self, tmp_path):
        proc = Process(pid=100, name="com.foo")
        adb = _mk_happy_adb()
        cfg = PerfConfig(package="com.foo", output_dir=tmp_path,
                         wait_timeout_sec=2.0,
                         cpu_interval_sec=0.05, mem_interval_sec=0.05,
                         rescan_interval_sec=10, status_interval_sec=10)
        with patch("perf_auto_test.discovery.discover", return_value=[proc]), \
             patch("perf_auto_test.pool.cpu_mod.sample", return_value=None), \
             patch("perf_auto_test.pool.mem_mod.sample", return_value=None):
            with pytest.raises(ValueError):
                with PerfTest(cfg, adb=adb, discover_fn=lambda a, p: [proc]) as t:
                    raise ValueError("user code blew up")
        assert t.result["run"]["exit_reason"] == "exception"
        assert t.result["run"]["exit_code"] >= 1


class TestPerfTestFailures:
    def test_package_not_installed_raises(self, tmp_path):
        adb = _mk_failing_adb()
        cfg = PerfConfig(package="com.no.such", output_dir=tmp_path,
                         wait_timeout_sec=1.0)
        with pytest.raises(DeviceSetupError, match="not installed"):
            with PerfTest(cfg, adb=adb) as t:
                pass  # pragma: no cover — body shouldn't run
        # Report still written so AI/CI can see what happened.
        assert (tmp_path / "report.json").exists()
        result = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert result["run"]["exit_code"] == 2
        assert result["run"]["exit_reason"] == "setup_failed"

    def test_wait_timeout_raises(self, tmp_path):
        adb = _mk_happy_adb()
        cfg = PerfConfig(package="com.foo", output_dir=tmp_path,
                         wait_timeout_sec=0.1)  # very short
        # No processes returned.
        with patch("perf_auto_test.discovery.discover", return_value=[]):
            with pytest.raises(TimeoutError):
                with PerfTest(cfg, adb=adb,
                              discover_fn=lambda a, p: []) as t:
                    pass  # pragma: no cover
        assert (tmp_path / "report.json").exists()
        result = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert result["run"]["exit_code"] == 3
        assert result["run"]["exit_reason"] == "wait_timeout"


class TestStatusJsonContents:
    def test_status_includes_processes_and_dump_counts(self, tmp_path):
        proc = Process(pid=100, name="com.foo")
        adb = _mk_happy_adb()
        cfg = PerfConfig(package="com.foo", output_dir=tmp_path,
                         wait_timeout_sec=2.0,
                         cpu_interval_sec=0.05, mem_interval_sec=0.05,
                         rescan_interval_sec=10, status_interval_sec=0.05)
        with patch("perf_auto_test.discovery.discover", return_value=[proc]), \
             patch("perf_auto_test.pool.cpu_mod.sample", return_value=None), \
             patch("perf_auto_test.pool.mem_mod.sample", return_value=None):
            with PerfTest(cfg, adb=adb, discover_fn=lambda a, p: [proc]) as t:
                time.sleep(0.15)
                # Capture status mid-run.
                mid = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
                assert mid["running"] is True
                assert any(p["pid"] == 100 for p in mid["processes"])
                assert "dump_counts" in mid

        final = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
        assert final["running"] is False
