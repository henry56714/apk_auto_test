"""Strict YAML config validation.

Unknown fields are rejected by default (with a hint listing legal fields); a
lenient mode ignores unknown fields but still validates values. All time,
count, buffer, filter and path parameters are range/type checked.
"""

from __future__ import annotations

from typing import Any, Dict, List

ALLOWED_KEYS = {
    "package",
    "device",
    "discovery",
    "collectors",
    "detection",
    "dumps",
    "health",
    "diagnosis",
    "policy",
    "quota",
    "redaction",
    "webhook",
    "plugins",
    "profile",
    "output",
}

ALLOWED_NESTED = {
    "discovery": {"wait_timeout_sec", "rescan_interval_sec", "process_filter"},
    "collectors": {"logcat", "device_health", "resource_risk"},
    "collectors.logcat": {"enabled", "buffers", "reconnect_backoff_sec"},
    "collectors.device_health": {"interval_sec", "reboot_policy"},
    "collectors.resource_risk": {
        "enabled", "interval_sec", "fd_growth_threshold",
        "thread_growth_threshold",
    },
    "detection": {
        "enable_java_crash", "enable_native_crash", "enable_anr",
        "enable_process_death", "dedup_window_sec",
    },
    "dumps": {
        "pre_context_sec", "post_context_sec", "max_incidents_per_type",
        "dump_shutdown_timeout_sec", "context_retention_sec",
        "context_buffer_max_lines", "context_buffer_max_bytes",
        "pull_tombstone", "pull_anr_trace",
    },
    "health": {"min_coverage_ratio"},
    "diagnosis": {
        "mapping_file", "retrace_command", "native_symbols_dir",
        "llvm_symbolizer_path",
    },
    "policy": {
        "fail_on", "max_process_death", "max_anr", "max_restarts",
        "min_uptime_ratio", "min_coverage_ratio",
        "fail_on_new_regression_only",
    },
    "quota": {
        "max_disk_bytes", "max_log_file_bytes", "log_retention_hours",
        "max_queue_size", "evidence_sample_every_n",
        "self_monitor_interval_sec",
    },
    "redaction": {"enabled", "regexes"},
    "webhook": {"url", "events", "rate_limit_sec"},
    "plugins": {"enabled"},
    "output": {"emit_html", "status_interval_sec", "dashboard"},
}


def _type_errors(path: str, value: Any, kinds) -> List[str]:
    if isinstance(value, kinds):
        return []
    return [f"{path}: expected {kinds!r}, got {type(value).__name__}"]


