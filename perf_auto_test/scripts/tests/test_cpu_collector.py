"""Unit tests for the CPU collector (parsing + percent calculation)."""

from __future__ import annotations

import pathlib

import pytest

from perf_auto_test.collectors.cpu import (
    CpuPercentCalculator,
    CpuSample,
    parse_combined,
    parse_proc_pid_stat,
    parse_proc_stat,
)

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class TestParseProcPidStat:
    def test_basic(self):
        utime, stime = parse_proc_pid_stat(_read("proc_pid_stat.txt"))
        assert utime == 12345
        assert stime == 6789

    def test_remote_process_with_colon(self):
        utime, stime = parse_proc_pid_stat(_read("proc_pid_stat_remote.txt"))
        assert utime == 5000
        assert stime == 1234

    def test_comm_with_spaces_and_parens(self):
        """comm field contains `(`, `)`, and spaces — parser must find the LAST `)`."""
        utime, stime = parse_proc_pid_stat(_read("proc_pid_stat_weird_comm.txt"))
        assert utime == 50000
        assert stime == 25000

    def test_returns_none_for_empty(self):
        assert parse_proc_pid_stat("") is None

    def test_returns_none_for_garbage(self):
        assert parse_proc_pid_stat("not a stat line") is None

    def test_returns_none_when_too_short(self):
        assert parse_proc_pid_stat("1 (foo) S 0 0") is None


class TestParseProcStat:
    def test_aggregate_cpu_line(self):
        total = parse_proc_stat(_read("proc_stat.txt"))
        # 3526789 + 1234 + 1456789 + 67890123 + 4567 + 0 + 5678 + 0 + 0 + 0
        assert total == 3526789 + 1234 + 1456789 + 67890123 + 4567 + 5678

    def test_returns_none_if_no_cpu_line(self):
        assert parse_proc_stat("intr 1 2 3\nctxt 100\n") is None

    def test_picks_aggregate_not_cpu0(self):
        """Must use the `cpu ` (aggregate) line, not `cpu0`/`cpu1`/etc."""
        text = "cpu  100 0 0 0 0 0 0 0 0 0\ncpu0 50 0 0 0 0 0 0 0 0 0\n"
        assert parse_proc_stat(text) == 100


class TestParseCombined:
    def test_split_and_parse(self):
        combined = (
            _read("proc_pid_stat.txt").rstrip() + "\n---SEP---\n" +
            _read("proc_stat.txt")
        )
        result = parse_combined(combined)
        assert result is not None
        (utime, stime), total = result
        assert utime == 12345
        assert stime == 6789
        assert total > 0

    def test_returns_none_without_separator(self):
        assert parse_combined("just some text") is None

    def test_returns_none_if_pid_stat_invalid(self):
        combined = "garbage\n---SEP---\n" + _read("proc_stat.txt")
        assert parse_combined(combined) is None


