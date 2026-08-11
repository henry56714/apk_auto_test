"""GitHub Actions step summary writer."""

from __future__ import annotations

import os
from typing import Dict


def write_github_summary(
    result: Dict,
    *,
    env_var: str = "GITHUB_STEP_SUMMARY",
) -> bool:
    """Append a Markdown summary to `$GITHUB_STEP_SUMMARY` if present."""
    path = os.environ.get(env_var)
    if not path:
        return False
    verdict = result.get("verdict", "unknown")
    incidents = result.get("incidents") or []
    policy = result.get("policy", {}) or {}
    lines = [
        "## stability_auto_test",
        f"- verdict: `{verdict}`",
        f"- incidents: {len(incidents)}",
        f"- coverage: {result.get('coverage_ratio', 'n/a')}",
        f"- policy passed: `{policy.get('passed', 'n/a')}`",
        "- report.json: `"
        f"{result.get('run', {}).get('config_effective', {}).get('output_dir', '')}`",
        "",
    ]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return True
