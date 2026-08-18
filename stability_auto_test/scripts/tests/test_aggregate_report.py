from __future__ import annotations

import json
from pathlib import Path

from sat.aggregate import aggregate_reports, write_aggregate


def _report(serial: str, groups: list, incidents: int = 1) -> dict:
    return {
        "_report_path": f"/reports/device_{serial}/report.json",
        "run": {
            "device": {
                "serial": serial,
                "android_version": "16",
                "sdk_int": 36,
            }
        },
        "verdict": "unstable",
        "coverage_ratio": 0.99,
        "incidents": [{} for _ in range(incidents)],
        "device_events": [
            {
                "event_type": "offline",
                "started_at": 1000.0,
                "ended_at": 1030.0,
                "detail": "gap",
            }
        ],
        "issue_groups": groups,
    }


def test_aggregate_merges_groups_and_devices():
    r1 = _report(
        "dev-1",
        [
            {
                "fingerprint": "fpA",
                "type": "anr",
                "occurrence_count": 2,
                "first_seen_at": "2026-05-21 10:00:00.000",
                "last_seen_at": "2026-05-21 10:02:00.000",
            },
        ],
    )
    r2 = _report(
        "dev-2",
        [
            {
                "fingerprint": "fpA",
                "type": "anr",
                "occurrence_count": 1,
                "first_seen_at": "2026-05-21 10:01:00.000",
                "last_seen_at": "2026-05-21 10:03:00.000",
            },
            {
                "fingerprint": "fpB",
                "type": "java_crash",
                "occurrence_count": 1,
                "first_seen_at": "2026-05-21 10:02:00.000",
                "last_seen_at": "2026-05-21 10:02:00.000",
            },
        ],
    )
    agg = aggregate_reports([r1, r2])
    assert agg["device_count"] == 2
    assert agg["aggregate_health"] == "healthy"
    by_fp = {g["fingerprint"]: g for g in agg["issue_groups"]}
    assert by_fp["fpA"]["occurrence_count"] == 3
    assert sorted(by_fp["fpA"]["affected_devices"]) == ["dev-1", "dev-2"]
    assert by_fp["fpB"]["affected_devices"] == ["dev-2"]
    assert agg["total_incidents"] == 2
    assert len(agg["device_events"]) == 2
    assert agg["device_events"][0]["device"] == "dev-1"


def test_aggregate_degraded_when_device_report_missing(tmp_path: Path):
    r1 = _report("dev-1", [])
    agg = aggregate_reports([r1, {}])
    assert agg["device_count"] == 2
    assert agg["ok_device_count"] == 1
    assert agg["aggregate_health"] == "degraded"


def test_write_aggregate_creates_json_and_html(tmp_path: Path):
    agg = aggregate_reports([_report("dev-1", [])])
    path = write_aggregate(agg, tmp_path / "agg")
    assert json.loads(path.read_text())["device_count"] == 1
    assert (tmp_path / "agg" / "aggregate.html").exists()
