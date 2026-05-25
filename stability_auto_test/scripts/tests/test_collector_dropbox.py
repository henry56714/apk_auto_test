from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sat.collectors.dropbox import DropboxFetcher, parse_dropbox_dump
from sat.detection import EVENT_ANR, EVENT_JAVA_CRASH, EVENT_NATIVE_CRASH

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_dropbox_dump_splits_entries():
    text = (FIXTURES / "dropbox_dump.txt").read_text(encoding="utf-8")
    entries = parse_dropbox_dump(text)
    tags = [e.tag for e in entries]
    assert tags == ["data_app_crash", "SYSTEM_TOMBSTONE", "data_app_anr"]


def _make_adb(stdout: str, returncode: int = 0):
    adb = MagicMock()
    adb.shell.return_value = MagicMock(returncode=returncode, stdout=stdout)
    return adb


# ── DropboxFetcher.fetch ──────────────────────────────────────────────────────

def test_fetch_returns_body_for_matching_java_crash():
    text = """\
Drop box contents: 1 entries
========================================
2026-05-24 12:42:44 data_app_crash (text, 500 bytes)
Process: com.example.app
PID: 5678
java.lang.NullPointerException: Attempt to invoke on null
\tat com.example.app.Foo.bar(Foo.java:42)
========================================
"""
    fetcher = DropboxFetcher(_make_adb(text))
    body = fetcher.fetch(EVENT_JAVA_CRASH, "com.example.app", "05-24 12:42:44.000")
    assert body is not None
    assert any("NullPointerException" in line for line in body)


def test_fetch_returns_none_for_wrong_package():
    text = """\
Drop box contents: 1 entries
========================================
2026-05-24 12:00:00 data_app_crash (text, 100 bytes)
Process: com.other.app
PID: 999
java.lang.RuntimeException: nope
========================================
"""
    fetcher = DropboxFetcher(_make_adb(text))
    body = fetcher.fetch(EVENT_JAVA_CRASH, "com.example.app", "05-24 12:00:00.000")
    assert body is None


def test_fetch_returns_none_when_outside_time_window():
    text = """\
Drop box contents: 1 entries
========================================
2026-05-24 10:00:00 data_app_crash (text, 100 bytes)
Process: com.example.app
PID: 1234
java.lang.RuntimeException: old crash
========================================
"""
    # event at 12:00:00, dropbox entry at 10:00:00 → 7200 s apart > 60 s window
    fetcher = DropboxFetcher(_make_adb(text))
    body = fetcher.fetch(EVENT_JAVA_CRASH, "com.example.app",
                         "05-24 12:00:00.000", window_sec=60.0)
    assert body is None


def test_fetch_picks_closest_entry_when_multiple_match():
    text = """\
Drop box contents: 2 entries
========================================
2026-05-24 12:42:30 data_app_crash (text, 100 bytes)
Process: com.example.app
PID: 1111
java.lang.RuntimeException: earlier
========================================
2026-05-24 12:42:44 data_app_crash (text, 100 bytes)
Process: com.example.app
PID: 2222
java.lang.NullPointerException: closer
========================================
"""
    # event at 12:42:45 → closer entry is the one at 12:42:44 (delta=1s)
    fetcher = DropboxFetcher(_make_adb(text))
    body = fetcher.fetch(EVENT_JAVA_CRASH, "com.example.app", "05-24 12:42:45.000")
    assert body is not None
    assert any("NullPointerException" in line for line in body)


def test_fetch_returns_none_on_adb_error():
    from sat.adb import AdbError
    adb = MagicMock()
    adb.shell.side_effect = AdbError("device offline")
    fetcher = DropboxFetcher(adb)
    assert fetcher.fetch(EVENT_JAVA_CRASH, "com.example.app") is None


def test_fetch_returns_none_on_nonzero_returncode():
    fetcher = DropboxFetcher(_make_adb("", returncode=1))
    assert fetcher.fetch(EVENT_JAVA_CRASH, "com.example.app") is None


def test_fetch_native_crash_matches_tombstone_tag():
    text = """\
Drop box contents: 1 entries
========================================
2026-05-24 12:00:00 SYSTEM_TOMBSTONE (compressed text, 2048 bytes)
pid: 9999, tid: 10000, name: Thread-1  >>> com.example.app <<<
signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0xdead
========================================
"""
    fetcher = DropboxFetcher(_make_adb(text))
    body = fetcher.fetch(EVENT_NATIVE_CRASH, "com.example.app", "05-24 12:00:00.000")
    assert body is not None
    assert any("SIGSEGV" in line for line in body)


def test_fetch_matches_subprocess_process_name():
    """com.example.app:service matches package com.example.app."""
    text = """\
Drop box contents: 1 entries
========================================
2026-05-24 12:00:00 data_app_crash (text, 100 bytes)
Process: com.example.app:service
PID: 1234
java.lang.RuntimeException: sub-process crash
========================================
"""
    fetcher = DropboxFetcher(_make_adb(text))
    body = fetcher.fetch(EVENT_JAVA_CRASH, "com.example.app:service", "05-24 12:00:00.000")
    assert body is not None


def test_fetch_without_device_ts_returns_first_match():
    text = """\
Drop box contents: 1 entries
========================================
2026-05-24 12:00:00 data_app_crash (text, 100 bytes)
Process: com.example.app
PID: 1234
java.lang.RuntimeException: any crash
========================================
"""
    fetcher = DropboxFetcher(_make_adb(text))
    body = fetcher.fetch(EVENT_JAVA_CRASH, "com.example.app", device_ts=None)
    assert body is not None
