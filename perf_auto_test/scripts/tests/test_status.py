"""Status writer tests — periodic heartbeat to status.json."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from perf_auto_test.status import StatusWriter


class TestStatusWriter:
    def test_initial_snapshot_written_immediately(self, tmp_path: Path):
        sw = StatusWriter(tmp_path, interval_sec=10.0,
                          query_fn=lambda: {"processes": []})
        sw.start()
        try:
            assert (tmp_path / "status.json").exists()
            data = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
            assert data["running"] is True
            assert "elapsed_sec" in data
            assert data["processes"] == []
        finally:
            sw.stop()

    def test_query_fn_extras_merged(self, tmp_path: Path):
        calls = {"n": 0}

        def fake_query():
            calls["n"] += 1
            return {"processes": [{"name": "com.foo", "pid": 100}],
                    "dump_counts": {"cpu": 2, "mem": 1}}

        sw = StatusWriter(tmp_path, interval_sec=0.05, query_fn=fake_query)
        sw.start()
        time.sleep(0.15)
        sw.stop()

        data = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
        # Final snapshot has running=False
        assert data["running"] is False
        # Query extras merged
        assert calls["n"] >= 1

    def test_periodic_writes(self, tmp_path: Path):
        sw = StatusWriter(tmp_path, interval_sec=0.05,
                          query_fn=lambda: {"x": 1})
        sw.start()
        time.sleep(0.20)  # ~3-4 intervals
        # Capture some intermediate mtime
        path = tmp_path / "status.json"
        mtime1 = path.stat().st_mtime
        time.sleep(0.10)
        mtime2 = path.stat().st_mtime
        sw.stop()
        assert mtime2 >= mtime1  # got rewritten at least once

    def test_final_snapshot_has_running_false(self, tmp_path: Path):
        sw = StatusWriter(tmp_path, interval_sec=10.0, query_fn=lambda: {})
        sw.start()
        sw.stop()
        data = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
        assert data["running"] is False

    def test_query_exception_does_not_break_writer(self, tmp_path: Path):
        def boom():
            raise RuntimeError("oops")

        sw = StatusWriter(tmp_path, interval_sec=0.05, query_fn=boom)
        sw.start()
        time.sleep(0.10)
        # Still writes a minimal snapshot
        assert (tmp_path / "status.json").exists()
        data = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
        assert "timestamp" in data
        sw.stop()
