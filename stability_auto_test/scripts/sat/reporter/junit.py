"""JUnit XML generation from the canonical report.

Semantics (fixed for CI consumers):
- one testcase per issue group (or per incident when no groups exist);
- a stability-gate failure becomes `<failure>`;
- an incomplete observation (`verdict == inconclusive`) becomes `<error>`;
- otherwise the testcase passes.
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


def render_junit(result: Dict) -> str:
    cases = _cases(result)
    verdict = result.get("verdict", "stable")
    policy = result.get("policy", {}) or {}
    gate_failed = policy.get("enabled") is True and policy.get("passed") is False

    passed: List[str] = []
    failures: List[str] = []
    errors: List[str] = []
    for case in cases:
        name = (case.get("fingerprint") or str(case.get("type") or "incident"))[:120]
        if gate_failed:
            failed_rules = [
                r["rule"] for r in (policy.get("rules") or []) if r.get("pass") is False
            ]
            failures.append(
                f'<testcase classname="stability_auto_test" name={quoteattr(name)}>'
                f"<failure message="
                f"{quoteattr('stability gate failed: ' + ', '.join(failed_rules))}/>"
                "</testcase>"
            )
        elif verdict == "inconclusive":
            errors.append(
                f'<testcase classname="stability_auto_test" name={quoteattr(name)}>'
                f"<error message={quoteattr('observation incomplete')}/>"
                "</testcase>"
            )
        else:
            passed.append(f'<testcase classname="stability_auto_test" name={quoteattr(name)}/>')

    # Empty report (no incidents / issue groups) with gate failure or
    # inconclusive verdict still needs a run-level testcase so CI sees the
    # problem rather than treating an empty suite as success.
    if not cases:
        if gate_failed:
            failed_rules = [
                r["rule"] for r in (policy.get("rules") or []) if r.get("pass") is False
            ]
            failures.append(
                f'<testcase classname="stability_auto_test" name="stability_gate">'
                f"<failure message="
                f"{quoteattr('stability gate failed: ' + ', '.join(failed_rules))}/>"
                "</testcase>"
            )
        elif verdict == "inconclusive":
            errors.append(
                '<testcase classname="stability_auto_test" name="stability_gate">'
                '<error message="observation incomplete (no incidents recorded)"/>'
                "</testcase>"
            )

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
