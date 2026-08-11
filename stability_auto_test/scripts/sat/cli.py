"""CLI entry-point. A thin shell around the StabilityTest library API that
adds duration timing and exit-code translation."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .adb import Adb
from .aggregate import aggregate_reports, write_aggregate
from .api import StabilityConfig, StabilityTest
from .compare import (
    COMPARE_FILENAME,
    CompareError,
    compare_reports,
    load_report,
    write_compare,
    write_compare_junit,
)
from .config import validate_config
from .device import DeviceSetupError, list_devices
from .doctor import run_doctor
from .indexer import load_index, scan_reports, trend, write_index, write_trend
from .matrix import run_matrix
from .profiles import apply_profile, profile_duration
from .recovery import recover_report
from .redaction import Redactor, export_bundle, redact_output_dir
from .replay import run_replay, write_replay_manifest
from .runlock import RunLockError
from .workloads.external import ExternalWorkload
from .workloads.launch import LaunchWorkload
from .workloads.monkey import MonkeyWorkload

log = logging.getLogger("stability_auto_test")

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_SETUP = 2
EXIT_WAIT_TIMEOUT = 3
EXIT_INCONCLUSIVE = 4
EXIT_SIGINT = 130


# ── Duration parsing ──────────────────────────────────────────────────────────
def _parse_duration(s: str) -> float:
    m = re.match(r"^\s*(\d+)\s*([smhd]?)\s*$", s)
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration: {s!r}; use 30s, 5m, 1h, 24h")
    n = int(m.group(1))
    unit = m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _default_output(package: str) -> str:
    slug = package.rsplit(".", 1)[-1]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"./reports/{slug}_{ts}"


def _parse_csv_list(s: Optional[str]) -> Optional[List[str]]:
    if s is None:
        return None
    out = [x.strip() for x in s.split(",") if x.strip()]
    return out or None


# ── Logging ───────────────────────────────────────────────────────────────────
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _setup_logging(quiet: bool, log_json: bool, verbose: bool) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(stream=sys.stderr)
    if log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


# ── Config building ───────────────────────────────────────────────────────────
def _load_yaml(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {}
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping in {path}")
    return data


def _flatten_yaml(data: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the nested YAML schema into flat StabilityConfig field names."""
    out: Dict[str, Any] = {}

    for k in ("package", "device"):
        if k in data:
            out[k] = data[k]

    discovery = data.get("discovery", {}) or {}
    if "wait_timeout_sec" in discovery:
        out["wait_timeout_sec"] = discovery["wait_timeout_sec"]
    if "rescan_interval_sec" in discovery:
        out["rescan_interval_sec"] = discovery["rescan_interval_sec"]
    if "process_filter" in discovery:
        out["process_filter"] = discovery["process_filter"]

    collectors = data.get("collectors", {}) or {}
    logcat = collectors.get("logcat", {}) or {}
    if "enabled" in logcat:
        out["logcat_enabled"] = logcat["enabled"]
    if "buffers" in logcat:
        out["logcat_buffers"] = list(logcat["buffers"])
    if "reconnect_backoff_sec" in logcat:
        out["logcat_reconnect_backoff_sec"] = logcat["reconnect_backoff_sec"]
    device_health = collectors.get("device_health", {}) or {}
    if "interval_sec" in device_health:
        out["device_health_interval_sec"] = device_health["interval_sec"]
    if "reboot_policy" in device_health:
        out["device_reboot_policy"] = device_health["reboot_policy"]
    resource_risk = collectors.get("resource_risk", {}) or {}
    if "enabled" in resource_risk:
        out["resource_risk_enabled"] = resource_risk["enabled"]
    if "interval_sec" in resource_risk:
        out["resource_risk_interval_sec"] = resource_risk["interval_sec"]
    if "fd_growth_threshold" in resource_risk:
        out["resource_fd_growth_threshold"] = resource_risk["fd_growth_threshold"]
    if "thread_growth_threshold" in resource_risk:
        out["resource_thread_growth_threshold"] = (
            resource_risk["thread_growth_threshold"]
        )
    detection = data.get("detection", {}) or {}
    for k in ("enable_java_crash", "enable_native_crash",
              "enable_anr", "enable_process_death"):
        if k in detection:
            out[k] = detection[k]
    if "dedup_window_sec" in detection:
        out["dedup_window_sec"] = detection["dedup_window_sec"]

    dumps = data.get("dumps", {}) or {}
    for k in ("pre_context_sec", "post_context_sec",
              "max_incidents_per_type", "dump_shutdown_timeout_sec",
              "context_retention_sec", "context_buffer_max_lines",
              "context_buffer_max_bytes",
              "pull_tombstone", "pull_anr_trace"):
        if k in dumps:
            out[k] = dumps[k]

    health = data.get("health", {}) or {}
    if "min_coverage_ratio" in health:
        out["min_coverage_ratio"] = health["min_coverage_ratio"]

    diagnosis = data.get("diagnosis", {}) or {}
    for k in ("mapping_file", "retrace_command", "native_symbols_dir",
              "llvm_symbolizer_path"):
        if k in diagnosis:
            out[k] = diagnosis[k]

    policy = data.get("policy", {}) or {}
    if "fail_on" in policy:
        out["policy_fail_on"] = list(policy["fail_on"])
    for k in ("max_process_death", "max_anr", "max_restarts",
              "min_uptime_ratio", "fail_on_new_regression_only"):
        if k in policy:
            out["policy_" + k] = policy[k]
    if "min_coverage_ratio" in policy:
        out["min_coverage_ratio"] = policy["min_coverage_ratio"]

    output = data.get("output", {}) or {}
    if "emit_html" in output:
        out["emit_html"] = output["emit_html"]
    if "status_interval_sec" in output:
        out["status_interval_sec"] = output["status_interval_sec"]
    if "dashboard" in output:
        out["dashboard"] = output["dashboard"]

    quota = data.get("quota", {}) or {}
    for k in ("max_disk_bytes", "max_log_file_bytes", "log_retention_hours",
              "max_queue_size", "evidence_sample_every_n",
              "self_monitor_interval_sec"):
        if k in quota:
            out[k] = quota[k]

    redaction = data.get("redaction", {}) or {}
    if "enabled" in redaction:
        out["redact"] = redaction["enabled"]
    if "regexes" in redaction:
        out["redaction_regexes"] = list(redaction["regexes"])

    webhook = data.get("webhook", {}) or {}
    if "url" in webhook:
        out["webhook_url"] = webhook["url"]
    if "events" in webhook:
        out["webhook_events"] = list(webhook["events"])
    if "rate_limit_sec" in webhook:
        out["webhook_rate_limit_sec"] = webhook["rate_limit_sec"]

    plugins = data.get("plugins", {}) or {}
    if "enabled" in plugins:
        out["plugins_enabled"] = plugins["enabled"]

    return out


