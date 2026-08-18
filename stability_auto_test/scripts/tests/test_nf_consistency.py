"""Report consistency across formats (T-NF-004) and no-fabrication rules."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from sat.reporter.junit import render_junit


def _base_result(verdict: str) -> dict:
    return {
        "schema_version": "1.15",
        "verdict": verdict,
        "verdict_reason": ["test reason"],
        "verdict_confidence": "high" if verdict != "inconclusive" else "none",
        "collection_health": "healthy",
        "coverage_ratio": 0.99,
        "expected_exit_count": 0,
        "policy": {
            "enabled": False,
            "passed": True,
            "rules": [],
        },
        "incidents": (
            [
                {
                    "id": "incident-001",
                    "type": "java_crash",
                    "process": "com.example.app",
                    "pid": 1,
                    "triggered_at": "2026-08-13 10:00:00.000",
                    "severity": "fatal",
                    "summary": "boom",
                    "evidence": {"exception_class": "RuntimeException"},
                }
            ]
            if verdict == "unstable"
            else []
        ),
        "issue_groups": [],
        "exit_info": [],
        "device_events": [],
        "resource_risk": [],
    }


def test_junit_counts_consistent_for_all_verdicts():
    # unstable → failures
    xml = render_junit(_base_result("unstable"))
    root = ET.fromstring(xml)
    assert int(root.attrib["failures"]) >= 1
    assert int(root.attrib["errors"]) == 0
    # inconclusive → errors
    xml = render_junit(_base_result("inconclusive"))
    root = ET.fromstring(xml)
    assert int(root.attrib["errors"]) == 1
    assert int(root.attrib["failures"]) == 0
    # stable → pass
    xml = render_junit(_base_result("stable"))
    root = ET.fromstring(xml)
    assert int(root.attrib["failures"]) == 0
    assert int(root.attrib["errors"]) == 0
    # Top-level counts always equal emitted testcases.
    for verdict in ("stable", "unstable", "inconclusive"):
        root = ET.fromstring(render_junit(_base_result(verdict)))
        assert int(root.attrib["tests"]) == len(root.findall(".//testcase"))


def test_junit_never_fabricates_facts():
    """JUnit carries only verdict/policy facts — never invented incidents."""
    xml = render_junit(_base_result("stable"))
    assert "java_crash" not in xml
    xml = render_junit(_base_result("unstable"))
    assert "confirmed stability failure" in xml


def test_empty_run_still_has_run_level_testcase():
    xml = render_junit(_base_result("stable"))
    root = ET.fromstring(xml)
    assert int(root.attrib["tests"]) >= 1
    assert root.find(".//testcase") is not None
