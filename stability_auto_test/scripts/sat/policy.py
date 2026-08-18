"""Opt-in CI stability gate.

Default behavior stays collect-only (exit 0 even with crashes, verdict reports
`unstable`). With `--ci` (or `policy.enabled`), the run exits 1 when any rule
fails and 4 when observation is inconclusive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .health import is_expected_incident


@dataclass
class PolicyConfig:
    fail_on: List[str] = field(
        default_factory=lambda: ["java_crash", "native_crash", "anr"],
    )
    max_process_death: int = 0
    max_anr: int = 0
    max_restarts: int = 0
    min_uptime_ratio: float = 0.99
    min_coverage_ratio: float = 0.99
    fail_on_new_regression_only: bool = False


def policy_from_dict(data: Dict) -> PolicyConfig:
    return PolicyConfig(
        fail_on=list(data.get("fail_on", ["java_crash", "native_crash", "anr"])),
        max_process_death=int(data.get("max_process_death", 0)),
        max_anr=int(data.get("max_anr", 0)),
        max_restarts=int(data.get("max_restarts", 0)),
        min_uptime_ratio=float(data.get("min_uptime_ratio", 0.99)),
        min_coverage_ratio=float(data.get("min_coverage_ratio", 0.99)),
        fail_on_new_regression_only=bool(data.get("fail_on_new_regression_only", False)),
    )


def evaluate_policy(
    incidents: List[Dict],
    processes: List[Dict],
    coverage_ratio: float,
    policy: PolicyConfig,
) -> Dict:
    rules: List[Dict] = []

    # Expected exits (force-stop, cached recycle, workload action windows, ...)
    # are audited, never policy failures.
    fatal_counts = {
        t: sum(1 for i in incidents if i.get("type") == t and not is_expected_incident(i))
        for t in policy.fail_on
    }
    rules.append(
        {
            "rule": "fail_on",
            "actual": fatal_counts,
            "threshold": policy.fail_on,
            "pass": all(v == 0 for v in fatal_counts.values()),
        }
    )

    anr_count = sum(1 for i in incidents if i.get("type") == "anr" and not is_expected_incident(i))
    rules.append(
        {
            "rule": "max_anr",
            "actual": anr_count,
            "threshold": policy.max_anr,
            "pass": anr_count <= policy.max_anr,
        }
    )

    death_count = sum(
        1 for i in incidents if i.get("type") == "process_death" and not is_expected_incident(i)
    )
    rules.append(
        {
            "rule": "max_process_death",
            "actual": death_count,
            "threshold": policy.max_process_death,
            "pass": death_count <= policy.max_process_death,
        }
    )

    restart_count = sum(p.get("restart_count", 0) for p in processes)
    rules.append(
        {
            "rule": "max_restarts",
            "actual": restart_count,
            "threshold": policy.max_restarts,
            "pass": restart_count <= policy.max_restarts,
        }
    )

    uptime = min(
        (p.get("uptime_ratio", 1.0) for p in processes),
        default=1.0,
    )
    rules.append(
        {
            "rule": "min_uptime_ratio",
            "actual": round(uptime, 4),
            "threshold": policy.min_uptime_ratio,
            "pass": uptime >= policy.min_uptime_ratio,
        }
    )

    rules.append(
        {
            "rule": "min_coverage_ratio",
            "actual": round(coverage_ratio, 4),
            "threshold": policy.min_coverage_ratio,
            "pass": coverage_ratio >= policy.min_coverage_ratio,
        }
    )

    return {
        "rules": rules,
        "passed": all(r["pass"] for r in rules),
    }