def build_config(args: argparse.Namespace, yaml_path: Optional[Path]) -> StabilityConfig:
    yaml_data = _load_yaml(yaml_path)
    errors = validate_config(yaml_data, lenient=args.config_lenient)
    if errors:
        raise ValueError("config validation failed: " + "; ".join(errors))
    cfg_kwargs = _flatten_yaml(yaml_data)
    sources = {k: "yaml" for k in cfg_kwargs}

    if args.profile is None and yaml_data.get("profile"):
        args.profile = yaml_data["profile"]
    if args.profile:
        cfg_kwargs, profile_sources = apply_profile(cfg_kwargs, args.profile)
        sources.update(profile_sources)

    cli_map = {
        "package": args.package,
        "output_dir": args.output,
        "device": args.device,
        "wait_timeout_sec": args.wait_timeout,
        "rescan_interval_sec": args.rescan_interval,
        "process_filter": _parse_csv_list(args.processes),
        "dedup_window_sec": args.dedup_window,
        "max_incidents_per_type": args.max_incidents_per_type,
        "dump_shutdown_timeout_sec": args.dump_shutdown_timeout,
        "context_retention_sec": args.context_retention,
        "context_buffer_max_lines": args.context_buffer_max_lines,
        "context_buffer_max_bytes": args.context_buffer_max_bytes,
        "min_coverage_ratio": args.min_coverage,
        "mapping_file": args.mapping_file,
        "retrace_command": args.retrace_command,
        "native_symbols_dir": args.native_symbols_dir,
        "llvm_symbolizer_path": args.llvm_symbolizer,
        "ci_mode": args.ci,
        "policy_fail_on": _parse_csv_list(args.fail_on),
        "policy_max_anr": args.max_anr,
        "policy_max_restarts": args.max_restarts,
        "policy_min_uptime_ratio": args.min_uptime_ratio,
        "device_reboot_policy": args.device_reboot_policy,
        "device_health_interval_sec": args.device_health_interval,
        "resource_risk_enabled": not args.no_resource_risk,
        "resource_risk_interval_sec": args.resource_risk_interval,
        "resource_fd_growth_threshold": args.resource_fd_threshold,
        "resource_thread_growth_threshold": args.resource_thread_threshold,
        "max_disk_bytes": args.max_disk_bytes,
        "max_log_file_bytes": args.max_log_file_bytes,
        "log_retention_hours": args.log_retention_hours,
        "max_queue_size": args.max_queue_size,
        "evidence_sample_every_n": args.evidence_sample_every_n,
        "self_monitor_interval_sec": args.self_monitor_interval,
        "redact": args.redact,
        "redaction_regexes": list(args.redaction_regex or []),
        "webhook_url": args.webhook_url,
        "webhook_events": args.webhook_event,
        "webhook_rate_limit_sec": args.webhook_rate_limit,
        "plugins_enabled": args.enable_plugins,
        "emit_html": not args.no_html,
        "status_interval_sec": args.status_interval,
        "dashboard": args.dashboard,
    }
    # Bool disable flags (only set when user passed them).
    if args.no_java_crash:
        cli_map["enable_java_crash"] = False
    if args.no_native_crash:
        cli_map["enable_native_crash"] = False
    if args.no_anr:
        cli_map["enable_anr"] = False
    if args.no_process_death:
        cli_map["enable_process_death"] = False
    if args.no_tombstone_pull:
        cli_map["pull_tombstone"] = False
    if args.no_anr_trace_pull:
        cli_map["pull_anr_trace"] = False

    for k, v in cli_map.items():
        if v is None:
            continue
        cfg_kwargs[k] = v
        sources[k] = "cli"

    if "package" not in cfg_kwargs:
        raise ValueError("--package is required (or set 'package' in --config YAML)")
    if "output_dir" not in cfg_kwargs:
        cfg_kwargs["output_dir"] = _default_output(cfg_kwargs["package"])

    cfg_kwargs["config_sources"] = sources
    if "profile_name" not in cfg_kwargs:
        cfg_kwargs["profile_name"] = None
    return StabilityConfig(**cfg_kwargs)


