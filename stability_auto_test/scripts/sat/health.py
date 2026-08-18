"""Collector / device health and coverage computation.

`coverage_ratio` is the fraction of the planned run during which logcat was
actually collecting. A run with low coverage or a completely unavailable core
collector must never be reported as "stable".

Verdict semantics (spec 4.3 — three independent layers):

- a *confirmed* failure (java/native crash or ANR that is not marked expected)
  always wins: `verdict=unstable`, regardless of collection health;
- with no confirmed failure, incomplete observation yields `inconclusive`;
- only a clean, fully-observed run yields `stable`.

`collection_health` (degraded/inconclusive) and `verdict_confidence`
(high/partial/none) are preserved independently so CI can distinguish
"confirmed failure under degraded coverage" from "no conclusion".
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

CONFIDENCE_HIGH = "high"
CONFIDENCE_PARTIAL = "partial"
CONFIDENCE_NONE = "none"

# Incident types that are deterministic failures regardless of coverage.
FATAL_INCIDENT_TYPES = ("java_crash", "native_crash", "anr")

# Exit reasons that describe *expected* process endings (normal user/system
# lifecycle, workload-driven exits, self-exit). These never count as failures
# but are still audited in `verdict_reason`. Matches `policy._normal_recycle`
# plus the ExitInfo `expected` classification.
EXPECTED_EXIT_REASON_SUBSTRINGS = (
    "cached",
    "force-stop",
    "force_stop",
    "user requested",
    "user stopped",
    "user_requested",
    "user_stopped",
    "exit_self",
    "normal_recycle",
    "package_updated",
)


@dataclass
class VerdictResult:
    verdict: str
    reasons: List[str] = field(default_factory=list)
    confidence: str = CONFIDENCE_NONE
    expected_count: int = 0


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
        reasons.append(f"coverage {coverage:.3f} below threshold {min_coverage_ratio}")
    else:
        health = HEALTH_HEALTHY

    if logcat_stats.get("reconnects"):
        reasons.append(f"logcat reconnected {logcat_stats['reconnects']} time(s)")
    if parse_failures:
        reasons.append(f"logcat parse failures: {parse_failures}")
    if adb_call_failures:
        reasons.append(f"adb call failures: {adb_call_failures}")

    return CollectorHealth(
        coverage_ratio=round(coverage, 4),
        health=health,
        reasons=reasons,
    )


def is_expected_incident(incident: Dict) -> bool:
    """Whether an incident describes an *expected* ending (audited, not a failure).

    Covers policy-style normal recycles, workload-marked exits and explicit
    `expected` flags set by an action window or the ExitInfo classifier.
    """
    evidence = incident.get("evidence") or {}
    if evidence.get("expected") is True or evidence.get("workload_expected") is True:
        return True
    reason = str(
        evidence.get("reason") or evidence.get("exit_info_reason") or incident.get("reason") or ""
    ).lower()
    return any(r in reason for r in EXPECTED_EXIT_REASON_SUBSTRINGS)


def compute_verdict(
    health: str,
    *,
    incidents: List[Dict],
    fatal_types: tuple = FATAL_INCIDENT_TYPES,
) -> VerdictResult:
    """Derive the run verdict from incidents first, then collection health.

    Rules (spec 4.3):

    1. Any confirmed failure (fatal-type incident that is not expected)
       => `unstable`.  `collection_health` may be degraded/inconclusive at the
       same time; that only lowers `verdict_confidence` to `partial`.
    2. No failure + unhealthy collection (or an unknown abnormal exit)
       => `inconclusive`.
    3. No failure + healthy collection => `stable`.

    Expected exits never count as failures but are audited via
    ``expected_count`` / a `verdict_reason` entry.
    """
    reasons: List[str] = []
    expected_count = 0
    fatal_incidents: List[Dict] = []
    unknown_abnormal = 0

    for inc in incidents:
        if is_expected_incident(inc):
            expected_count += 1
            continue
        inc_type = inc.get("type")
        if inc_type in fatal_types or inc.get("severity") == "fatal":
            fatal_incidents.append(inc)
        elif inc_type == "process_death":
            # Not expected and not fatal: an exit we cannot explain counts as
            # an unknown abnormal exit (spec 4.3), which blocks "stable".
            unknown_abnormal += 1

    if fatal_incidents:
        counts: Dict[str, int] = {}
        for inc in fatal_incidents:
            t = inc.get("type", "unknown")
            counts[t] = counts.get(t, 0) + 1
        reasons.append(
            "confirmed failure: " + ", ".join(f"{t} x{c}" for t, c in sorted(counts.items()))
        )
        if health != HEALTH_HEALTHY:
            reasons.append(f"collection health is {health} (coverage incomplete)")
            confidence = CONFIDENCE_PARTIAL
        else:
            confidence = CONFIDENCE_HIGH
        verdict = VERDICT_UNSTABLE
    elif health != HEALTH_HEALTHY:
        reasons.append(f"no failure detected, but collection health is {health}")
        confidence = CONFIDENCE_NONE
        verdict = VERDICT_INCONCLUSIVE
    elif unknown_abnormal:
        reasons.append(
            f"no fatal failure detected, but {unknown_abnormal} "
            "unexpected process exit(s) need explanation"
        )
        confidence = CONFIDENCE_NONE
        verdict = VERDICT_INCONCLUSIVE
    else:
        reasons.append("no failures detected; core collection complete")
        confidence = CONFIDENCE_HIGH
        verdict = VERDICT_STABLE

    if expected_count:
        reasons.append(f"{expected_count} expected exit(s) audited (not failures)")

    return VerdictResult(
        verdict=verdict,
        reasons=reasons,
        confidence=confidence,
        expected_count=expected_count,
    )
