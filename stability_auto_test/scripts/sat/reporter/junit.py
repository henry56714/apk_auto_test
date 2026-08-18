"""JUnit XML generation from the canonical report.

Semantics (spec S1-01 — verdict-driven, three layers never mixed):

- a *confirmed* stability failure (`verdict == unstable`) becomes `<failure>`,
  and takes priority over every other signal; when collection health is also
  degraded the failure message carries the coverage/health information;
- a pure observation gap (`verdict == inconclusive`, no confirmed failure)
  becomes `<error>`;
- a CI stability-gate failure without a confirmed failure also becomes
  `<failure>` (the gate is the CI contract);
- otherwise the testcase passes.

Top-level `tests/failures/errors` counts always match the testcases emitted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List
from xml.sax.saxutils import quoteattr

JUNIT_FILENAME = "junit.xml"


def _cases(result: Dict) -> List[Dict]:
    groups = result.get("issue_groups") or []
    if groups:
        return groups
    return [
        {"occurrence_ids": [i.get("id")], "type": i.get("type")}
        for i in (result.get("incidents") or [])
    ]


def _failed_rules(policy: Dict) -> List[str]:
    return [r["rule"] for r in (policy.get("rules") or []) if r.get("pass") is False]


def _failure_message(result: Dict) -> str:
    """Build the failure message for confirmed failures.

    Leads with the verdict reasons and appends collection health/coverage so
    "confirmed failure under degraded observation" is visible in CI output.
    """
    parts: List[str] = []
    reasons = result.get("verdict_reason") or []
    if reasons:
        parts.append("confirmed stability failure: " + "; ".join(reasons))
    else:
        parts.append("confirmed stability failure")
    health = result.get("collection_health", "healthy")
    if health != "healthy":
        parts.append(f"collection_health={health} coverage={result.get('coverage_ratio', 1.0)}")
    return "; ".join(parts)


def render_junit(result: Dict) -> str:
    cases = _cases(result)
    verdict = result.get("verdict", "stable")
    policy = result.get("policy", {}) or {}
    gate_failed = policy.get("enabled") is True and policy.get("passed") is False

    passed: List[str] = []
    failures: List[str] = []
    errors: List[str] = []

    if verdict == "unstable":
        # Confirmed failure wins; attach health info when observation was
        # degraded at the same time, and the gate rules when they failed too.
        msg = _failure_message(result)
        if gate_failed:
            msg += "; stability gate failed: " + ", ".join(_failed_rules(policy))
        for case in cases:
            name = (case.get("fingerprint") or str(case.get("type") or "incident"))[:120]
            failures.append(
                f'<testcase classname="stability_auto_test" name={quoteattr(name)}>'
                f"<failure message={quoteattr(msg)}/>"
                "</testcase>"
            )
    elif verdict == "inconclusive":
        # Pure observation gap: `<error>` even when the CI gate would fail on
        # coverage — the same underlying fact must not be double-reported.
        for case in cases:
            name = (case.get("fingerprint") or str(case.get("type") or "incident"))[:120]
            errors.append(
                f'<testcase classname="stability_auto_test" name={quoteattr(name)}>'
                f"<error message={quoteattr('observation incomplete')}/>"
                "</testcase>"
            )
    elif gate_failed:
        # Stable observation that still breaks a stability rule (restarts,
        # uptime, process deaths): the CI gate is the contract here.
        for case in cases:
            name = (case.get("fingerprint") or str(case.get("type") or "incident"))[:120]
            failures.append(
                f'<testcase classname="stability_auto_test" name={quoteattr(name)}>'
                f"<failure message="
                f"{quoteattr('stability gate failed: ' + ', '.join(_failed_rules(policy)))}/>"
                "</testcase>"
            )
    else:
        for case in cases:
            name = (case.get("fingerprint") or str(case.get("type") or "incident"))[:120]
            passed.append(f'<testcase classname="stability_auto_test" name={quoteattr(name)}/>')

    # Empty report (no incidents / issue groups) still needs a run-level
    # testcase so CI sees failure/inconclusive rather than an empty suite.
    if not cases:
        if verdict == "unstable":
            msg = _failure_message(result)
            if gate_failed:
                msg += "; stability gate failed: " + ", ".join(_failed_rules(policy))
            failures.append(
                '<testcase classname="stability_auto_test" name="stability_gate">'
                f"<failure message={quoteattr(msg)}/>"
                "</testcase>"
            )
        elif verdict == "inconclusive":
            errors.append(
                '<testcase classname="stability_auto_test" name="stability_gate">'
                '<error message="observation incomplete (no incidents recorded)"/>'
                "</testcase>"
            )
        elif gate_failed:
            failures.append(
                '<testcase classname="stability_auto_test" name="stability_gate">'
                f"<failure message="
                f"{quoteattr('stability gate failed: ' + ', '.join(_failed_rules(policy)))}/>"
                "</testcase>"
            )
        else:
            # Empty but healthy run: still emit a run-level testcase so an
            # empty suite can never be misread as "no tests executed".
            passed.append('<testcase classname="stability_auto_test" name="stability_gate"/>')

    all_cases = passed + failures + errors
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuites tests="{len(all_cases)}" failures="{len(failures)}" '
        f'errors="{len(errors)}">\n'
        f'<testsuite name="stability_auto_test" tests="{len(all_cases)}" '
        f'failures="{len(failures)}" errors="{len(errors)}">\n'
        + "\n".join(all_cases)
        + "\n</testsuite>\n</testsuites>\n"
    )


def write_junit(result: Dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_junit(result), encoding="utf-8")
    return path
