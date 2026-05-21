"""CLI entry-point. A thin shell around the PerfTest library API that adds
duration timing and exit-code translation."""

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

from .api import PerfConfig, PerfTest
from .device import DeviceSetupError

log = logging.getLogger("perf_auto_test")

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_SETUP = 2
EXIT_WAIT_TIMEOUT = 3
EXIT_PROCESS_DIED = 4
EXIT_SIGINT = 130


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Config building
# ---------------------------------------------------------------------------
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
    """Flatten the nested YAML schema documented in the plan into the flat
    PerfConfig field names. Unknown keys pass through (will be rejected later
    by PerfConfig if invalid)."""
    out: Dict[str, Any] = {}

    # Top-level scalars (package, device)
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

    sampling = data.get("sampling", {}) or {}
    if "cpu_interval_sec" in sampling:
        out["cpu_interval_sec"] = sampling["cpu_interval_sec"]
    if "mem_interval_sec" in sampling:
        out["mem_interval_sec"] = sampling["mem_interval_sec"]

    thresholds = data.get("thresholds", {}) or {}
    cpu_thr = thresholds.get("cpu", {}) or {}
    if "percent" in cpu_thr:
        out["cpu_threshold_percent"] = cpu_thr["percent"]
    if "sustain_sec" in cpu_thr:
        out["cpu_sustain_sec"] = cpu_thr["sustain_sec"]
    if "cooldown_sec" in cpu_thr:
        out["cpu_cooldown_sec"] = cpu_thr["cooldown_sec"]
    mem_thr = thresholds.get("mem", {}) or {}
    if "pss_mb" in mem_thr:
        out["mem_threshold_pss_mb"] = mem_thr["pss_mb"]
    if "sustain_sec" in mem_thr:
        out["mem_sustain_sec"] = mem_thr["sustain_sec"]
    if "cooldown_sec" in mem_thr:
        out["mem_cooldown_sec"] = mem_thr["cooldown_sec"]

    dumps = data.get("dumps", {}) or {}
    if "enable_heap" in dumps:
        out["enable_heap_dumps"] = dumps["enable_heap"]
    if "max_heap_dumps" in dumps:
        out["max_heap_dumps"] = dumps["max_heap_dumps"]
    if "max_thread_dumps" in dumps:
        out["max_cpu_dumps"] = dumps["max_thread_dumps"]

    output = data.get("output", {}) or {}
    if "emit_html" in output:
        out["emit_html"] = output["emit_html"]
    if "status_interval_sec" in output:
        out["status_interval_sec"] = output["status_interval_sec"]

    return out


