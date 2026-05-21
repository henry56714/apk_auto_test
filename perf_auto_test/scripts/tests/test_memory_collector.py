"""Unit tests for the memory collector (dumpsys meminfo parsing)."""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest

from pat.adb import AdbResult, AdbTimeout
from pat.collectors.memory import MemSample, parse_meminfo, sample

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class TestParseMeminfoAndroid12:
    @pytest.fixture
    def sample(self):
        return parse_meminfo(_read("meminfo_android12.txt"), pid=12345)

    def test_returns_sample(self, sample: MemSample):
        assert sample is not None

    def test_pid_passthrough(self, sample: MemSample):
        assert sample.pid == 12345

    def test_total_pss(self, sample: MemSample):
        # TOTAL PSS: 85594 KB → 83.59 MB
        assert sample.total_pss_mb == pytest.approx(85594 / 1024.0)

    def test_java_heap(self, sample: MemSample):
        assert sample.java_heap_pss_mb == pytest.approx(9134 / 1024.0)

    def test_native_heap(self, sample: MemSample):
        assert sample.native_heap_pss_mb == pytest.approx(12340 / 1024.0)

    def test_code(self, sample: MemSample):
        assert sample.code_pss_mb == pytest.approx(24234 / 1024.0)

    def test_stack(self, sample: MemSample):
        assert sample.stack_pss_mb == pytest.approx(128 / 1024.0)

    def test_graphics(self, sample: MemSample):
        assert sample.graphics_pss_mb == pytest.approx(35000 / 1024.0)


class TestParseMeminfoAndroid10:
    def test_total_pss(self):
        s = parse_meminfo(_read("meminfo_android10.txt"))
        assert s is not None
        assert s.total_pss_mb == pytest.approx(178256 / 1024.0)

    def test_breakdown(self):
        s = parse_meminfo(_read("meminfo_android10.txt"))
        assert s.java_heap_pss_mb == pytest.approx(17800 / 1024.0)
        assert s.native_heap_pss_mb == pytest.approx(44000 / 1024.0)
        assert s.code_pss_mb == pytest.approx(26700 / 1024.0)
        assert s.graphics_pss_mb == pytest.approx(65000 / 1024.0)


class TestParseMeminfoAndroid8:
    def test_total_pss(self):
        s = parse_meminfo(_read("meminfo_android8.txt"))
        assert s is not None
        assert s.total_pss_mb == pytest.approx(34750 / 1024.0)

    def test_breakdown(self):
        s = parse_meminfo(_read("meminfo_android8.txt"))
        assert s.java_heap_pss_mb == pytest.approx(4500 / 1024.0)
        assert s.native_heap_pss_mb == pytest.approx(8000 / 1024.0)
        assert s.code_pss_mb == pytest.approx(13100 / 1024.0)


class TestParseMeminfoNoSummary:
    """Older Android may lack `App Summary`; we should still produce TOTAL PSS
    from the table's `TOTAL` row, with breakdown zeros."""

    def test_total_pss_from_table(self):
        s = parse_meminfo(_read("meminfo_no_summary.txt"))
        assert s is not None
        assert s.total_pss_mb == pytest.approx(8550 / 1024.0)

    def test_breakdown_zero(self):
        s = parse_meminfo(_read("meminfo_no_summary.txt"))
        assert s.java_heap_pss_mb == 0.0
        assert s.native_heap_pss_mb == 0.0
        assert s.graphics_pss_mb == 0.0


class TestParseMeminfoEdges:
    def test_empty_input(self):
        assert parse_meminfo("") is None

    def test_garbage_returns_none(self):
        assert parse_meminfo("not a meminfo dump at all\n") is None

    def test_pid_default_zero(self):
        s = parse_meminfo(_read("meminfo_android12.txt"))
        assert s.pid == 0


class TestSampleAdbInvocation:
    """`sample()` must pass a long timeout and disable retries by default,
    because `dumpsys meminfo` reads /proc/<pid>/smaps which can be slow for
    large processes under memory pressure — and retrying won't help."""

    def test_default_timeout_is_30s(self):
        adb = MagicMock()
        adb.shell.return_value = AdbResult(
            returncode=0, stdout=_read("meminfo_android12.txt"),
            stderr="", duration_sec=0.1,
        )
        sample(adb, 1234)
        _, kwargs = adb.shell.call_args
        assert kwargs["timeout"] == 30.0
        assert kwargs["retries"] == 0

    def test_timeout_returns_none(self):
        adb = MagicMock()
        adb.shell.side_effect = AdbTimeout("timed out")
        assert sample(adb, 1234) is None

    def test_custom_timeout_passed_through(self):
        adb = MagicMock()
        adb.shell.return_value = AdbResult(
            returncode=0, stdout=_read("meminfo_android12.txt"),
            stderr="", duration_sec=0.1,
        )
        sample(adb, 1234, timeout=5.0, retries=2)
        _, kwargs = adb.shell.call_args
        assert kwargs["timeout"] == 5.0
        assert kwargs["retries"] == 2
