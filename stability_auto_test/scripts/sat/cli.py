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

from .api import StabilityConfig, StabilityTest
from .device import DeviceSetupError

log = logging.getLogger("stability_auto_test")

EXIT_OK = 0
EXIT_SETUP = 2
EXIT_WAIT_TIMEOUT = 3
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
    return _flatten_yaml(data)


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
    detection = data.get("detection", {}) or {}
    for k in ("enable_java_crash", "enable_native_crash",
              "enable_anr", "enable_process_death"):
        if k in detection:
            out[k] = detection[k]
    if "dedup_window_sec" in detection:
        out["dedup_window_sec"] = detection["dedup_window_sec"]

    dumps = data.get("dumps", {}) or {}
    for k in ("pre_context_sec", "post_context_sec",
              "max_incidents_per_type", "pull_tombstone", "pull_anr_trace"):
        if k in dumps:
            out[k] = dumps[k]

    output = data.get("output", {}) or {}
    if "emit_html" in output:
        out["emit_html"] = output["emit_html"]
    if "status_interval_sec" in output:
        out["status_interval_sec"] = output["status_interval_sec"]

    return out


def build_config(args: argparse.Namespace, yaml_path: Optional[Path]) -> StabilityConfig:
    cfg_kwargs = _load_yaml(yaml_path)

    cli_map = {
        "package": args.package,
        "output_dir": args.output,
        "device": args.device,
        "wait_timeout_sec": args.wait_timeout,
        "rescan_interval_sec": args.rescan_interval,
        "process_filter": _parse_csv_list(args.processes),
        "dedup_window_sec": args.dedup_window,
        "max_incidents_per_type": args.max_incidents_per_type,
        "emit_html": not args.no_html,
        "status_interval_sec": args.status_interval,
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

    if "package" not in cfg_kwargs:
        raise ValueError("--package is required (or set 'package' in --config YAML)")
    if "output_dir" not in cfg_kwargs:
        cfg_kwargs["output_dir"] = _default_output(cfg_kwargs["package"])

    return StabilityConfig(**cfg_kwargs)


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
    p.add_argument("--duration", type=_parse_duration, default=_parse_duration("5m"),
                   help="Run duration, e.g. 30s, 5m, 1h, 24h (default: 5m)")
    p.add_argument("--device", default=None,
                   help="ADB serial (required if multiple devices)")
    p.add_argument("--config", default=None,
                   help="Path to YAML config file (CLI flags override its values)")

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
    p.add_argument("--no-tombstone-pull", action="store_true",
                   help="Skip pulling /data/tombstones/ for native crashes")
    p.add_argument("--no-anr-trace-pull", action="store_true",
                   help="Skip pulling /data/anr/ for ANRs")

    # Output
    p.add_argument("--no-html", action="store_true", help="Skip report.html")
    p.add_argument("--status-interval", type=float, default=None,
                   help="status.json heartbeat interval seconds (default: 10)")

    # Logging
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-json", action="store_true",
                   help="Emit logs as JSON lines to stderr")

    return p


# ── Main ──────────────────────────────────────────────────────────────────────
def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.quiet, args.log_json, args.verbose)

    try:
        cfg = build_config(args, Path(args.config) if args.config else None)
    except (ValueError, FileNotFoundError) as e:
        log.error("config error: %s", e)
        return EXIT_SETUP

    log.info("stability_auto_test starting; package=%s duration=%.0fs out=%s",
             cfg.package, args.duration, cfg.output_dir)

    stab: Optional[StabilityTest] = None
    interrupted = False
    try:
        stab = StabilityTest(cfg)
        stab.start()
        # Use wall-clock time for the deadline so that OS sleep (which
        # suspends time.monotonic() on macOS) does not silently extend the
        # run past the user-specified duration.
        deadline = time.time() + args.duration
        try:
            while time.time() < deadline:
                time.sleep(0.5)
        except KeyboardInterrupt:
            interrupted = True
            log.info("interrupted; stopping pool")
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
                stab.stop()
            except Exception:
                log.exception("error during stop()")

    if stab is None or stab._result is None:
        return EXIT_SIGINT if interrupted else EXIT_SETUP

    if interrupted:
        return EXIT_SIGINT
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
