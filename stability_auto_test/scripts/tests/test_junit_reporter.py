from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from sat.reporter.junit import render_junit, write_junit


def _result(**overrides) -> dict:
    data = {
        "verdict": "unstable",
        "policy": {
            "enabled": True,
            "passed": False,
            "rules": [
                {"rule": "fail_on", "pass": False},
                {"rule": "max_anr", "pass": True},
            ],
        },
        "issue_groups": [
            {
                "fingerprint": "abc123",
                "type": "java_crash",
                "occurrence_count": 2,
                "occurrence_ids": ["incident-001", "incident-002"],
            },
            {
                "fingerprint": "def456",
                "type": "anr",
                "occurrence_count": 1,
                "occurrence_ids": ["incident-003"],
            },
        ],
        "incidents": [],
        "coverage_ratio": 1.0,
    }
    data.update(overrides)
    return data


def test_junit_counts_match_report(tmp_path: Path):
    result = _result()
    path = write_junit(result, tmp_path / "junit.xml")
    tree = ET.parse(path)
    root = tree.getroot()
    assert root.tag == "testsuites"
    assert int(root.attrib["tests"]) == 2
    assert int(root.attrib["failures"]) == 2
    assert int(root.attrib["errors"]) == 0
    cases = root.findall(".//testcase")
    assert len(cases) == 2
    assert cases[0].attrib["name"] == "abc123"
    assert cases[0].find("failure") is not None
    assert "fail_on" in cases[0].find("failure").attrib["message"]


def test_junit_inconclusive_uses_error():
    result = _result(verdict="inconclusive", policy={"enabled": False, "passed": True, "rules": []})
    xml = render_junit(result)
    root = ET.fromstring(xml)
    assert int(root.attrib["errors"]) == 2
    assert int(root.attrib["failures"]) == 0


def test_junit_escapes_special_characters(tmp_path: Path):
    result = _result()
    result["issue_groups"][0]["fingerprint"] = 'a<b>&"c"'
    xml = render_junit(result)
    root = ET.fromstring(xml)
    names = [c.attrib["name"] for c in root.findall(".//testcase")]
    assert 'a<b>&"c"' in names
    assert "<failure" not in names[0]
