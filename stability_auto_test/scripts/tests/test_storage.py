from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sat.storage import (
    EVENTS_COLUMNS,
    EVENTS_SCHEMA_TAG,
    CsvStreamWriter,
    LogStreamWriter,
)


def _clock_seq(hours):
    it = iter(hours)
    return lambda: next(it)


def test_csv_writer_emits_schema_tag_and_header(tmp_path: Path):
    w = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG)
    w.write_row({
        "timestamp": "2026-05-21 10:00:00.000",
        "event_type": "java_crash",
        "process_name": "com.example.app",
        "pid": 123,
        "severity": "fatal",
        "summary": "x",
    })
    w.close()
    files = list(tmp_path.glob("events_*.csv"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == f"# {EVENTS_SCHEMA_TAG}"
    assert "timestamp" in lines[1]
    assert "java_crash" in text


def test_csv_writer_rotates_on_hour_boundary(tmp_path: Path):
    hours = [
        datetime(2026, 5, 21, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 21, 11, 0, 0, tzinfo=timezone.utc),
    ]
    clock = _clock_seq(hours)
    w = CsvStreamWriter(tmp_path, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG,
                        clock=clock)
    w.write_row({
        "timestamp": "x", "event_type": "anr", "process_name": "p",
        "pid": 1, "severity": "error", "summary": "",
    })
    w.write_row({
        "timestamp": "y", "event_type": "anr", "process_name": "p",
        "pid": 1, "severity": "error", "summary": "",
    })
    w.close()
    files = sorted(tmp_path.glob("events_*.csv"))
    assert len(files) == 2


def test_log_writer_writes_lines(tmp_path: Path):
    w = LogStreamWriter(tmp_path)
    w.write_line("first")
    w.write_line("second\n")
    w.close()
    files = list(tmp_path.glob("logcat_*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert text.startswith("# stability_auto_test/logcat/v1\n")
    assert "first\nsecond\n" in text


def test_log_writer_thread_safe(tmp_path: Path):
    w = LogStreamWriter(tmp_path, flush_every=1)
    barrier = threading.Barrier(4)

    def worker(i: int):
        barrier.wait()
        for j in range(50):
            w.write_line(f"t{i}-{j}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    w.close()
    files = list(tmp_path.glob("logcat_*.log"))
    text = files[0].read_text(encoding="utf-8")
    # 200 data lines + 1 schema-tag line.
    assert len(text.splitlines()) == 201