def _run_matrix_mode(args: argparse.Namespace, cfg: StabilityConfig) -> int:
    duration_sec = args.duration if args.duration is not None else (
        profile_duration(args.profile) or 300
    )
    if args.devices == "all":
        adb = Adb(serial=args.device)
        try:
            devices = [s for s, st in list_devices(adb) if st == "device"]
        except Exception as e:
            log.error("device enumeration failed: %s", e)
            return EXIT_SETUP
    else:
        devices = [d.strip() for d in (args.devices or "").split(",") if d.strip()]
    if not devices:
        log.error("no devices selected for matrix mode")
        return EXIT_SETUP

    from .matrix import launch_package_on
    for device in devices:
        try:
            launch_package_on(device, cfg.package)
        except Exception:
            log.warning("could not launch %s on %s", cfg.package, device)

    output_root = cfg.output_dir
    results = run_matrix(
        package=cfg.package,
        devices=devices,
        output_root=output_root,
        duration_sec=int(duration_sec),
        max_parallel=2,
        extra_args=["--min-coverage", str(cfg.min_coverage_ratio)],
    )
    reports = []
    for res in results:
        report_path = res.output_dir / "report.json"
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["_report_path"] = str(report_path)
            reports.append(report)
    aggregate = aggregate_reports(reports)
    write_aggregate(aggregate, output_root)
    failed = [r for r in results if r.returncode != 0]
    log.info("matrix complete: %d/%d ok", len(results) - len(failed), len(results))
    return EXIT_GATE_FAILED if failed else EXIT_OK