def build_config(args: argparse.Namespace, yaml_path: Optional[Path]) -> PerfConfig:
    """Merge YAML config (if any) with CLI args. CLI overrides YAML."""
    cfg_kwargs = _load_yaml(yaml_path)

    # CLI args that map to PerfConfig fields, with sentinel detection for
    # "user didn't pass this flag" so YAML defaults are preserved.
    cli_map = {
        "package": args.package,
        "output_dir": args.output,
        "device": args.device,
        "wait_timeout_sec": args.wait_timeout,
        "cpu_interval_sec": args.cpu_interval,
        "mem_interval_sec": args.mem_interval,
        "rescan_interval_sec": args.rescan_interval,
        "process_filter": _parse_csv_list(args.processes),
        "cpu_threshold_percent": args.cpu_threshold_percent,
        "cpu_sustain_sec": args.cpu_sustain_sec,
        "cpu_cooldown_sec": args.cpu_cooldown_sec,
        "mem_threshold_pss_mb": args.mem_threshold_pss_mb,
        "mem_sustain_sec": args.mem_sustain_sec,
        "mem_cooldown_sec": args.mem_cooldown_sec,
        "enable_heap_dumps": not args.no_heap_dumps,
        "emit_html": not args.no_html,
        "status_interval_sec": args.status_interval,
    }
    for k, v in cli_map.items():
        if v is None:
            continue
        if isinstance(v, bool):
            # Only override YAML bool if the user explicitly flipped the flag.
            # For these flags the default in argparse already matches PerfConfig,
            # so it's safe to always set.
            cfg_kwargs[k] = v
        else:
            cfg_kwargs[k] = v

    if "package" not in cfg_kwargs:
        raise ValueError("--package is required (or set 'package' in --config YAML)")
    if "output_dir" not in cfg_kwargs:
        cfg_kwargs["output_dir"] = _default_output(cfg_kwargs["package"])

    return PerfConfig(**cfg_kwargs)


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="perf_auto_test",
        description="Generic Android APK performance auto-test (CPU+Mem, "
                    "threshold-based dumps, AI-friendly reports).",
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

    # Sampling
    p.add_argument("--wait-timeout", type=float, default=None,
                   help="Seconds to wait for target process to appear (default: 60)")
    p.add_argument("--cpu-interval", type=float, default=None,
                   help="CPU sampling interval seconds (default: 1.0)")
    p.add_argument("--mem-interval", type=float, default=None,
                   help="Memory sampling interval seconds (default: 5.0)")
    p.add_argument("--rescan-interval", type=float, default=None,
                   help="Process re-discovery interval seconds (default: 5.0)")
    p.add_argument("--processes", default=None,
                   help="Comma-separated filter (e.g. ':remote,:push'). Empty = all.")

    # Thresholds
    p.add_argument("--cpu-threshold-percent", type=float, default=None,
                   help="CPU%% trip value (default: 80)")
    p.add_argument("--cpu-sustain-sec", type=float, default=None,
                   help="Seconds CPU must stay above threshold to fire (default: 60)")
    p.add_argument("--cpu-cooldown-sec", type=float, default=None,
                   help="Cooldown after a CPU dump fires (default: 300)")
    p.add_argument("--mem-threshold-pss-mb", type=float, default=None,
                   help="Memory PSS MB trip value (default: 500)")
    p.add_argument("--mem-sustain-sec", type=float, default=None,
                   help="Seconds Mem must stay above threshold to fire (default: 120)")
    p.add_argument("--mem-cooldown-sec", type=float, default=None,
                   help="Cooldown after a Mem dump fires (default: 600)")

    # Dumps
    p.add_argument("--no-heap-dumps", action="store_true",
                   help="Disable `am dumpheap` (still captures dumpsys meminfo -d)")
    p.add_argument("--status-interval", type=float, default=None,
                   help="status.json heartbeat interval seconds (default: 10)")

    # Output
    p.add_argument("--no-html", action="store_true", help="Skip report.html")

    # Logging
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-json", action="store_true",
                   help="Emit logs as JSON lines to stderr (for log aggregators)")

    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.quiet, args.log_json, args.verbose)

    try:
        cfg = build_config(args, Path(args.config) if args.config else None)
    except (ValueError, FileNotFoundError) as e:
        log.error("config error: %s", e)
        return EXIT_SETUP

    log.info("perf_auto_test starting; package=%s duration=%.0fs out=%s",
             cfg.package, args.duration, cfg.output_dir)

    perf: Optional[PerfTest] = None
    interrupted = False
    try:
        perf = PerfTest(cfg)
        perf.start()
        deadline = time.monotonic() + args.duration
        try:
            while time.monotonic() < deadline:
                time.sleep(0.5)
        except KeyboardInterrupt:
            interrupted = True
            log.info("interrupted; stopping pool")
    except DeviceSetupError as e:
        log.error("preflight failed: %s", e)
        if perf is not None and getattr(perf, "_stopped", False):
            log.info("partial report written to %s", cfg.output_dir)
        return EXIT_SETUP
    except TimeoutError as e:
        log.error("%s", e)
        return EXIT_WAIT_TIMEOUT
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if perf is not None and not getattr(perf, "_stopped", False):
            try:
                perf.stop()
            except Exception:
                log.exception("error during stop()")

    if perf is None or perf._result is None:
        return EXIT_SIGINT if interrupted else EXIT_SETUP

    if interrupted:
        return EXIT_SIGINT
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
