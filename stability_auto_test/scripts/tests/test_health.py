"""Verdict / health / policy three-layer semantics (spec S1-01).

Tests T-L0-001 .. T-L0-004: a confirmed failure must survive degraded
coverage, incomplete observation may only block "stable", expected exits are
audited (not failures), and JUnit separates `<failure>` from `<error>`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from sat.health import (
    CONFIDENCE_HIGH,
    CONFIDENCE_NONE,
    CONFIDENCE_PARTIAL,
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    HEALTH_INCONCLUSIVE,
    VERDICT_INCONCLUSIVE,
    VERDICT_STABLE,
    VERDICT_UNSTABLE,
    compute_verdict,
)
from sat.reporter.junit import render_junit

FATAL = {
    "type": "java_crash",
    "process": "com.example.app",
    "pid": 42,
    "triggered_at": "2026-08-13 10:00:00.000",
    "severity": "fatal",
}


# ── T-L0-001: confirmed failure + degraded coverage => unstable ──────────────


def test_fatal_with_degraded_health_is_unstable():
    r = compute_verdict(
        HEALTH_DEGRADED,
        incidents=[dict(FATAL)],
    )
    assert r.verdict == VERDICT_UNSTABLE
    assert r.confidence == CONFIDENCE_PARTIAL
    assert any("confirmed failure" in reason for reason in r.reasons)
    assert any("degraded" in reason for reason in r.reasons)


def test_fatal_with_inconclusive_health_is_unstable():
    r = compute_verdict(HEALTH_INCONCLUSIVE, incidents=[dict(FATAL)])
    assert r.verdict == VERDICT_UNSTABLE
    assert r.confidence == CONFIDENCE_PARTIAL


def test_fatal_with_healthy_health_is_unstable_high_confidence():
    r = compute_verdict(HEALTH_HEALTHY, incidents=[dict(FATAL)])
    assert r.verdict == VERDICT_UNSTABLE
    assert r.confidence == CONFIDENCE_HIGH


# ── T-L0-002: no incidents + low coverage => inconclusive, never stable ──────


def test_no_incidents_low_coverage_is_inconclusive():
    r = compute_verdict(HEALTH_DEGRADED, incidents=[])
    assert r.verdict == VERDICT_INCONCLUSIVE
    assert r.confidence == CONFIDENCE_NONE
    assert r.verdict != VERDICT_STABLE


def test_no_incidents_never_collected_is_inconclusive():
    r = compute_verdict(HEALTH_INCONCLUSIVE, incidents=[])
    assert r.verdict == VERDICT_INCONCLUSIVE


# ── T-L0-003: only expected exits => stable when coverage complete ───────────


def test_expected_exits_do_not_fail():
    expected_death = {
        "type": "process_death",
        "process": "com.example.app",
        "pid": 7,
        "triggered_at": "2026-08-13 10:00:00.000",
        "evidence": {"reason": "cached-empty (19)"},
    }
    r = compute_verdict(HEALTH_HEALTHY, incidents=[expected_death])
    assert r.verdict == VERDICT_STABLE
    assert r.expected_count == 1
    assert any("audited" in reason for reason in r.reasons)


def test_explicit_expected_flag_audited_not_failure():
    expected_exit = {
        "type": "process_death",
        "process": "com.example.app",
        "pid": 7,
        "triggered_at": "2026-08-13 10:00:00.000",
        "evidence": {"expected": True, "reason": "workload action"},
    }
    r = compute_verdict(HEALTH_HEALTHY, incidents=[expected_exit])
    assert r.verdict == VERDICT_STABLE
    assert r.expected_count == 1


def test_expected_exit_with_degraded_coverage_is_inconclusive():
    expected_exit = {
        "type": "process_death",
        "process": "com.example.app",
        "pid": 7,
        "triggered_at": "2026-08-13 10:00:00.000",
        "evidence": {"expected": True},
    }
    r = compute_verdict(HEALTH_DEGRADED, incidents=[expected_exit])
    assert r.verdict == VERDICT_INCONCLUSIVE


# ── T-L0-004: fatal + inconclusive health => JUnit failure with coverage ─────


def _junit_for(incidents, health="healthy", coverage=1.0):
    from sat.policy import PolicyConfig, evaluate_policy

    policy_result = evaluate_policy(incidents, [], coverage, PolicyConfig())
    policy_result["enabled"] = True
    r = compute_verdict(health, incidents=incidents)
    result = {
        "verdict": r.verdict,
        "verdict_reason": r.reasons,
        "collection_health": health,
        "coverage_ratio": coverage,
        "policy": policy_result,
        "issue_groups": [
            {
                "fingerprint": f"fp-{i}",
                "type": inc.get("type"),
                "occurrence_count": 1,
                "occurrence_ids": [f"incident-{i}"],
            }
            for i, inc in enumerate(incidents, start=1)
        ],
        "incidents": incidents,
    }
    return render_junit(result)


def test_junit_failure_wins_with_health_info():
    xml = _junit_for([dict(FATAL)], health=HEALTH_INCONCLUSIVE, coverage=0.7)
    root = ET.fromstring(xml)
    assert int(root.attrib["failures"]) >= 1
    assert int(root.attrib["errors"]) == 0
    failure = root.find(".//failure")
    assert failure is not None
    assert "coverage=0.7" in failure.attrib["message"]
    assert "inconclusive" in failure.attrib["message"]
    # Top-level counts match the testcases emitted.
    assert int(root.attrib["tests"]) == len(root.findall(".//testcase"))


def test_junit_pure_inconclusive_is_error():
    xml = _junit_for([], health=HEALTH_DEGRADED, coverage=0.7)
    root = ET.fromstring(xml)
    assert int(root.attrib["errors"]) == 1
    assert int(root.attrib["failures"]) == 0
    err = root.find(".//error")
    assert err is not None
    assert "observation incomplete" in err.attrib["message"]


def test_junit_fatal_and_gate_failure_are_single_failure():
    xml = _junit_for([dict(FATAL)], health=HEALTH_HEALTHY, coverage=1.0)
    root = ET.fromstring(xml)
    assert int(root.attrib["failures"]) == 1
    assert int(root.attrib["errors"]) == 0
    assert int(root.attrib["tests"]) == 1


# ── unknown abnormal exit blocks stable (spec 4.3) ───────────────────────────


def test_unknown_process_death_makes_clean_run_inconclusive():
    unknown = {
        "type": "process_death",
        "process": "com.example.app",
        "pid": 9,
        "triggered_at": "2026-08-13 10:00:00.000",
        "evidence": {"reason": "unknown_reason"},
    }
    r = compute_verdict(HEALTH_HEALTHY, incidents=[unknown])
    assert r.verdict == VERDICT_INCONCLUSIVE


# ── regression: healthy + clean => stable, high confidence ───────────────────


def test_clean_healthy_run_is_stable_high_confidence():
    r = compute_verdict(HEALTH_HEALTHY, incidents=[])
    assert r.verdict == VERDICT_STABLE
    assert r.confidence == CONFIDENCE_HIGH
