"""Unit tests for CollectorPool — workers + dynamic discovery."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, List
from unittest.mock import patch

import pytest

from pat.alerting import ThresholdConfig
from pat.collectors.cpu import CpuSample
from pat.collectors.memory import MemSample
from pat.discovery import Process
from pat.pool import (
    CollectorPool,
    DumpsConfig,
    ThresholdsBundle,
    _normalize_filter,
)
from pat.storage import (
    CPU_COLUMNS,
    CPU_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    MEM_COLUMNS,
    MEM_SCHEMA_TAG,
    CsvStreamWriter,
)


class _FakeAdb:
    pass


def _read_data_rows(path: Path) -> List[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln for ln in lines[2:] if ln.strip()]


def _writers(tmp_path: Path):
    cpu = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG)
    mem = CsvStreamWriter(tmp_path, "mem", MEM_COLUMNS, MEM_SCHEMA_TAG)
    life = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG)
    return cpu, mem, life


def _static_discover(processes: List[Process]) -> Callable:
    """Return a discover_fn that always returns the same list."""
    def _fn(adb, package):
        return list(processes)
    return _fn


def _mutable_discover(state: dict) -> Callable:
    """state['live'] is a list[Process]; the fn returns the current value."""
    def _fn(adb, package):
        return list(state["live"])
    return _fn


# -----------------------------------------------------------------------------
# Filter normalization
# -----------------------------------------------------------------------------

class TestNormalizeFilter:
    def test_none_means_all(self):
        assert _normalize_filter(None, "com.foo") is None
        assert _normalize_filter([], "com.foo") is None

    def test_main_keyword(self):
        f = _normalize_filter(["main"], "com.foo")
        assert f == {"com.foo"}

    def test_empty_string_treated_as_main(self):
        f = _normalize_filter([""], "com.foo")
        assert f == {"com.foo"}

    def test_colon_suffix(self):
        f = _normalize_filter([":remote", ":push"], "com.foo")
        assert f == {"com.foo:remote", "com.foo:push"}

    def test_exact_name(self):
        f = _normalize_filter(["com.bar"], "com.foo")
        assert f == {"com.bar"}

    def test_mixed(self):
        f = _normalize_filter(["main", ":remote", "com.other"], "com.foo")
        assert f == {"com.foo", "com.foo:remote", "com.other"}


# -----------------------------------------------------------------------------
# Static-process scenarios
# -----------------------------------------------------------------------------

class TestPoolStatic:
    def test_initial_processes_get_new_lifecycle(self, tmp_path: Path):
        proc = Process(pid=100, name="com.foo")
        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=10, mem_interval_sec=10,
            rescan_interval_sec=10,
            discover_fn=_static_discover([proc]),
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=[proc])
            time.sleep(0.05)
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()

        rows = _read_data_rows(life.files()[0])
        assert any(",new,0,100," in r and "com.foo" in r for r in rows), rows

    def test_cpu_loop_writes_after_two_samples(self, tmp_path: Path):
        proc = Process(pid=1234, name="com.foo")
        seq = [
            CpuSample(timestamp=0.0, pid=1234, utime=100, stime=50, total_jiffies=1000),
            CpuSample(timestamp=1.0, pid=1234, utime=200, stime=100, total_jiffies=1200),
            CpuSample(timestamp=2.0, pid=1234, utime=300, stime=150, total_jiffies=1400),
        ]
        idx = {"i": 0}

        def fake_sample(adb, pid):
            i = idx["i"]
            idx["i"] += 1
            if i < len(seq):
                return seq[i]
            return None

        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=0.02, mem_interval_sec=10,
            rescan_interval_sec=10,
            discover_fn=_static_discover([proc]),
        )
        with patch("pat.pool.cpu_mod.sample", side_effect=fake_sample), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=[proc])
            time.sleep(0.15)
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()

        rows = _read_data_rows(cpu.files()[0])
        assert len(rows) >= 1
        for r in rows:
            fields = r.split(",")
            assert fields[1] == "com.foo"
            assert fields[2] == "1234"

    def test_mem_loop_writes_each_sample(self, tmp_path: Path):
        proc = Process(pid=9999, name="com.bar")
        mem_sample = MemSample(
            timestamp=0.0, pid=9999, total_pss_mb=85.59,
            java_heap_pss_mb=8.92, native_heap_pss_mb=12.05,
            code_pss_mb=23.67, stack_pss_mb=0.13, graphics_pss_mb=34.18,
        )
        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.bar",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=10, mem_interval_sec=0.02,
            rescan_interval_sec=10,
            discover_fn=_static_discover([proc]),
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=mem_sample):
            pool.start(initial_processes=[proc])
            time.sleep(0.10)
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()

        rows = _read_data_rows(mem.files()[0])
        assert len(rows) >= 2
        for r in rows:
            fields = r.split(",")
            assert fields[1] == "com.bar"
            assert float(fields[3]) == pytest.approx(85.59)

    def test_stop_unblocks_quickly(self, tmp_path: Path):
        proc = Process(pid=1, name="com.slow")
        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.slow",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=30, mem_interval_sec=30, rescan_interval_sec=30,
            discover_fn=_static_discover([proc]),
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=[proc])
            time.sleep(0.05)
            t0 = time.monotonic()
            pool.stop(join_timeout=5.0)
            elapsed = time.monotonic() - t0
        for w in (cpu, mem, life):
            w.close()
        assert elapsed < 1.0, f"stop() took {elapsed:.2f}s; should be near-instant"

    def test_sample_failures_counted_when_sample_returns_none(self, tmp_path: Path):
        """Each None from mem_mod.sample / cpu_mod.sample should bump
        pool.sample_failures(); the report later surfaces this so a silent
        adb-timeout outage is visible instead of looking like 'no data'."""
        proc = Process(pid=42, name="com.foo")
        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=0.02, mem_interval_sec=0.02,
            rescan_interval_sec=10,
            discover_fn=_static_discover([proc]),
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=[proc])
            time.sleep(0.15)
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()

        fails = pool.sample_failures()
        assert "com.foo" in fails
        assert fails["com.foo"]["cpu"] >= 2
        assert fails["com.foo"]["mem"] >= 2

    def test_sample_failures_zero_when_sampling_succeeds(self, tmp_path: Path):
        proc = Process(pid=42, name="com.foo")
        mem_sample = MemSample(
            timestamp=0.0, pid=42, total_pss_mb=100.0,
            java_heap_pss_mb=10, native_heap_pss_mb=20,
            code_pss_mb=30, stack_pss_mb=1, graphics_pss_mb=10,
        )
        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=10, mem_interval_sec=0.02,
            rescan_interval_sec=10,
            discover_fn=_static_discover([proc]),
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=mem_sample):
            pool.start(initial_processes=[proc])
            time.sleep(0.10)
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()

        fails = pool.sample_failures()
        # CPU sample returns None (sample failures) but mem should be 0.
        assert fails.get("com.foo", {}).get("mem", 0) == 0

    def test_collector_exception_does_not_kill_loop(self, tmp_path: Path):
        proc = Process(pid=1, name="com.flaky")
        cpu, mem, life = _writers(tmp_path)
        calls = {"n": 0}
        s1 = CpuSample(timestamp=0.0, pid=1, utime=100, stime=50, total_jiffies=1000)
        s2 = CpuSample(timestamp=1.0, pid=1, utime=200, stime=100, total_jiffies=1200)

        def fake_sample(adb, pid):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("transient")
            if calls["n"] in (1, 3):
                return s1 if calls["n"] == 1 else s2
            return None

        pool = CollectorPool(
            _FakeAdb(), "com.flaky",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=0.02, mem_interval_sec=10, rescan_interval_sec=10,
            discover_fn=_static_discover([proc]),
        )
        with patch("pat.pool.cpu_mod.sample", side_effect=fake_sample), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=[proc])
            time.sleep(0.20)
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()
        assert calls["n"] >= 3


# -----------------------------------------------------------------------------
# Dynamic-discovery scenarios
# -----------------------------------------------------------------------------

class TestPoolDynamic:
    def test_new_process_picked_up_by_watcher(self, tmp_path: Path):
        state = {"live": []}  # nothing initially
        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=10, mem_interval_sec=10,
            rescan_interval_sec=0.03,
            discover_fn=_mutable_discover(state),
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start()
            # Add a process after the watcher is running.
            state["live"] = [Process(pid=42, name="com.foo")]
            time.sleep(0.20)
            assert any(p.name == "com.foo" for p in pool.current_processes())
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()

        rows = _read_data_rows(life.files()[0])
        assert any(",new,0,42," in r and "com.foo" in r for r in rows), rows

    def test_process_disappearance_emits_gone(self, tmp_path: Path):
        p = Process(pid=42, name="com.foo")
        state = {"live": [p]}
        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=10, mem_interval_sec=10,
            rescan_interval_sec=0.03,
            discover_fn=_mutable_discover(state),
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=[p])
            time.sleep(0.10)
            state["live"] = []  # process gone
            time.sleep(0.20)
            assert pool.current_processes() == []
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()

        rows = _read_data_rows(life.files()[0])
        assert any(",gone," in r and "com.foo" in r for r in rows), rows

    def test_pid_change_emits_restart_with_old_pid(self, tmp_path: Path):
        state = {"live": [Process(pid=100, name="com.foo")]}
        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=10, mem_interval_sec=10,
            rescan_interval_sec=0.03,
            discover_fn=_mutable_discover(state),
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=state["live"])
            time.sleep(0.10)
            state["live"] = [Process(pid=200, name="com.foo")]
            time.sleep(0.20)
            assert pool.current_processes()[0].pid == 200
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()

        rows = _read_data_rows(life.files()[0])
        restart = [r for r in rows if ",restart," in r]
        assert restart, rows
        # restart row: ts, name, "restart", old_pid=100, new_pid=200, gap_sec
        fields = restart[0].split(",")
        assert fields[1] == "com.foo"
        assert fields[2] == "restart"
        assert fields[3] == "100"
        assert fields[4] == "200"

    def test_process_filter_excludes(self, tmp_path: Path):
        main_p = Process(pid=100, name="com.foo")
        remote_p = Process(pid=101, name="com.foo:remote")
        push_p = Process(pid=102, name="com.foo:push")
        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=10, mem_interval_sec=10,
            rescan_interval_sec=0.03,
            process_filter=[":remote"],  # only :remote should be monitored
            discover_fn=_static_discover([main_p, remote_p, push_p]),
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=[main_p, remote_p, push_p])
            time.sleep(0.10)
            current = {p.name for p in pool.current_processes()}
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()
        assert current == {"com.foo:remote"}

    def test_restart_gap_sec_populated(self, tmp_path: Path):
        """When a process disappears then comes back, gap_sec should reflect
        the time between gone-detection and new-detection."""
        state = {"live": [Process(pid=100, name="com.foo")]}
        cpu, mem, life = _writers(tmp_path)
        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=10, mem_interval_sec=10,
            rescan_interval_sec=0.03,
            discover_fn=_mutable_discover(state),
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=state["live"])
            time.sleep(0.10)
            state["live"] = []
            time.sleep(0.15)
            state["live"] = [Process(pid=200, name="com.foo")]
            time.sleep(0.15)
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()

        rows = _read_data_rows(life.files()[0])
        # Should see: new(100), gone, restart(200) — restart with positive gap
        restarts = [r for r in rows if ",restart," in r]
        assert restarts, rows
        gap_sec = float(restarts[-1].split(",")[5])
        assert gap_sec > 0.0


# -----------------------------------------------------------------------------
# Alerting + dump integration
# -----------------------------------------------------------------------------

class TestPoolAlerting:
    def _aggressive_thresholds(self) -> ThresholdsBundle:
        # Trip on any value > 0 for any sustain of 0s, no cooldown.
        return ThresholdsBundle(
            cpu=ThresholdConfig("cpu_pct", 0.0, 0.0, 1000.0),
            mem=ThresholdConfig("mem_pss_mb", 0.0, 0.0, 1000.0),
        )

    def test_cpu_breach_invokes_dumper(self, tmp_path: Path):
        proc = Process(pid=1234, name="com.foo")
        # Two samples so CpuPercentCalculator yields a non-None percent.
        seq = [
            CpuSample(timestamp=0.0, pid=1234, utime=0, stime=0, total_jiffies=0),
            CpuSample(timestamp=1.0, pid=1234, utime=100, stime=0, total_jiffies=400),
        ]
        idx = {"i": 0}

        def fake_cpu_sample(adb, pid):
            i = idx["i"]
            idx["i"] = i + 1
            return seq[i] if i < len(seq) else None

        cpu, mem, life = _writers(tmp_path)
        incidents_dir = tmp_path / "incidents"

        cpu_calls = []
        heap_calls = []

        def fake_cpu_dump(adb, process, alert, idir):
            cpu_calls.append((process.pid, alert.value_at_trigger))
            (Path(idir) / f"cpu_dump_pid{process.pid}.json").write_text("{}")

        def fake_heap_dump(adb, process, alert, idir, **kw):
            heap_calls.append((process.pid, alert.value_at_trigger))

        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=0.02, mem_interval_sec=10,
            rescan_interval_sec=10,
            discover_fn=_static_discover([proc]),
            thresholds=self._aggressive_thresholds(),
            incidents_dir=incidents_dir,
            cpu_dump_fn=fake_cpu_dump,
            heap_dump_fn=fake_heap_dump,
        )
        with patch("pat.pool.cpu_mod.sample", side_effect=fake_cpu_sample), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=[proc])
            time.sleep(0.25)
            pool.stop(join_timeout=3.0)
        for w in (cpu, mem, life):
            w.close()

        assert cpu_calls, "expected CPU dump to be called"
        assert cpu_calls[0][0] == 1234
        assert heap_calls == []

    def test_mem_breach_invokes_heap_dumper(self, tmp_path: Path):
        proc = Process(pid=9999, name="com.bar")
        mem_sample = MemSample(
            timestamp=0.0, pid=9999, total_pss_mb=600.0,
            java_heap_pss_mb=50, native_heap_pss_mb=80,
            code_pss_mb=200, stack_pss_mb=1, graphics_pss_mb=200,
        )
        cpu, mem, life = _writers(tmp_path)
        incidents_dir = tmp_path / "incidents"

        cpu_calls, heap_calls = [], []

        def fake_cpu_dump(adb, process, alert, idir):
            cpu_calls.append(process.pid)

        def fake_heap_dump(adb, process, alert, idir, **kw):
            heap_calls.append((process.pid, alert.value_at_trigger, alert.metric))

        pool = CollectorPool(
            _FakeAdb(), "com.bar",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=10, mem_interval_sec=0.02,
            rescan_interval_sec=10,
            discover_fn=_static_discover([proc]),
            thresholds=self._aggressive_thresholds(),
            incidents_dir=incidents_dir,
            cpu_dump_fn=fake_cpu_dump,
            heap_dump_fn=fake_heap_dump,
        )
        with patch("pat.pool.cpu_mod.sample", return_value=None), \
             patch("pat.pool.mem_mod.sample", return_value=mem_sample):
            pool.start(initial_processes=[proc])
            time.sleep(0.20)
            pool.stop(join_timeout=3.0)
        for w in (cpu, mem, life):
            w.close()

        assert heap_calls, "expected heap dump to be called"
        assert heap_calls[0][0] == 9999
        assert heap_calls[0][2] == "mem_pss_mb"

    def test_max_dumps_respected(self, tmp_path: Path):
        """Cooldown is 0s in the aggressive config, so the tracker would fire
        on every sample; the pool's per-kind max_*_dumps cap must stop us."""
        proc = Process(pid=1234, name="com.foo")
        # Pre-fill calculator state so every subsequent sample yields a value.
        seq_factory = {"n": 0}

        def fake_cpu_sample(adb, pid):
            n = seq_factory["n"]
            seq_factory["n"] = n + 1
            return CpuSample(timestamp=float(n), pid=pid,
                             utime=100 * n, stime=0,
                             total_jiffies=400 * max(1, n))

        cpu, mem, life = _writers(tmp_path)
        cpu_calls = []

        def fake_cpu_dump(adb, process, alert, idir):
            cpu_calls.append(alert.triggered_at)

        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=0.01, mem_interval_sec=10,
            rescan_interval_sec=10,
            discover_fn=_static_discover([proc]),
            thresholds=ThresholdsBundle(
                cpu=ThresholdConfig("cpu_pct", 0.0, 0.0, 0.0),
                mem=ThresholdConfig("mem_pss_mb", 1e9, 0.0, 0.0),
            ),
            dumps=DumpsConfig(enable_heap=True, max_cpu_dumps=3, max_heap_dumps=3, max_concurrent=4),
            incidents_dir=tmp_path / "incidents",
            cpu_dump_fn=fake_cpu_dump,
            heap_dump_fn=lambda *a, **k: None,
        )
        with patch("pat.pool.cpu_mod.sample", side_effect=fake_cpu_sample), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=[proc])
            time.sleep(0.30)
            pool.stop(join_timeout=3.0)
        for w in (cpu, mem, life):
            w.close()

        assert len(cpu_calls) <= 3
        assert pool.dump_counts()["cpu"] <= 3

    def test_no_incidents_dir_no_dump(self, tmp_path: Path):
        """If incidents_dir is None, alerts shouldn't crash; just no dump."""
        proc = Process(pid=1234, name="com.foo")
        seq = [
            CpuSample(timestamp=0.0, pid=1234, utime=0, stime=0, total_jiffies=0),
            CpuSample(timestamp=1.0, pid=1234, utime=100, stime=0, total_jiffies=400),
        ]
        idx = {"i": 0}

        def fake_sample(adb, pid):
            i = idx["i"]
            idx["i"] = i + 1
            return seq[i] if i < len(seq) else None

        cpu, mem, life = _writers(tmp_path)
        cpu_calls = []

        def fake_cpu_dump(adb, process, alert, idir):
            cpu_calls.append(1)

        pool = CollectorPool(
            _FakeAdb(), "com.foo",
            cpu_cores=4, cpu_writer=cpu, mem_writer=mem, lifecycle_writer=life,
            cpu_interval_sec=0.02, mem_interval_sec=10,
            rescan_interval_sec=10,
            discover_fn=_static_discover([proc]),
            thresholds=self._aggressive_thresholds(),
            incidents_dir=None,
            cpu_dump_fn=fake_cpu_dump,
            heap_dump_fn=lambda *a, **k: None,
        )
        with patch("pat.pool.cpu_mod.sample", side_effect=fake_sample), \
             patch("pat.pool.mem_mod.sample", return_value=None):
            pool.start(initial_processes=[proc])
            time.sleep(0.20)
            pool.stop(join_timeout=2.0)
        for w in (cpu, mem, life):
            w.close()
        assert cpu_calls == []  # no dump attempted
