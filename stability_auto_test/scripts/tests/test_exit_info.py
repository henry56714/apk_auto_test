from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from sat.collectors.exit_info import (
    REASON_ANR,
    REASON_CRASHED,
    REASON_LOW_MEMORY,
    REASON_USER_REQUESTED,
    parse_exit_info_text,
    query_exit_info,
)

_FIXTURE = """Historical process exit information:
Package: com.example.app (u0a123)
  Process: com.example.app (pid 1234)
  Timestamp: 2026-05-21T10:00:00.123
  Reason: CRASHED
  Subreason: java.lang.RuntimeException
  Status: 5
  Importance: 100
  PSS: 12345 kB
  RSS: 23456 kB
  Description: java.lang.RuntimeException: boom
"""


def test_parse_exit_info_normalizes_crash():
    records = parse_exit_info_text(_FIXTURE)
    assert len(records) == 1
    rec = records[0]
    assert rec.pid == 1234
    assert rec.process == "com.example.app"
    assert rec.exit_reason == REASON_CRASHED
    assert rec.is_stability_failure is True
    assert rec.category == "crash"
    assert rec.pss_kb == 12345
    assert rec.rss_kb == 23456
    assert rec.source == "exit_info"
    assert rec.confidence == "high"


def test_normalized_mapping_for_common_reasons():
    cases = [
        ("Reason: ANR", REASON_ANR, True),
        ("Reason: LOW_MEMORY", REASON_LOW_MEMORY, True),
        ("Reason: USER_REQUESTED", REASON_USER_REQUESTED, False),
    ]
    for reason_line, expected_reason, failure in cases:
        rec = parse_exit_info_text(
            "Package: com.example.app\n"
            "  Process: com.example.app (pid 1)\n"
            "  Timestamp: 2026-05-21T10:00:00.000\n"
            f"  {reason_line}\n"
        )[0]
        assert rec.exit_reason == expected_reason
        assert rec.is_stability_failure is failure


def test_cached_recycle_is_not_stability_failure():
    rec = parse_exit_info_text(
        "Package: com.example.app\n"
        "  Process: com.example.app (pid 1)\n"
        "  Timestamp: 2026-05-21T10:00:00.000\n"
        "  Reason: OTHER\n"
        "  Status: cached\n"
        "  Importance: 16\n"
    )[0]
    assert rec.exit_reason == "normal_recycle"
    assert rec.is_stability_failure is False
    assert rec.expected is True
    assert rec.category == "process_exit"


def test_query_exit_info_filters_package_and_watermark():
    adb = MagicMock()
    adb.shell.return_value = MagicMock(
        returncode=0,
        stdout=(
            "Historical process exit information:\n"
            "Package: com.example.app (u0a123)\n"
            "  Process: com.example.app (pid 1234)\n"
            "  Timestamp: 2026-05-21T10:00:00.000\n"
            "  Reason: ANR\n"
            "Package: com.example.app (u0a123)\n"
            "  Process: com.example.app (pid 1235)\n"
            "  Timestamp: 2026-05-21T10:05:00.000\n"
            "  Reason: USER_REQUESTED\n"
            "Package: com.other.app (u0a9)\n"
            "  Process: com.other.app (pid 999)\n"
            "  Timestamp: 2026-05-21T10:06:00.000\n"
            "  Reason: CRASHED\n"
        ),
    )
    records = query_exit_info(
        adb, "com.example.app",
        since_epoch=datetime(2026, 5, 21, 10, 3, tzinfo=timezone.utc).timestamp(),
    )
    assert len(records) == 1
    assert records[0].pid == 1235
    assert records[0].exit_reason == REASON_USER_REQUESTED


def test_exit_info_unavailable_returns_empty():
    adb = MagicMock()
    adb.shell.return_value = MagicMock(returncode=1, stdout="")
    assert query_exit_info(adb, "com.example.app") == []


def test_parse_android12_plus_exit_info_format():
    text = (
        "ACTIVITY MANAGER PROCESS EXIT INFO (dumpsys activity exit-info)\n"
        "  package: com.example.app\n"
        "    Historical Process Exit for uid=10123\n"
        "        ApplicationExitInfo #0:\n"
        "          timestamp=2026-08-10 21:00:00.123 pid=1234 realUid=10123 "
        "packageUid=10123 definingUid=10123 user=0\n"
        "          process=com.example.app reason=5 (CRASHED) subreason=0 (UNKNOWN) "
        "status=5\n"
        "          importance=100 pss=12.3MB rss=34.5MB description=null "
        "state=empty trace=null\n"
    )
    records = parse_exit_info_text(text)
    assert len(records) == 1
    rec = records[0]
    assert rec.pid == 1234
    assert rec.exit_reason == "crashed"
    assert rec.pss_kb == int(12.3 * 1024)
    assert rec.rss_kb == int(34.5 * 1024)
    assert rec.is_stability_failure is True