# ── Argparse ──────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stability_auto_test",
        description="Generic Android APK stability auto-test "
                    "(Java/Native crash + ANR + process death, AI-friendly reports).",
    )
    p.add_argument("--package", default=None,
                   help="Target package (required unless set in --config)")
    p.add_argument("--output", default=None,
                   help="Output directory (default: ./reports/<pkg>_<YYYYMMDD_HHMMSS>)")
    p.add_argument("--duration", type=_parse_duration, default=None,
                   help="Run duration, e.g. 30s, 5m, 1h, 24h "
                        "(default: 5m or profile default)")
    p.add_argument("--device", default=None,
                   help="ADB serial (required if multiple devices)")
    p.add_argument("--devices", default=None,
                   help="Comma-separated device serials or 'all' "
                        "(multi-device matrix mode)")
    p.add_argument("--profile", choices=["smoke", "soak", "overnight", "automotive"],
                   default=None,
                   help="Configuration preset (CLI/YAML still override)")
    p.add_argument("--print-effective-config", action="store_true",
                   help="Print the final effective config and sources, then exit")
    p.add_argument("--config", default=None,
                   help="Path to YAML config file (CLI flags override its values)")
    p.add_argument("--config-lenient", action="store_true",
                   help="Ignore unknown YAML fields (values are still validated)")

    # Discovery
    p.add_argument("--wait-timeout", type=float, default=None,
                   help="Seconds to wait for target process (default: 60)")
    p.add_argument("--rescan-interval", type=float, default=None,
                   help="Process re-discovery interval seconds (default: 5)")
    p.add_argument("--processes", default=None,
                   help="Comma-separated filter (e.g. ':remote,:push'). Empty = all.")

    # Detection
    p.add_argument("--no-java-crash", action="store_true",
                   help="Disable Java crash detection")
    p.add_argument("--no-native-crash", action="store_true",
                   help="Disable native crash detection")
    p.add_argument("--no-anr", action="store_true",
                   help="Disable ANR detection")
    p.add_argument("--no-process-death", action="store_true",
                   help="Disable process death detection")
    p.add_argument("--dedup-window", type=float, default=None,
                   help="Dedup window seconds for same (process,pid,type) (default: 5)")

    # Dumps
    p.add_argument("--max-incidents-per-type", type=int, default=None,
                   help="Cap on incidents written per event type (default: 200)")
    p.add_argument("--dump-shutdown-timeout", type=float, default=None,
                   help="Seconds to wait for in-flight incident dumps during "
                        "stop before marking them timed_out (default: 60)")
    p.add_argument("--context-retention", type=float, default=None,
                   help="Logcat context buffer retention seconds "
                        "(default: pre+post+60)")
    p.add_argument("--context-buffer-max-lines", type=int, default=None,
                   help="Max logcat lines kept in the context ring buffer "
                        "(default: 5000)")
    p.add_argument("--context-buffer-max-bytes", type=int, default=None,
                   help="Max bytes kept in the context ring buffer "
                        "(default: 4194304)")
    p.add_argument("--min-coverage", type=float, default=None,
                   help="Minimum logcat coverage ratio for a confident verdict "
                        "(default: 0.99)")
    p.add_argument("--mapping-file", default=None,
                   help="ProGuard/R8 mapping file for Java deobfuscation")
    p.add_argument("--retrace-command", default=None,
                   help="Retrace tool command (default: built-in mapping parser)")
    p.add_argument("--native-symbols-dir", default=None,
                   help="Directory tree with unstripped .so files")
    p.add_argument("--llvm-symbolizer", default=None,
                   help="Path to llvm-symbolizer for native stack symbolization")
    p.add_argument("--ci", action="store_true",
                   help="Enable CI gate: exit 1 when policy rules fail")
    p.add_argument("--fail-on", default=None,
                   help="Comma-separated event types that fail the gate")
    p.add_argument("--max-anr", type=int, default=None,
                   help="Maximum tolerated ANR count (default: 0)")
    p.add_argument("--max-restarts", type=int, default=None,
                   help="Maximum tolerated process restarts (default: 0)")
    p.add_argument("--min-uptime-ratio", type=float, default=None,
                   help="Minimum per-process uptime ratio (default: 0.99)")
    p.add_argument("--device-reboot-policy",
                   choices=["continue", "fail-fast", "wait-and-resume"],
                   default=None,
                   help="Device reboot/offline policy (default: wait-and-resume)")
    p.add_argument("--device-health-interval", type=float, default=None,
                   help="Device health sampling interval seconds (default: 5)")
    p.add_argument("--no-resource-risk", action="store_true",
                   help="Disable FD/thread resource-risk pre-warning")
    p.add_argument("--resource-risk-interval", type=float, default=None,
                   help="Resource-risk sampling interval seconds (default: 30)")
    p.add_argument("--resource-fd-threshold", type=int, default=None,
                   help="FD growth threshold before a risk event (default: 200)")
    p.add_argument("--resource-thread-threshold", type=int, default=None,
                   help="Thread growth threshold (default: 50)")
    p.add_argument("--max-disk-bytes", type=int, default=None,
                   help="Hard disk free-space quota; stop big evidence when reached")
    p.add_argument("--max-log-file-bytes", type=int, default=None,
                   help="Max bytes per log file before rotation guard (default: 512MiB)")
    p.add_argument("--log-retention-hours", type=int, default=None,
                   help="Delete logcat files older than this (default: 24h)")
    p.add_argument("--max-queue-size", type=int, default=None,
                   help="Max queued dump tasks (default: 50)")
    p.add_argument("--evidence-sample-every-n", type=int, default=None,
                   help="Save full evidence for first and every Nth occurrence "
                        "(default: 5)")
    p.add_argument("--self-monitor-interval", type=float, default=None,
                   help="Tool-self resource sampling interval seconds (default: 60)")
    p.add_argument("--redact", action="store_true",
                   help="Apply built-in privacy redaction to reports/logs")
    p.add_argument("--redaction-regex", action="append", default=None,
                   help="Extra redaction regex (repeatable)")
    p.add_argument("--webhook-url", default=None,
                   help="Generic webhook URL for notifications")
    p.add_argument("--webhook-event", action="append", default=None,
                   help="Webhook event type (repeatable; default: all four)")
    p.add_argument("--webhook-rate-limit", type=float, default=None,
                   help="Minimum seconds between same-event notifications")
    p.add_argument("--enable-plugins", action="store_true",
                   help="Enable installed sat.plugins entry points "
                        "(default: disabled)")
    p.add_argument("--no-tombstone-pull", action="store_true",
                   help="Skip pulling /data/tombstones/ for native crashes")
    p.add_argument("--no-anr-trace-pull", action="store_true",
                   help="Skip pulling /data/anr/ for ANRs")

    # Output
    p.add_argument("--no-html", action="store_true", help="Skip report.html")
    p.add_argument("--status-interval", type=float, default=None,
                   help="status.json heartbeat interval seconds (default: 10)")
    p.add_argument("--dashboard", action="store_true",
                   help="Start a localhost-only live dashboard")
    p.add_argument("--junit", default=None,
                   help="Write JUnit XML to this path (one case per issue group)")

    # Workloads
    p.add_argument("--workload", choices=["launch", "monkey", "external"],
                   default=None,
                   help="Run a workload inside the monitor window "
                        "(default: monitor-only)")
    p.add_argument("--monkey-seed", type=int, default=0,
                   help="Monkey seed (deterministic runs)")
    p.add_argument("--monkey-events", type=int, default=1000,
                   help="Monkey event count")
    p.add_argument("--monkey-throttle", type=int, default=50,
                   help="Monkey throttle milliseconds")
    p.add_argument("--external-cmd", default=None,
                   help="External workload command (e.g. maestro test flow.yaml)")
    p.add_argument("--workload-timeout", type=float, default=300.0,
                   help="External workload timeout seconds")

    # Logging
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-json", action="store_true",
                   help="Emit logs as JSON lines to stderr")

    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    recover = sub.add_parser(
        "recover",
        help="Rebuild report.json from a run's incident journal",
    )
    recover.add_argument("--output", required=True,
                         help="Run output directory to recover")
    doctor = sub.add_parser(
        "doctor",
        help="Read-only environment / device capability self-check",
    )
    doctor.add_argument("--package", required=True,
                        help="Target package to check")
    doctor.add_argument("--device", default=None,
                        help="ADB serial (required if multiple devices)")
    doctor.add_argument("--json", action="store_true",
                        help="Emit the diagnosis as JSON")
    doctor.add_argument("--output-dir", default=None,
                        help="Output directory to check (default: ./reports)")
    compare = sub.add_parser(
        "compare",
        help="Compare two reports by incident fingerprint",
    )
    compare.add_argument("--baseline", required=True,
                         help="Baseline report.json")
    compare.add_argument("--current", required=True,
                         help="Current report.json")
    compare.add_argument("--output", required=True,
                         help="Output directory for compare artifacts")
    compare.add_argument("--fail-on-new-regression", action="store_true",
                         help="Exit 1 when new regressions or worsened issues exist")
    compare.add_argument("--junit", default=None,
                         help="Optional JUnit XML path")
    replay = sub.add_parser(
        "replay",
        help="Replay a previous run from its replay.yaml manifest",
    )
    replay.add_argument("--manifest", required=True,
                        help="Path to replay.yaml")
    replay.add_argument("--output", default=None,
                        help="New output directory (default: sibling replay dir)")
    replay.add_argument("--duration", type=_parse_duration, default="60s",
                        help="Replay duration (default: 60s)")
    export = sub.add_parser(
        "export",
        help="Export a run directory as a shareable bundle",
    )
    export.add_argument("--output", required=True,
                        help="Run output directory")
    export.add_argument("--target", required=True,
                        help="Destination zip path")
    export.add_argument("--redacted", action="store_true",
                        help="Redact sensitive data before bundling")
    index = sub.add_parser(
        "index",
        help="Scan a reports root and build a local index",
    )
    index.add_argument("root", help="Reports root directory")
    trend_cmd = sub.add_parser(
        "trend",
        help="Aggregate trend from indexed reports",
    )
    trend_cmd.add_argument("root", help="Reports root directory")
    trend_cmd.add_argument("--output", default=None,
                           help="Output directory (default: <root>)")
    trend_cmd.add_argument("--by", default="fingerprint",
                           choices=["fingerprint", "type"],
                           help="Trend grouping key")
    return p


