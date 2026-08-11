"""Built-in configuration presets."""

from __future__ import annotations

from typing import Dict, Optional, Tuple

PROFILES: Dict[str, Dict] = {
    "smoke": {
        "pre_context_sec": 10,
        "post_context_sec": 5,
        "max_incidents_per_type": 50,
        "status_interval_sec": 5,
        "resource_risk_interval_sec": 10,
        "min_coverage_ratio": 0.9,
        "duration_sec": 30,
    },
    "soak": {
        "pre_context_sec": 30,
        "post_context_sec": 10,
        "max_incidents_per_type": 200,
        "status_interval_sec": 15,
        "resource_risk_interval_sec": 30,
        "min_coverage_ratio": 0.99,
        "duration_sec": 3600,
    },
    "overnight": {
        "pre_context_sec": 30,
        "post_context_sec": 10,
        "max_incidents_per_type": 500,
        "status_interval_sec": 30,
        "resource_risk_interval_sec": 60,
        "min_coverage_ratio": 0.99,
        "duration_sec": 8 * 3600,
    },
    "automotive": {
        "pre_context_sec": 60,
        "post_context_sec": 30,
        "max_incidents_per_type": 300,
        "status_interval_sec": 10,
        "resource_risk_interval_sec": 30,
        "min_coverage_ratio": 0.95,
        "duration_sec": 4 * 3600,
    },
}


def apply_profile(
    cfg_kwargs: Dict,
    profile: str,
) -> Tuple[Dict, Dict[str, str]]:
    """Apply profile defaults; returns (kwargs, value->source map)."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile!r}")
    sources: Dict[str, str] = {}
    for key, value in PROFILES[profile].items():
        if key == "duration_sec":
            continue
        if key not in cfg_kwargs:
            cfg_kwargs[key] = value
            sources[key] = "profile"
    cfg_kwargs["profile_name"] = profile
    sources["profile_name"] = "profile"
    return cfg_kwargs, sources


def profile_duration(profile: Optional[str]) -> Optional[int]:
    if not profile:
        return None
    return int(PROFILES[profile]["duration_sec"])
