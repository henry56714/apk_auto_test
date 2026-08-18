"""Offline bugreport import (spec S3-01 / T-L0-029)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from sat.analyzers.fingerprint import fingerprint_incident
from sat.detection import LogcatLineParser
from sat.offline import SOURCE_MODE_OFFLINE, analyze_bugreport, parse_logcat_events

PACKAGE = "com.example.app"

CRASH_BLOCK = (
    "05-21 10:00:00.100  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main\n"
    "05-21 10:00:00.100  1234  1234 E AndroidRuntime: Process: com.example.app, PID: 1234\n"
    "05-21 10:00:00.100  1234  1234 E AndroidRuntime: java.lang.RuntimeException: boom\n"
    "05-21 10:00:00.100  1234  1234 E AndroidRuntime: \tat com.example.A.b(A.java:42)\n"
    "05-21 10:00:00.200  9999  9999 I OtherTag: end\n"
)


def _make_bugreport_zip(tmp_path: Path, logcat_text: str) -> Path:
    zip_path = tmp_path / "bugreport-test.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("bugreport-fake.txt", "Package [com.example.app] (u0a123)\n")
        zf.writestr("logcat.txt", logcat_text)
    return zip_path


def test_offline_and_live_parser_produce_same_fingerprint():
    """T-L0-029: the same raw record yields the same fingerprint whether it
    flows through the live logcat adapter or the offline bugreport adapter."""
    # Live path: the pool's parser fed line by line.
    live_parser = LogcatLineParser(
        PACKAGE,
        now_iso_fn=lambda: "2026-08-13T10:00:00Z",
    )
    live_events = []
    for line in CRASH_BLOCK.splitlines():
        live_events.extend(live_parser.feed_line(line))
    live_events.extend(live_parser.flush())
    assert len(live_events) == 1

    # Offline path: the same text through the bugreport parser.
    offline_events = parse_logcat_events(CRASH_BLOCK, PACKAGE)
    assert len(offline_events) == 1

    from sat.dumpers import build_incident_dict

    live_incident = build_incident_dict(
        live_events[0],
        logcat_slice_file=None,
        trace_file=None,
        fallback_reason=None,
    )
    offline_incident = build_incident_dict(
        offline_events[0],
        logcat_slice_file=None,
        trace_file=None,
        fallback_reason=None,
    )
    assert fingerprint_incident(live_incident) == fingerprint_incident(offline_incident)
    assert live_incident["type"] == offline_incident["type"] == "java_crash"


def test_analyze_bugreport_end_to_end(tmp_path: Path):
    zip_path = _make_bugreport_zip(tmp_path, CRASH_BLOCK)
    out = tmp_path / "offline-report"
    report = analyze_bugreport(zip_path, out)
    assert report["source_mode"] == SOURCE_MODE_OFFLINE
    assert report["verdict"] == "unstable"
    crashes = [i for i in report["incidents"] if i["type"] == "java_crash"]
    assert len(crashes) == 1
    assert crashes[0]["process"] == PACKAGE
    # Same report schema is valid.
    import jsonschema

    schema = json.loads(
        (Path(__file__).parent.parent / "schemas" / "report.schema.json").read_text()
    )
    jsonschema.validate(report, schema)
    # Pipeline identity holds for offline runs too.
    pipeline = report["event_pipeline"]
    assert pipeline["detected_count"] == pipeline["persisted_count"] == 1


def test_analyze_bugreport_requires_package(tmp_path: Path):
    zip_path = tmp_path / "nopkg.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("logcat.txt", CRASH_BLOCK)
    out = tmp_path / "offline-report2"
    import pytest

    with pytest.raises(ValueError):
        analyze_bugreport(zip_path, out)
