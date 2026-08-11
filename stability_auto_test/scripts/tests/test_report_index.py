from __future__ import annotations

import json
from pathlib import Path

from sat.indexer import load_index, scan_reports, write_index


def _report(tmp: Path, name: str, fp: str, count: int) -> Path:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "report.json"
    p.write_text(json.dumps({
        "schema_version": "1.12",
        "run": {
            "run_id": f"run-{name}",
            "started_at": "2026-05-21 10:00:00.000",
            "package": "com.example.app",
            "device": {"serial": "dev-1", "android_version": "16"},
        },
        "verdict": "unstable",
        "incidents": [{}] * count,
        "issue_groups": [{
            "fingerprint": fp,
            "type": "anr",
            "occurrence_count": count,
            "first_seen_at": "2026-05-21 10:00:00.000",
            "last_seen_at": "2026-05-21 10:01:00.000",
        }],
    }), encoding="utf-8")
    return p


def test_index_scan_skip_damaged_and_no_duplicates(tmp_path: Path):
    _report(tmp_path, "run1", "fpA", 2)
    _report(tmp_path, "run2", "fpA", 3)
    broken = tmp_path / "broken" / "report.json"
    broken.parent.mkdir()
    broken.write_text("{not json", encoding="utf-8")

    data = scan_reports(tmp_path)
    assert data["run_count"] == 2
    assert len(data["errors"]) == 1
    path = write_index(tmp_path, data)
    assert load_index(tmp_path)["run_count"] == 2

    again = scan_reports(tmp_path)
    assert again["run_count"] == data["run_count"]

    path.unlink()
    assert (tmp_path / "run1" / "report.json").exists()


def test_index_records_device_and_incident_counts(tmp_path: Path):
    _report(tmp_path, "run1", "fpA", 2)
    data = scan_reports(tmp_path)
    entry = data["runs"][0]
    assert entry["device"] == "dev-1"
    assert entry["incident_count"] == 2