# ── Main ──────────────────────────────────────────────────────────────────────
def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.quiet, args.log_json, args.verbose)

    if args.command == "recover":
        try:
            result = recover_report(Path(args.output))
        except (RunLockError, ValueError, FileNotFoundError) as e:
            log.error("recover failed: %s", e)
            return EXIT_SETUP
        log.info("recover complete; verdict=%s incidents=%d",
                 result.get("verdict"), len(result.get("incidents", [])))
        return EXIT_OK

    if args.command == "doctor":
        adb = Adb(serial=args.device)
        try:
            result = run_doctor(
                adb, args.package, device=args.device,
                output_dir=Path(args.output_dir) if args.output_dir else None,
            )
        except DeviceSetupError as e:
            if args.json:
                print(json.dumps(
                    {"ok": False, "error": str(e), "checks": []},
                    indent=2, ensure_ascii=False,
                ))
            else:
                log.error("doctor failed: %s", e)
            return EXIT_SETUP
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            log.info("doctor ok for %s on %s", args.package, result["device"])
            for check in result["checks"]:
                log.info("  [%s] %s: %s",
                         check["status"], check["name"], check["detail"])
        return EXIT_OK

    if args.command == "compare":
        try:
            baseline = load_report(Path(args.baseline))
            current = load_report(Path(args.current))
            result = compare_reports(baseline, current)
            write_compare(result, Path(args.output))
            if args.junit:
                write_compare_junit(result, Path(args.junit))
        except CompareError as e:
            log.error("compare failed: %s", e)
            return EXIT_SETUP
        log.info("compare written: %s", Path(args.output) / COMPARE_FILENAME)
        if args.fail_on_new_regression and (
            result["new_regressions"] or result["worsened"]
        ):
            return EXIT_GATE_FAILED
        return EXIT_OK

    if args.command == "replay":
        manifest = Path(args.manifest)
        out = Path(args.output) if args.output else (
            manifest.parent / f"replay-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        try:
            stab = run_replay(
                manifest, out, adb=Adb(), duration_sec=args.duration,
            )
        except (ValueError, FileNotFoundError, KeyError) as e:
            log.error("replay failed: %s", e)
            return EXIT_SETUP
        log.info("replay complete; report=%s", out / "report.json")
        return EXIT_OK

    if args.command == "export":
        try:
            target = export_bundle(
                Path(args.output), Path(args.target), redacted=args.redacted,
            )
        except (OSError, ValueError) as e:
            log.error("export failed: %s", e)
            return EXIT_SETUP
        log.info("export written: %s", target)
        return EXIT_OK

    if args.command == "index":
        try:
            data = scan_reports(Path(args.root))
            write_index(Path(args.root), data)
        except OSError as e:
            log.error("index failed: %s", e)
            return EXIT_SETUP
        log.info("indexed %d runs (%d errors)",
                 data["run_count"], len(data["errors"]))
        return EXIT_OK

    if args.command == "trend":
        root = Path(args.root)
        data = load_index(root)
        if not data:
            data = scan_reports(root)
            write_index(root, data)
        result = trend(data, by=args.by)
        out = Path(args.output) if args.output else root
        try:
            write_trend(result, out)
        except OSError as e:
            log.error("trend failed: %s", e)
            return EXIT_SETUP
        return EXIT_OK

    try:
        cfg = build_config(args, Path(args.config) if args.config else None)
    except (ValueError, FileNotFoundError) as e:
        log.error("config error: %s", e)
        return EXIT_SETUP

    if args.devices:
        return _run_matrix_mode(args, cfg)

    if args.print_effective_config:
        print(json.dumps(cfg.config_effective(), indent=2, ensure_ascii=False))
        return EXIT_OK

    effective_duration = args.duration if args.duration is not None else (
        profile_duration(args.profile) or 300
    )

    log.info("stability_auto_test starting; package=%s duration=%.0fs out=%s",
             cfg.package, effective_duration, cfg.output_dir)

    stab: Optional[StabilityTest] = None
    interrupted = False
    try:
        stab = StabilityTest(cfg)
        if args.workload == "launch" and args.device:
            from .matrix import launch_package_on
            try:
                launch_package_on(args.device, cfg.package)
            except Exception:
                log.warning("pre-launch failed; monitor will still wait for process")
        stab.start()
        workload = _build_workload(args, stab)
        if workload is not None:
            stab.run_workload(workload)
        # Use wall-clock time for the deadline so that OS sleep (which
        # suspends time.monotonic() on macOS) does not silently extend the
        # run past the user-specified duration.
        deadline = time.time() + effective_duration
        try:
            while time.time() < deadline:
                time.sleep(0.5)
        except KeyboardInterrupt:
            interrupted = True
            log.info("interrupted; stopping pool")
            stab.set_exit(EXIT_SIGINT, "interrupted")
    except DeviceSetupError as e:
        log.error("preflight failed: %s", e)
        if stab is not None and getattr(stab, "_stopped", False):
            log.info("partial report written to %s", cfg.output_dir)
        return EXIT_SETUP
    except TimeoutError as e:
        log.error("%s", e)
        return EXIT_WAIT_TIMEOUT
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if stab is not None and not getattr(stab, "_stopped", False):
            try:
                if interrupted:
                    stab.set_exit(EXIT_SIGINT, "interrupted")
                stab.stop()
                if getattr(stab, "_result", None) is not None:
                    manifest_path = cfg.output_dir / "workload_manifest.json"
                    workload_manifest = None
                    if manifest_path.exists():
                        workload_manifest = json.loads(manifest_path.read_text())
                    write_replay_manifest(
                        cfg,
                        workload_manifest=workload_manifest,
                        output_dir=cfg.output_dir,
                        run_id=stab._result["run"].get("run_id"),
                    )
            except Exception:
                log.exception("error during stop()")

    if stab is None or stab._result is None:
        return EXIT_SIGINT if interrupted else EXIT_SETUP

    if interrupted:
        return EXIT_SIGINT
    result = stab._result
    if args.junit:
        try:
            from .reporter.junit import write_junit
            junit_result = result
            if cfg.redact:
                junit_result = Redactor.from_config(
                    cfg.redaction_regexes,
                ).redact_dict(result)
            write_junit(junit_result, Path(args.junit))
        except Exception:
            log.exception("junit write failed")
    if cfg.redact:
        try:
            redactor = Redactor.from_config(cfg.redaction_regexes)
            redact_output_dir(cfg.output_dir, redactor)
        except Exception:
            log.exception("redaction failed")
    try:
        from .reporter.github_summary import write_github_summary
        write_github_summary(result)
    except Exception:
        log.exception("github summary write failed")
    if result.get("verdict") == "inconclusive":
        return EXIT_INCONCLUSIVE
    if cfg.ci_mode and result.get("policy", {}).get("passed") is False:
        return EXIT_GATE_FAILED
    return EXIT_OK


def _build_workload(args: argparse.Namespace, stab: StabilityTest):
    if args.workload is None:
        return None
    if args.workload == "launch":
        return LaunchWorkload(stab._adb, stab.config.package, activity=None)
    if args.workload == "monkey":
        return MonkeyWorkload(
            stab._adb,
            stab.config.package,
            seed=args.monkey_seed,
            event_count=args.monkey_events,
            throttle_ms=args.monkey_throttle,
        )
    if args.workload == "external":
        if not args.external_cmd:
            raise ValueError("--external-cmd is required for --workload external")
        return ExternalWorkload(args.external_cmd, timeout_sec=args.workload_timeout)
    raise ValueError(f"unknown workload: {args.workload}")


if __name__ == "__main__":
    sys.exit(main())
