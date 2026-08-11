from __future__ import annotations

import json
from pathlib import Path

import pytest
from sat.indexer import render_trend_html, trend, write_trend


def _index_data():
    return {
        "runs": [
            {
                "device": "dev-1",
                "android_version": "16",
                "app_version_name": "2.3.1",
                "coverage_ratio": 0.98,
                "started_at": "2026-05-21 10:00:00.000",
                "issue_groups": [{
                    "fingerprint": "fpA",
                    "occurrence_count": 2,
                    "first_seen_at": "2026-05-21 10:00:00.000",
                    "last_seen_at": "2026-05-21 10:01:00.000",
                }],
            },
            {
                "device": "dev-2",
                "android_version": "15",
                "app_version_name": "2.3.1",
                "coverage_ratio": 0.99,
                "started_at": "2026-05-22 10:00:00.000",
                "issue_groups": [{
                    "fingerprint": "fpA",
                    "occurrence_count": 3,
                    "first_seen_at": "2026-05-22 10:00:00.000",
                    "last_seen_at": "2026-05-22 10:01:00.000",
                }, {
                    "fingerprint": "fpB",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-22 10:00:00.000",
                    "last_seen_at": "2026-05-22 10:00:30.000",
                }],
            },
        ]
    }


def test_trend_counts_match_original_sums(tmp_path: Path):
    result = trend(_index_data(), by="fingerprint")
    assert result["by"] == "fingerprint"
    row = result["rows"][0]
    assert row["occurrence_count"] == 5
    assert row["affected_devices"] == ["dev-1", "dev-2"]
    assert row["android_versions"] == ["15", "16"]
    assert row["app_versions"] == ["2.3.1"]
    assert row["avg_coverage"] == pytest.approx(0.985, abs=0.001)
    new_by_run = result["new_regressions_by_run"]
    assert [n["new_fingerprint_count"] for n in new_by_run] == [1, 1]


def test_write_trend_creates_json_and_html(tmp_path: Path):
    result = trend(_index_data())
    write_trend(result, tmp_path / "out")
    assert json.loads((tmp_path / "out" / "trend.json").read_text())["by"] == "fingerprint"
    html = (tmp_path / "out" / "trend.html").read_text()
    assert "fpA" in html
    assert "<table" in render_trend_html(result)
