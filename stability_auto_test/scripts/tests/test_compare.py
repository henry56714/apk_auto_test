from __future__ import annotations

import json
from pathlib import Path

import pytest
from sat import cli
from sat.compare import CompareError, compare_reports, load_report

FIXTURES = Path(__file__).parent / "fixtures" / "reports"


def _load(name: str):
    return load_report(FIXTURES / name)


def test_compare_classifies_all_categories():
    result = compare_reports(_load("baseline.json"), _load("current.json"))
    assert [i["fingerprint"] for i in result["new_regressions"]] == ["fpD"]
    assert [i["fingerprint"] for i in result["fixed"]] == ["fpB"]
    assert [i["fingerprint"] for i in result["worsened"]] == ["fpC"]
    assert [i["fingerprint"] for i in result["unchanged"]] == ["fpA"]
    assert result["worsened"][0]["baseline_count"] == 1
    assert result["worsened"][0]["current_count"] == 3


def test_compare_cli_writes_json(tmp_path: Path):
    out = tmp_path / "compare"
    rc = cli.main([
        "compare",
        "--baseline", str(FIXTURES / "baseline.json"),
        "--current", str(FIXTURES / "current.json"),
        "--output", str(out),
    ])
    assert rc == cli.EXIT_OK
    data = json.loads((out / "compare.json").read_text())
    assert len(data["new_regressions"]) == 1
    assert (out / "compare.html").exists()


def test_compare_fail_on_new_regression_returns_gate_failed(tmp_path: Path):
    rc = cli.main([
        "compare",
        "--baseline", str(FIXTURES / "baseline.json"),
        "--current", str(FIXTURES / "current.json"),
        "--output", str(tmp_path / "out"),
        "--fail-on-new-regression",
    ])
    assert rc == cli.EXIT_GATE_FAILED


def test_compare_known_issues_only_passes_gate(tmp_path: Path):
    only_known = tmp_path / "only_known.json"
    only_known.write_text(json.dumps({
        "schema_version": "1.7",
        "issue_groups": [
            {
                "fingerprint": "fpA",
                "type": "anr",
                "occurrence_count": 2,
                "affected_devices": ["dev-1"],
            }
        ],
    }), encoding="utf-8")
    rc = cli.main([
        "compare",
        "--baseline", str(FIXTURES / "baseline.json"),
        "--current", str(only_known),
        "--output", str(tmp_path / "out"),
        "--fail-on-new-regression",
    ])
    assert rc == cli.EXIT_OK


def test_compare_unparseable_baseline_is_tool_error(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    rc = cli.main([
        "compare",
        "--baseline", str(bad),
        "--current", str(FIXTURES / "current.json"),
        "--output", str(tmp_path / "out"),
    ])
    assert rc == cli.EXIT_SETUP


def test_compare_schema_incompatible_rejected(tmp_path: Path):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({
        "schema_version": "1.0",
        "issue_groups": [],
    }), encoding="utf-8")
    with pytest.raises(CompareError, match="incompatible"):
        compare_reports(_load("baseline.json"), load_report(other))
