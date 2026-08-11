"""Collector / device health and coverage computation.

`coverage_ratio` is the fraction of the planned run during which logcat was
actually collecting. A run with low coverage or a completely unavailable core
collector must never be reported as "stable" — the verdict becomes
`inconclusive` instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_INCONCLUSIVE = "inconclusive"

VERDICT_STABLE = "stable"
VERDICT_UNSTABLE = "unstable"
VERDICT_INCONCLUSIVE = "inconclusive"


@dataclass
class CollectorHealth:
    coverage_ratio: float = 0.0
    health: str = HEALTH_INCONCLUSIVE
    reasons: List[str] = field(default_factory=list)


def compute_collector_health(
    *,
    logcat_stats: Optional[Dict] = None,
    planned_sec: float,
    min_coverage_ratio: float = 0.99,
    logcat_enabled: bool = True,
    parse_failures: int = 0,
    adb_call_failures: int = 0,
) -> CollectorHealth:
    logcat_stats = logcat_stats or {}
    planned_sec = max(0.0, float(planned_sec))
    up_intervals = logcat_stats.get("up_intervals") or []
    success_sec = sum(max(0.0, end - start) for start, end in up_intervals)
    coverage = min(1.0, success_sec / planned_sec) if planned_sec > 0 else 0.0
    reasons: List[str] = []

    if not logcat_enabled:
        health = HEALTH_INCONCLUSIVE
        reasons.append("logcat collector disabled")
    elif planned_sec <= 0 or coverage <= 0:
        health = HEALTH_INCONCLUSIVE
        reasons.append("logcat collector never collected")
    elif coverage < min_coverage_ratio:
        health = HEALTH_DEGRADED
        reasons.append(
            f"coverage {coverage:.3f} below threshold {min_coverage_ratio}"
        )
    else:
        health = HEALTH_HEALTHY

    if logcat_stats.get("reconnects"):
        reasons.append(
            f"logcat reconnected {logcat_stats['reconnects']} time(s)"
        )
    if parse_failures:
        reasons.append(f"logcat parse failures: {parse_failures}")
    if adb_call_failures:
        reasons.append(f"adb call failures: {adb_call_failures}")

    return CollectorHealth(
        coverage_ratio=round(coverage, 4),
        health=health,
        reasons=reasons,
    )


def compute_verdict(
    health: str,
    *,
    incidents: List[Dict],
) -> str:
    """Derive the run verdict from collection health + incidents.

    Coverage/collector problems always win over a "clean" result; only a
    healthy collection can be judged stable or unstable.
    """
    if health != HEALTH_HEALTHY:
        return VERDICT_INCONCLUSIVE
    fatal_types = {"java_crash", "native_crash", "anr"}
    if any(inc.get("type") in fatal_types for inc in incidents):
        return VERDICT_UNSTABLE
    return VERDICT_STABLE
