"""Unit tests for CsvStreamWriter — rotation, schema header, thread-safety."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List

import pytest

from perf_auto_test.storage import (
    CPU_COLUMNS,
    CPU_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    MEM_COLUMNS,
    MEM_SCHEMA_TAG,
    CsvStreamWriter,
)


def _fake_clock(initial: datetime) -> Iterator[datetime]:
    """A mutable wrapper: returns a callable + mutable state."""
    state = {"t": initial}

    def clock() -> datetime:
        return state["t"]

    def advance(seconds: int) -> None:
        state["t"] = state["t"] + timedelta(seconds=seconds)

    return clock, advance


class TestCsvStreamWriter:
    def test_creates_output_dir(self, tmp_path: Path):
        out = tmp_path / "nested" / "run"
        CsvStreamWriter(out, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG)
        assert out.exists()

    def test_writes_schema_header_and_columns(self, tmp_path: Path):
        clock, _ = _fake_clock(datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc))
        w = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG, clock=clock)
        w.write_row({"timestamp": "t1", "process_name": "com.foo", "pid": 100, "cpu_pct": 12.3})
        w.close()
        path = tmp_path / "cpu_2026-05-15_10.csv"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[0] == f"# {CPU_SCHEMA_TAG}"
        assert lines[1] == ",".join(CPU_COLUMNS)
        assert lines[2] == "t1,com.foo,100,12.3"

    def test_rotates_on_hour_boundary(self, tmp_path: Path):
        clock, advance = _fake_clock(datetime(2026, 5, 15, 10, 59, 0, tzinfo=timezone.utc))
        w = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG, clock=clock)
        w.write_row({"timestamp": "t1", "process_name": "p", "pid": 1, "cpu_pct": 1.0})
        advance(120)  # 11:01 — different hour
        w.write_row({"timestamp": "t2", "process_name": "p", "pid": 1, "cpu_pct": 2.0})
        w.close()
        files = sorted(p.name for p in tmp_path.glob("cpu_*.csv"))
        assert files == ["cpu_2026-05-15_10.csv", "cpu_2026-05-15_11.csv"]
        # Each file has its own header.
        for f in files:
            text = (tmp_path / f).read_text(encoding="utf-8")
            assert text.startswith(f"# {CPU_SCHEMA_TAG}\n")

    def test_does_not_rotate_within_same_hour(self, tmp_path: Path):
        clock, advance = _fake_clock(datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc))
        w = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG, clock=clock)
        for i in range(5):
            w.write_row({"timestamp": f"t{i}", "process_name": "p", "pid": 1, "cpu_pct": float(i)})
            advance(60)  # +1 min
        w.close()
        files = sorted(tmp_path.glob("cpu_*.csv"))
        assert len(files) == 1

    def test_resume_appends_no_extra_header(self, tmp_path: Path):
        clock, _ = _fake_clock(datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc))
        w1 = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG, clock=clock)
        w1.write_row({"timestamp": "t1", "process_name": "p", "pid": 1, "cpu_pct": 1.0})
        w1.close()

        # New writer opens the same file (resuming a crashed run, say).
        w2 = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG, clock=clock)
        w2.write_row({"timestamp": "t2", "process_name": "p", "pid": 1, "cpu_pct": 2.0})
        w2.close()

        path = tmp_path / "cpu_2026-05-15_10.csv"
        text = path.read_text(encoding="utf-8")
        # Exactly one header block.
        assert text.count(f"# {CPU_SCHEMA_TAG}") == 1
        # But two data rows after header.
        assert text.count("\nt1,") == 1
        assert text.count("\nt2,") == 1

    def test_flush_every_n_rows(self, tmp_path: Path):
        """flush_every controls how often we hit the OS — values are still written
        through Python's csv into the file buffer regardless. Just verify no error."""
        clock, _ = _fake_clock(datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc))
        w = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG,
                            flush_every=3, clock=clock)
        for i in range(10):
            w.write_row({"timestamp": f"t{i}", "process_name": "p", "pid": 1, "cpu_pct": float(i)})
        w.close()
        text = (tmp_path / "cpu_2026-05-15_10.csv").read_text(encoding="utf-8")
        assert text.count("\n") >= 12  # header_comment + columns + 10 data rows

    def test_thread_safety(self, tmp_path: Path):
        """Concurrent writers must not interleave fields within a row."""
        clock, _ = _fake_clock(datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc))
        w = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG, clock=clock)

        def writer(start: int) -> None:
            for i in range(start, start + 100):
                w.write_row({
                    "timestamp": f"t{i}",
                    "process_name": f"p{i % 4}",
                    "pid": i,
                    "cpu_pct": float(i),
                })

        threads = [threading.Thread(target=writer, args=(i * 100,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        w.close()

        text = (tmp_path / "cpu_2026-05-15_10.csv").read_text(encoding="utf-8")
        lines = text.splitlines()
        # Comment + header + 400 rows = 402.
        assert len(lines) == 402
        # Every data row has exactly 4 commas (4 fields = 3 commas... wait, columns is 4 fields, so 3 commas).
        data = lines[2:]
        for line in data:
            assert line.count(",") == 3, f"corrupt row: {line!r}"

    def test_mem_schema(self, tmp_path: Path):
        clock, _ = _fake_clock(datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc))
        w = CsvStreamWriter(tmp_path, "mem", MEM_COLUMNS, MEM_SCHEMA_TAG, clock=clock)
        w.write_row({
            "timestamp": "t1", "process_name": "p", "pid": 1,
            "pss_mb": 83.59, "java_heap_mb": 8.92, "native_heap_mb": 12.05,
            "graphics_mb": 34.18, "code_mb": 23.67, "stack_mb": 0.13,
        })
        w.close()
        lines = (tmp_path / "mem_2026-05-15_10.csv").read_text(encoding="utf-8").splitlines()
        assert lines[0] == f"# {MEM_SCHEMA_TAG}"
        assert lines[1] == ",".join(MEM_COLUMNS)
        assert lines[2].startswith("t1,p,1,83.59,")

    def test_lifecycle_schema(self, tmp_path: Path):
        clock, _ = _fake_clock(datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc))
        w = CsvStreamWriter(tmp_path, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG, clock=clock)
        w.write_row({
            "timestamp": "t1", "process_name": "p", "event": "restart",
            "old_pid": 100, "new_pid": 200, "gap_sec": 1.5,
        })
        w.close()
        assert (tmp_path / "lifecycle_2026-05-15_10.csv").exists()

    def test_files_lists_all(self, tmp_path: Path):
        clock, advance = _fake_clock(datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc))
        w = CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG, clock=clock)
        w.write_row({"timestamp": "t1", "process_name": "p", "pid": 1, "cpu_pct": 1.0})
        advance(3700)
        w.write_row({"timestamp": "t2", "process_name": "p", "pid": 1, "cpu_pct": 2.0})
        files = w.files()
        w.close()
        assert len(files) == 2

    def test_context_manager_closes(self, tmp_path: Path):
        clock, _ = _fake_clock(datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc))
        with CsvStreamWriter(tmp_path, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG, clock=clock) as w:
            w.write_row({"timestamp": "t1", "process_name": "p", "pid": 1, "cpu_pct": 1.0})
        # No exception → close worked.
        assert (tmp_path / "cpu_2026-05-15_10.csv").exists()