def validate_config(data: Dict, *, lenient: bool = False) -> List[str]:
    """Return a list of validation errors (empty = valid)."""
    errors: List[str] = []

    if not isinstance(data, dict):
        return ["config root must be a mapping"]

    for key in data:
        if key not in ALLOWED_KEYS:
            errors.append(
                f"unknown field '{key}' (allowed: {sorted(ALLOWED_KEYS)})"
            )

    def check(path: str, value: Any, *, minimum=None, maximum=None, kinds=(int, float)):
        errors.extend(_type_errors(path, value, kinds))
        if minimum is not None and isinstance(value, (int, float)) and value < minimum:
            errors.append(f"{path}: must be >= {minimum}, got {value}")
        if maximum is not None and isinstance(value, (int, float)) and value > maximum:
            errors.append(f"{path}: must be <= {maximum}, got {value}")

    # ── scalar top-level ──
    if "package" in data:
        check("package", data["package"], kinds=(str,))
        if not str(data["package"]).strip():
            errors.append("package: must not be empty")
    if "device" in data and data["device"] is not None:
        check("device", data["device"], kinds=(str,))
    if "profile" in data:
        check("profile", data["profile"], kinds=(str,))
        if data["profile"] not in ("smoke", "soak", "overnight", "automotive"):
            errors.append(
                "profile: must be smoke | soak | overnight | automotive"
            )

    # ── nested sections ──
    for section in ALLOWED_KEYS - {"package", "device", "profile"}:
        if section not in data:
            continue
        value = data[section]
        if not isinstance(value, dict):
            errors.append(f"{section}: must be a mapping")
            continue
        allowed = ALLOWED_NESTED.get(section, set())
        for key in value:
            if key not in allowed:
                errors.append(
                    f"unknown field '{section}.{key}' "
                    f"(allowed: {sorted(allowed)})"
                )

    # ── discovery ──
    discovery = data.get("discovery") or {}
    if isinstance(discovery, dict):
        if "wait_timeout_sec" in discovery:
            check("discovery.wait_timeout_sec", discovery["wait_timeout_sec"], minimum=0)
        if "rescan_interval_sec" in discovery:
            check("discovery.rescan_interval_sec", discovery["rescan_interval_sec"], minimum=0.01)
        if "process_filter" in discovery:
            pf = discovery["process_filter"]
            if not isinstance(pf, list) or not all(isinstance(x, str) for x in pf):
                errors.append("discovery.process_filter: must be a list of strings")

    # ── collectors.logcat ──
    collectors = data.get("collectors") or {}
    if isinstance(collectors, dict):
        logcat = collectors.get("logcat") or {}
        if isinstance(logcat, dict):
            if "enabled" in logcat:
                check("collectors.logcat.enabled", logcat["enabled"], kinds=(bool,))
            if "buffers" in logcat:
                buffers = logcat["buffers"]
                if not isinstance(buffers, list) or not buffers:
                    errors.append("collectors.logcat.buffers: must be a non-empty list")
                elif not all(isinstance(b, str) and b.strip() for b in buffers):
                    errors.append(
                        "collectors.logcat.buffers: all entries must be "
                        "non-empty strings"
                    )
            if "reconnect_backoff_sec" in logcat:
                check("collectors.logcat.reconnect_backoff_sec",
                      logcat["reconnect_backoff_sec"], minimum=0.01)
        device_health = collectors.get("device_health") or {}
        if isinstance(device_health, dict):
            if "interval_sec" in device_health:
                check("collectors.device_health.interval_sec",
                      device_health["interval_sec"], minimum=0.1)
            if "reboot_policy" in device_health:
                policy = device_health["reboot_policy"]
                check("collectors.device_health.reboot_policy",
                      policy, kinds=(str,))
                if policy not in ("continue", "fail-fast", "wait-and-resume"):
                    errors.append(
                        "collectors.device_health.reboot_policy: must be "
                        "continue | fail-fast | wait-and-resume"
                    )
        resource_risk = collectors.get("resource_risk") or {}
        if isinstance(resource_risk, dict):
            if "enabled" in resource_risk:
                check("collectors.resource_risk.enabled",
                      resource_risk["enabled"], kinds=(bool,))
            if "interval_sec" in resource_risk:
                check("collectors.resource_risk.interval_sec",
                      resource_risk["interval_sec"], minimum=1)
            for k in ("fd_growth_threshold", "thread_growth_threshold"):
                if k in resource_risk:
                    check(f"collectors.resource_risk.{k}",
                          resource_risk[k], minimum=1, kinds=(int,))

    # ── detection ──
    detection = data.get("detection") or {}
    if isinstance(detection, dict):
        for k in ("enable_java_crash", "enable_native_crash", "enable_anr",
                  "enable_process_death"):
            if k in detection:
                check(f"detection.{k}", detection[k], kinds=(bool,))
        if "dedup_window_sec" in detection:
            check("detection.dedup_window_sec", detection["dedup_window_sec"], minimum=0)

    # ── dumps ──
    dumps = data.get("dumps") or {}
    if isinstance(dumps, dict):
        if "pre_context_sec" in dumps:
            check("dumps.pre_context_sec", dumps["pre_context_sec"], minimum=0)
        if "post_context_sec" in dumps:
            check("dumps.post_context_sec", dumps["post_context_sec"], minimum=0)
        if "max_incidents_per_type" in dumps:
            check("dumps.max_incidents_per_type", dumps["max_incidents_per_type"],
                  minimum=0, kinds=(int,))
        if "dump_shutdown_timeout_sec" in dumps:
            check("dumps.dump_shutdown_timeout_sec", dumps["dump_shutdown_timeout_sec"],
                  minimum=0.01)
        if "context_retention_sec" in dumps and dumps["context_retention_sec"] is not None:
            check("dumps.context_retention_sec", dumps["context_retention_sec"], minimum=1)
        if "context_buffer_max_lines" in dumps:
            check("dumps.context_buffer_max_lines", dumps["context_buffer_max_lines"],
                  minimum=1, kinds=(int,))
        if "context_buffer_max_bytes" in dumps:
            check("dumps.context_buffer_max_bytes", dumps["context_buffer_max_bytes"],
                  minimum=1, kinds=(int,))
        for k in ("pull_tombstone", "pull_anr_trace"):
            if k in dumps:
                check(f"dumps.{k}", dumps[k], kinds=(bool,))

    # ── health ──
    health = data.get("health") or {}
    if isinstance(health, dict) and "min_coverage_ratio" in health:
        check("health.min_coverage_ratio", health["min_coverage_ratio"],
              minimum=0.0, maximum=1.0)

    # ── diagnosis ──
    diagnosis = data.get("diagnosis") or {}
    if isinstance(diagnosis, dict):
        for k in ("mapping_file", "retrace_command", "native_symbols_dir",
                  "llvm_symbolizer_path"):
            if k in diagnosis and diagnosis[k] is not None:
                check(f"diagnosis.{k}", diagnosis[k], kinds=(str,))
                if not str(diagnosis[k]).strip():
                    errors.append(f"diagnosis.{k}: must not be empty")

    # ── policy ──
    policy = data.get("policy") or {}
    if isinstance(policy, dict):
        if "fail_on" in policy:
            pf = policy["fail_on"]
            if not isinstance(pf, list) or not all(isinstance(x, str) for x in pf):
                errors.append("policy.fail_on: must be a list of strings")
        for k in ("max_process_death", "max_anr", "max_restarts"):
            if k in policy:
                check(f"policy.{k}", policy[k], minimum=0, kinds=(int,))
        if "min_uptime_ratio" in policy:
            check("policy.min_uptime_ratio", policy["min_uptime_ratio"],
                  minimum=0.0, maximum=1.0)
        if "min_coverage_ratio" in policy:
            check("policy.min_coverage_ratio", policy["min_coverage_ratio"],
                  minimum=0.0, maximum=1.0)
        if "fail_on_new_regression_only" in policy:
            check("policy.fail_on_new_regression_only",
                  policy["fail_on_new_regression_only"], kinds=(bool,))

    # ── quota ──
    quota = data.get("quota") or {}
    if isinstance(quota, dict):
        for k in ("max_disk_bytes", "max_log_file_bytes"):
            if k in quota and quota[k] is not None:
                check(f"quota.{k}", quota[k], minimum=1, kinds=(int,))
        for k in ("log_retention_hours", "max_queue_size",
                  "evidence_sample_every_n"):
            if k in quota:
                check(f"quota.{k}", quota[k], minimum=1, kinds=(int,))
        if "self_monitor_interval_sec" in quota:
            check("quota.self_monitor_interval_sec",
                  quota["self_monitor_interval_sec"], minimum=1)

    # ── redaction ──
    redaction = data.get("redaction") or {}
    if isinstance(redaction, dict):
        if "enabled" in redaction:
            check("redaction.enabled", redaction["enabled"], kinds=(bool,))
        if "regexes" in redaction:
            rx = redaction["regexes"]
            if not isinstance(rx, list) or not all(isinstance(x, str) for x in rx):
                errors.append("redaction.regexes: must be a list of strings")
            else:
                for i, raw in enumerate(rx):
                    try:
                        __import__("re").compile(raw)
                    except Exception:
                        errors.append(f"redaction.regexes[{i}]: invalid regex")

    # ── webhook ──
    webhook = data.get("webhook") or {}
    if isinstance(webhook, dict):
        if "url" in webhook and webhook["url"] is not None:
            check("webhook.url", webhook["url"], kinds=(str,))
            if not str(webhook["url"]).startswith(("http://", "https://")):
                errors.append("webhook.url: must start with http:// or https://")
        if "events" in webhook:
            ev = webhook["events"]
            if not isinstance(ev, list) or not all(isinstance(x, str) for x in ev):
                errors.append("webhook.events: must be a list of strings")
        if "rate_limit_sec" in webhook:
            check("webhook.rate_limit_sec", webhook["rate_limit_sec"], minimum=0)

    # ── plugins ──
    plugins = data.get("plugins") or {}
    if isinstance(plugins, dict) and "enabled" in plugins:
        check("plugins.enabled", plugins["enabled"], kinds=(bool,))

    # ── output ──
    output = data.get("output") or {}
    if isinstance(output, dict):
        if "emit_html" in output:
            check("output.emit_html", output["emit_html"], kinds=(bool,))
        if "status_interval_sec" in output:
            check("output.status_interval_sec", output["status_interval_sec"], minimum=0.01)
        if "dashboard" in output:
            check("output.dashboard", output["dashboard"], kinds=(bool,))

    if lenient:
        errors = [e for e in errors if not e.startswith("unknown field")]
    return errors