class TestCpuPercentCalculator:
    def test_first_sample_returns_none(self):
        c = CpuPercentCalculator(cores=4)
        s = CpuSample(timestamp=0.0, pid=1, utime=100, stime=50, total_jiffies=1000)
        assert c.update(s) is None

    def test_second_sample_returns_percent(self):
        c = CpuPercentCalculator(cores=4)
        s1 = CpuSample(timestamp=0.0, pid=1, utime=100, stime=50, total_jiffies=1000)
        s2 = CpuSample(timestamp=1.0, pid=1, utime=150, stime=80, total_jiffies=1200)
        c.update(s1)
        pct = c.update(s2)
        # d_proc = 80, d_total = 200, cores = 4 → 80/200 * 4 * 100 = 160%
        assert pct == pytest.approx(160.0)

    def test_full_single_core_saturation(self):
        """A process using exactly one core fully should report ~100%."""
        c = CpuPercentCalculator(cores=4)
        # System: 4 cores, 1 second @ 100 jiffies/s/core = 400 jiffies per sec.
        # Process consumed 100 jiffies (one core full).
        s1 = CpuSample(timestamp=0.0, pid=1, utime=0, stime=0, total_jiffies=0)
        s2 = CpuSample(timestamp=1.0, pid=1, utime=100, stime=0, total_jiffies=400)
        c.update(s1)
        assert c.update(s2) == pytest.approx(100.0)

    def test_full_all_cores_saturation(self):
        """A process using all 4 cores should report ~400%."""
        c = CpuPercentCalculator(cores=4)
        s1 = CpuSample(timestamp=0.0, pid=1, utime=0, stime=0, total_jiffies=0)
        s2 = CpuSample(timestamp=1.0, pid=1, utime=400, stime=0, total_jiffies=400)
        c.update(s1)
        assert c.update(s2) == pytest.approx(400.0)

    def test_idle_process_zero_percent(self):
        c = CpuPercentCalculator(cores=4)
        s1 = CpuSample(timestamp=0.0, pid=1, utime=100, stime=50, total_jiffies=1000)
        s2 = CpuSample(timestamp=1.0, pid=1, utime=100, stime=50, total_jiffies=1400)
        c.update(s1)
        assert c.update(s2) == pytest.approx(0.0)

    def test_pid_change_resets(self):
        """If the pid changes (process restarted with same name), drop delta."""
        c = CpuPercentCalculator(cores=4)
        c.update(CpuSample(timestamp=0.0, pid=1, utime=100, stime=50, total_jiffies=1000))
        s2 = CpuSample(timestamp=1.0, pid=2, utime=10, stime=5, total_jiffies=1200)
        assert c.update(s2) is None

    def test_zero_total_delta_returns_none(self):
        c = CpuPercentCalculator(cores=4)
        c.update(CpuSample(timestamp=0.0, pid=1, utime=100, stime=50, total_jiffies=1000))
        s2 = CpuSample(timestamp=1.0, pid=1, utime=100, stime=50, total_jiffies=1000)
        assert c.update(s2) is None

    def test_negative_proc_delta_returns_none(self):
        """Negative deltas can't happen in steady state; guard against pid reuse."""
        c = CpuPercentCalculator(cores=4)
        c.update(CpuSample(timestamp=0.0, pid=1, utime=1000, stime=500, total_jiffies=10000))
        s2 = CpuSample(timestamp=1.0, pid=1, utime=900, stime=400, total_jiffies=10200)
        assert c.update(s2) is None

    def test_reset_drops_prev(self):
        c = CpuPercentCalculator(cores=4)
        c.update(CpuSample(timestamp=0.0, pid=1, utime=100, stime=50, total_jiffies=1000))
        c.reset()
        # Next sample is treated as the first one.
        s2 = CpuSample(timestamp=1.0, pid=1, utime=200, stime=100, total_jiffies=1500)
        assert c.update(s2) is None

    def test_cores_floor_at_one(self):
        c = CpuPercentCalculator(cores=0)
        assert c.cores == 1


class TestRealisticPair:
    """End-to-end: parse two real fixtures and compute the resulting CPU%."""

    def test_t1_to_t2_delta(self):
        combined_t1 = _read("proc_pid_stat.txt").rstrip() + "\n---SEP---\n" + _read("proc_stat.txt")
        combined_t2 = _read("proc_pid_stat_t2.txt").rstrip() + "\n---SEP---\n" + _read("proc_stat_t2.txt")

        (u1, s1), t1 = parse_combined(combined_t1)
        (u2, s2), t2 = parse_combined(combined_t2)

        # Manual sanity: utime delta = 12500-12345 = 155; stime delta = 6850-6789 = 61
        assert (u2 - u1) == 155
        assert (s2 - s1) == 61

        c = CpuPercentCalculator(cores=4)
        c.update(CpuSample(timestamp=0.0, pid=12345, utime=u1, stime=s1, total_jiffies=t1))
        pct = c.update(CpuSample(timestamp=1.0, pid=12345, utime=u2, stime=s2, total_jiffies=t2))
        assert pct is not None
        # Reasonable range — not asserting exact value since fixtures are synthetic.
        assert 0.0 < pct < 800.0
