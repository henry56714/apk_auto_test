"""Library API — StabilityConfig + StabilityTest context manager.

Usage:
    cfg = StabilityConfig(package="com.example.app", output_dir="./stab-out")
    with StabilityTest(cfg) as t:
        run_scenario_a()
        t.bookmark("scenario_a_done")
    print(t.result["run"]["exit_code"])

StabilityTest is the same plumbing the CLI uses. The CLI is a thin wrapper
that adds duration timing + exit-code translation on top.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .adb import Adb, AdbError
from .bookmark import BookmarkWriter
from .device import DeviceInfo, DeviceSetupError, preflight
from .discovery import wait_for_processes
from .health import compute_collector_health
from .live import LiveServer
from .metadata import collect_app_metadata
from .plugins.registry import PluginRunner, discover_plugins, load_plugin
from .pool import (
    CollectorPool,
    CollectorsConfig,
    DetectionConfig,
    DiagnosisConfig,
    DumpsConfig,
)
from .redaction import Redactor
from .reporter import html as html_renderer
from .reporter import result as result_builder
from .runlock import RunLock, RunLockError
from .status import StatusWriter
from .storage import (
    EVENTS_COLUMNS,
    EVENTS_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    CsvStreamWriter,
    LogStreamWriter,
)
from .webhook import WebhookNotifier
from .workloads.base import Workload, WorkloadResult
from .workloads.runner import MANIFEST_FILENAME, WorkloadRunner

log = logging.getLogger(__name__)

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_WAIT_TIMEOUT_SEC: float = 60.0
DEFAULT_RESCAN_INTERVAL_SEC: float = 5.0
DEFAULT_LOGCAT_RECONNECT_BACKOFF_SEC: float = 2.0
DEFAULT_DEDUP_WINDOW_SEC: float = 5.0
DEFAULT_PRE_CONTEXT_SEC: float = 30.0
DEFAULT_POST_CONTEXT_SEC: float = 10.0
DEFAULT_MAX_INCIDENTS_PER_TYPE: int = 200
DEFAULT_MAX_CONCURRENT_DUMPS: int = 2
DEFAULT_DUMP_SHUTDOWN_TIMEOUT_SEC: float = 60.0
DEFAULT_CONTEXT_BUFFER_MAX_LINES: int = 5000
DEFAULT_CONTEXT_BUFFER_MAX_BYTES: int = 4 * 1024 * 1024
DEFAULT_MIN_COVERAGE_RATIO: float = 0.99
DEFAULT_EMIT_HTML: bool = True
DEFAULT_STATUS_INTERVAL_SEC: float = 10.0
DEFAULT_LOGCAT_BUFFERS: Sequence[str] = ("main", "system", "events", "crash")


@dataclass
class StabilityConfig:
    package: str
    output_dir: Path

    device: Optional[str] = None
    wait_timeout_sec: float = DEFAULT_WAIT_TIMEOUT_SEC
    rescan_interval_sec: float = DEFAULT_RESCAN_INTERVAL_SEC
    process_filter: Optional[List[str]] = None

    logcat_enabled: bool = True
    logcat_buffers: List[str] = field(default_factory=lambda: list(DEFAULT_LOGCAT_BUFFERS))
    logcat_reconnect_backoff_sec: float = DEFAULT_LOGCAT_RECONNECT_BACKOFF_SEC
    device_health_interval_sec: float = 5.0
    device_reboot_policy: str = "wait-and-resume"
    resource_risk_enabled: bool = True
    resource_risk_interval_sec: float = 30.0
    resource_fd_growth_threshold: int = 200
    resource_thread_growth_threshold: int = 50
    max_disk_bytes: Optional[int] = None
    max_log_file_bytes: int = 512 * 1024 * 1024
    log_retention_hours: int = 24
    max_queue_size: int = 50
    evidence_sample_every_n: int = 5
    self_monitor_enabled: bool = True
    self_monitor_interval_sec: float = 60.0
    profile_name: Optional[str] = None
    config_sources: Dict = field(default_factory=dict)
    redact: bool = False
    redaction_regexes: List[str] = field(default_factory=list)
    webhook_url: Optional[str] = None
    webhook_events: List[str] = field(
        default_factory=lambda: [
            "on_first_fatal",
            "on_gate_failed",
            "on_device_offline",
            "on_run_complete",
        ],
    )
    webhook_rate_limit_sec: float = 60.0
    plugins_enabled: bool = False

    enable_java_crash: bool = True
    enable_native_crash: bool = True
    enable_anr: bool = True
    enable_process_death: bool = True
    dedup_window_sec: float = DEFAULT_DEDUP_WINDOW_SEC

    pre_context_sec: float = DEFAULT_PRE_CONTEXT_SEC
    post_context_sec: float = DEFAULT_POST_CONTEXT_SEC
    max_incidents_per_type: int = DEFAULT_MAX_INCIDENTS_PER_TYPE
    max_concurrent_dumps: int = DEFAULT_MAX_CONCURRENT_DUMPS
    dump_shutdown_timeout_sec: float = DEFAULT_DUMP_SHUTDOWN_TIMEOUT_SEC
    context_retention_sec: Optional[float] = None
    context_buffer_max_lines: int = DEFAULT_CONTEXT_BUFFER_MAX_LINES
    context_buffer_max_bytes: int = DEFAULT_CONTEXT_BUFFER_MAX_BYTES
    min_coverage_ratio: float = DEFAULT_MIN_COVERAGE_RATIO
    mapping_file: Optional[str] = None
    retrace_command: Optional[str] = None
    native_symbols_dir: Optional[str] = None
    llvm_symbolizer_path: Optional[str] = None
    ci_mode: bool = False
    policy_fail_on: List[str] = field(
        default_factory=lambda: ["java_crash", "native_crash", "anr"],
    )
    policy_max_process_death: int = 0
    policy_max_anr: int = 0
    policy_max_restarts: int = 0
    policy_min_uptime_ratio: float = 0.99
    policy_fail_on_new_regression_only: bool = False
    replay_of_run_id: Optional[str] = None
    pull_tombstone: bool = True
    pull_anr_trace: bool = True

    emit_html: bool = DEFAULT_EMIT_HTML
    status_interval_sec: float = DEFAULT_STATUS_INTERVAL_SEC
    dashboard: bool = False

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        if not self.package:
            raise ValueError("StabilityConfig.package is required")
        if self.dedup_window_sec < 0:
            raise ValueError("dedup_window_sec must be >= 0")
        if self.max_incidents_per_type < 0:
            raise ValueError("max_incidents_per_type must be >= 0")
        if self.dump_shutdown_timeout_sec <= 0:
            raise ValueError("dump_shutdown_timeout_sec must be > 0")

    def config_effective(self) -> Dict[str, Any]:
        d = asdict(self)
        d["output_dir"] = str(self.output_dir)
        return d


class StabilityTest:
    def __init__(
        self,
        config: StabilityConfig,
        *,
        adb: Optional[Adb] = None,
        discover_fn: Optional[Any] = None,
        java_crash_dump_fn: Optional[Any] = None,
        native_crash_dump_fn: Optional[Any] = None,
        anr_dump_fn: Optional[Any] = None,
        proc_death_dump_fn: Optional[Any] = None,
    ) -> None:
        self.config = config
        self._adb_override = adb
        self._discover_fn = discover_fn
        self._java_crash_dump_fn = java_crash_dump_fn
        self._native_crash_dump_fn = native_crash_dump_fn
        self._anr_dump_fn = anr_dump_fn
        self._proc_death_dump_fn = proc_death_dump_fn
        self._adb: Optional[Adb] = None
        self._device_info: Optional[DeviceInfo] = None
        self._app_metadata: Dict = {}
        self._events_writer: Optional[CsvStreamWriter] = None
        self._lifecycle_writer: Optional[CsvStreamWriter] = None
        self._logcat_writer: Optional[LogStreamWriter] = None
        self._pool: Optional[CollectorPool] = None
        self._run_lock: Optional[RunLock] = None
        self._run_id = uuid.uuid4().hex
        self._bookmarks: Optional[BookmarkWriter] = None
        self._status: Optional[StatusWriter] = None
        self._live: Optional[LiveServer] = None
        self._started_at: Optional[datetime] = None
        self._ended_at: Optional[datetime] = None
        # Monotonic counters — only tick while the process actually runs, so
        # `duration_sec` in the report reflects script-active time (not wall
        # clock that includes OS sleep / suspend periods).
        self._started_monotonic: Optional[float] = None
        self._ended_monotonic: Optional[float] = None
        self._exit_code: int = 0
        self._exit_reason: str = "duration_elapsed"
        self._result: Optional[Dict] = None
        self._started = False
        self._stopped = False

    @property
    def output_dir(self) -> Path:
        return self.config.output_dir

    @property
    def result(self) -> Dict:
        if self._result is None:
            raise RuntimeError("StabilityTest.result is only available after stop()")
        return self._result

    @property
    def started_at(self) -> Optional[datetime]:
        return self._started_at

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            raise RuntimeError("StabilityTest already started")
        self._started = True
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self._run_lock = RunLock(
            self.config.output_dir,
            run_id=self._run_id,
            device=self.config.device,
        )
        try:
            self._run_lock.acquire()
        except RunLockError as e:
            self._abort("setup_failed", exit_code=2, msg=str(e))
            raise DeviceSetupError(str(e)) from e
        self._bookmarks = BookmarkWriter(self.config.output_dir)
        self._started_at = datetime.now(timezone.utc)
        self._started_monotonic = time.monotonic()
        self._adb = self._adb_override or Adb(serial=self.config.device)

        try:
            self._device_info = preflight(
                self._adb,
                serial=self.config.device,
                package=self.config.package,
            )
        except DeviceSetupError as e:
            self._abort("setup_failed", exit_code=2, msg=str(e))
            raise
        except AdbError as e:
            self._abort("adb_unavailable", exit_code=2, msg=str(e))
            raise DeviceSetupError(str(e)) from e
        try:
            self._app_metadata = collect_app_metadata(self._adb, self.config.package)
        except Exception:
            log.exception("app metadata collection failed")

        procs = wait_for_processes(
            self._adb,
            self.config.package,
            timeout_sec=self.config.wait_timeout_sec,
        )
        if not procs:
            msg = f"no processes for {self.config.package!r} within {self.config.wait_timeout_sec}s"
            self._abort("wait_timeout", exit_code=3, msg=msg)
            raise TimeoutError(msg)

        self._events_writer = CsvStreamWriter(
            self.config.output_dir,
            "events",
            EVENTS_COLUMNS,
            EVENTS_SCHEMA_TAG,
        )
        self._lifecycle_writer = CsvStreamWriter(
            self.config.output_dir,
            "lifecycle",
            LIFECYCLE_COLUMNS,
            LIFECYCLE_SCHEMA_TAG,
        )
        self._logcat_writer = LogStreamWriter(self.config.output_dir)

        detection = DetectionConfig(
            enable_java_crash=self.config.enable_java_crash,
            enable_native_crash=self.config.enable_native_crash,
            enable_anr=self.config.enable_anr,
            enable_process_death=self.config.enable_process_death,
            dedup_window_sec=self.config.dedup_window_sec,
        )
        dumps = DumpsConfig(
            pre_context_sec=self.config.pre_context_sec,
            post_context_sec=self.config.post_context_sec,
            max_incidents_per_type=self.config.max_incidents_per_type,
            max_concurrent=self.config.max_concurrent_dumps,
            dump_shutdown_timeout_sec=self.config.dump_shutdown_timeout_sec,
            context_retention_sec=self.config.context_retention_sec,
            context_buffer_max_lines=self.config.context_buffer_max_lines,
            context_buffer_max_bytes=self.config.context_buffer_max_bytes,
            max_disk_bytes=self.config.max_disk_bytes,
            max_log_file_bytes=self.config.max_log_file_bytes,
            log_retention_hours=self.config.log_retention_hours,
            max_queue_size=self.config.max_queue_size,
            evidence_sample_every_n=self.config.evidence_sample_every_n,
            self_monitor_enabled=self.config.self_monitor_enabled,
            self_monitor_interval_sec=self.config.self_monitor_interval_sec,
            pull_tombstone=self.config.pull_tombstone,
            pull_anr_trace=self.config.pull_anr_trace,
        )
        collectors = CollectorsConfig(
            logcat_enabled=self.config.logcat_enabled,
            logcat_buffers=tuple(self.config.logcat_buffers),
            logcat_reconnect_backoff_sec=self.config.logcat_reconnect_backoff_sec,
            device_health_interval_sec=self.config.device_health_interval_sec,
            device_reboot_policy=self.config.device_reboot_policy,
            resource_risk_enabled=self.config.resource_risk_enabled,
            resource_risk_interval_sec=self.config.resource_risk_interval_sec,
            resource_fd_growth_threshold=self.config.resource_fd_growth_threshold,
            resource_thread_growth_threshold=(self.config.resource_thread_growth_threshold),
        )
        diagnosis = DiagnosisConfig(
            mapping_file=self.config.mapping_file,
            retrace_command=self.config.retrace_command,
            native_symbols_dir=self.config.native_symbols_dir,
            llvm_symbolizer_path=self.config.llvm_symbolizer_path,
        )

        self._pool = CollectorPool(
            self._adb,
            self.config.package,
            events_writer=self._events_writer,
            lifecycle_writer=self._lifecycle_writer,
            logcat_writer=self._logcat_writer,
            rescan_interval_sec=self.config.rescan_interval_sec,
            process_filter=self.config.process_filter,
            detection=detection,
            dumps=dumps,
            collectors=collectors,
            diagnosis=diagnosis,
            incidents_dir=self.config.output_dir / "incidents",
            run_id=self._run_id,
            discover_fn=self._discover_fn,
            java_crash_dump_fn=self._java_crash_dump_fn,
            native_crash_dump_fn=self._native_crash_dump_fn,
            anr_dump_fn=self._anr_dump_fn,
            proc_death_dump_fn=self._proc_death_dump_fn,
        )
        self._pool.start(initial_processes=procs)

        self._status = StatusWriter(
            self.config.output_dir,
            interval_sec=self.config.status_interval_sec,
            query_fn=self._query_status,
        )
        self._status.start()

        if self.config.dashboard:
            self._live = LiveServer(
                host="127.0.0.1",
                port=0,
                status_query=self._query_status,
                stop_callback=self.stop,
                bookmark_callback=self.bookmark,
                output_dir=self.config.output_dir,
            )
            self._live.start()
            log.info("dashboard listening on 127.0.0.1:%s", self._live.bound_port)

    # ------------------------------------------------------------------

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            if self._live is not None:
                self._live.stop()
            if self._status is not None:
                self._status.stop()
            if self._pool is not None:
                self._pool.stop()
                self._pool.close()
            for w in (self._events_writer, self._lifecycle_writer, self._logcat_writer):
                if w is not None:
                    w.close()
        finally:
            self._ended_monotonic = time.monotonic()
            self._ended_at = datetime.now(timezone.utc)
            self._build_and_write_reports()
            if self._run_lock is not None:
                self._run_lock.release()

    def __enter__(self) -> "StabilityTest":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None and self._exit_reason == "duration_elapsed":
            self._exit_reason = "exception"
            self._exit_code = max(self._exit_code, 1)
        self.stop()

    # ------------------------------------------------------------------

    def bookmark(self, label: str, metadata: Optional[Dict] = None) -> None:
        if self._bookmarks is None:
            raise RuntimeError("StabilityTest.bookmark() called before start()")
        self._bookmarks.append(label, metadata)

    def run_workload(self, workload: Workload) -> WorkloadResult:
        """Run a workload inside the test window (bookmarks + manifest)."""
        if self._bookmarks is None:
            raise RuntimeError("StabilityTest.run_workload() called before start()")
        runner = WorkloadRunner(
            workload,
            bookmarks=self._bookmarks,
            manifest_path=self.config.output_dir / MANIFEST_FILENAME,
        )
        result = runner.run()
        if result.status == "failed":
            self.set_exit(1, "workload_failed")
        return result

    def set_exit(self, exit_code: int, exit_reason: str) -> None:
        self._exit_code = int(exit_code)
        self._exit_reason = str(exit_reason)

    def rewrite_reports(self) -> None:
        if not self._stopped:
            raise RuntimeError("rewrite_reports() is only valid after stop()")
        self._build_and_write_reports()

    # ------------------------------------------------------------------

    def _abort(self, exit_reason: str, *, exit_code: int, msg: str) -> None:
        log.error("StabilityTest aborting: %s (%s)", exit_reason, msg)
        self._exit_code = exit_code
        self._exit_reason = exit_reason
        self._stopped = True
        self._ended_monotonic = time.monotonic()
        self._ended_at = datetime.now(timezone.utc)
        try:
            self._build_and_write_reports()
        except Exception:
            log.exception("failed to write reports during abort")
        if self._run_lock is not None:
            self._run_lock.release()

    def _query_status(self) -> Dict:
        if self._pool is None:
            return {"processes": [], "event_counts": {}, "dump_task_states": {}}
        procs = self._pool.current_processes()
        incidents_dir = self.config.output_dir / "incidents"
        incidents_count = 0
        if incidents_dir.exists():
            incidents_count = sum(1 for _ in incidents_dir.glob("*.json"))
        return {
            "run_id": self._run_id,
            "processes": [{"name": p.name, "pid": p.pid} for p in procs],
            "event_counts": self._pool.event_counts(),
            "dump_task_states": self._pool.dump_task_states(),
            "collectors": self._pool.collector_status(),
            "sample_failures": self._pool.sample_failures(),
            "incidents_count": incidents_count,
        }

    def _build_and_write_reports(self) -> None:
        bookmarks: List[Dict] = []
        if self._bookmarks is not None:
            bookmarks = self._bookmarks.read_all()

        device = (
            asdict(self._device_info)
            if self._device_info is not None
            else {
                "serial": self.config.device or "?",
                "android_version": "?",
                "sdk_int": 0,
                "cpu_cores": 0,
            }
        )

        # Prefer monotonic delta for duration_sec — it does not advance
        # while the OS suspends the process, so the reported duration
        # always matches the configured run budget (regardless of wall
        # clock divergence from system sleep).
        active_duration_sec: Optional[float] = None
        if self._started_monotonic is not None and self._ended_monotonic is not None:
            active_duration_sec = max(0.0, self._ended_monotonic - self._started_monotonic)

        sample_failures = self._pool.sample_failures() if self._pool is not None else {}
        task_states = self._pool.dump_task_states() if self._pool is not None else {}
        dropped_by_cap = self._pool.dropped_by_cap_count() if self._pool is not None else 0
        dropped_by_backpressure = (
            self._pool.dropped_by_backpressure_count() if self._pool is not None else 0
        )
        logcat_stats = (
            self._pool.collector_status().get("logcat", {}) if self._pool is not None else {}
        )
        parse_failures = sample_failures.get("logcat", 0)
        adb_call_failures = getattr(self._adb, "failure_count", 0) if self._adb is not None else 0
        health = compute_collector_health(
            logcat_stats=logcat_stats,
            planned_sec=active_duration_sec or 0.0,
            min_coverage_ratio=self.config.min_coverage_ratio,
            logcat_enabled=self.config.logcat_enabled,
            parse_failures=parse_failures,
            adb_call_failures=adb_call_failures,
        )
        collectors = self._pool.collector_status() if self._pool is not None else {}
        exit_info = self._pool.exit_info_records() if self._pool is not None else []
        device_events = self._pool.device_events() if self._pool is not None else []
        resource_risk = self._pool.resource_risk_events() if self._pool is not None else []
        self_resource = self._pool.self_resource_summary() if self._pool is not None else {}
        if collectors.get("logcat"):
            collectors["logcat"]["queue_backlog_peak"] = self._pool.queue_backlog_peak()
        event_pipeline = {
            "detected_count": sum(task_states.values()),
            "persisted_count": task_states.get("persisted", 0),
            "failed_count": task_states.get("failed", 0),
            "timed_out_count": task_states.get("timed_out", 0),
            "dropped_by_cap_count": dropped_by_cap,
            "dropped_by_backpressure_count": dropped_by_backpressure,
        }

        result = result_builder.build(
            output_dir=self.config.output_dir,
            package=self.config.package,
            started_at=self._started_at or datetime.now(timezone.utc),
            ended_at=self._ended_at or datetime.now(timezone.utc),
            device=device,
            config_effective=self.config.config_effective(),
            exit_code=self._exit_code,
            exit_reason=self._exit_reason,
            run_id=self._run_id,
            app_metadata=self._app_metadata,
            bookmarks=bookmarks,
            sample_failures=sample_failures,
            event_pipeline=event_pipeline,
            collector_health={
                "health": health.health,
                "coverage_ratio": health.coverage_ratio,
                "reasons": health.reasons,
            },
            collectors=collectors,
            exit_info=exit_info,
            device_events=device_events,
            resource_risk=resource_risk,
            self_resource=self_resource,
            policy_config={
                "fail_on": list(self.config.policy_fail_on),
                "max_process_death": self.config.policy_max_process_death,
                "max_anr": self.config.policy_max_anr,
                "max_restarts": self.config.policy_max_restarts,
                "min_uptime_ratio": self.config.policy_min_uptime_ratio,
                "min_coverage_ratio": self.config.min_coverage_ratio,
                "fail_on_new_regression_only": (self.config.policy_fail_on_new_regression_only),
            },
            ci_mode=self.config.ci_mode,
            duration_sec=active_duration_sec,
        )
        result_builder.write(result, self.config.output_dir)

        if self.config.emit_html:
            try:
                html_renderer.write(result, self.config.output_dir)
            except Exception:
                log.exception("html render failed")

        if self.config.webhook_url:
            redactor = (
                Redactor.from_config(self.config.redaction_regexes) if self.config.redact else None
            )

            def red(value: str) -> str:
                if redactor is None:
                    return value
                return redactor.redact(value)[0]

            notifier = WebhookNotifier(
                self.config.webhook_url,
                events=list(self.config.webhook_events),
                rate_limit_sec=self.config.webhook_rate_limit_sec,
            )
            fatal = [
                i
                for i in result.get("incidents") or []
                if i.get("type") in ("java_crash", "native_crash", "anr")
            ]
            if fatal:
                notifier.notify(
                    "on_first_fatal",
                    {
                        "summary": red(fatal[0].get("summary", "")),
                        "severity": red(fatal[0].get("severity", "fatal")),
                    },
                )
            if self.config.ci_mode and not result.get("policy", {}).get("passed", True):
                notifier.notify(
                    "on_gate_failed",
                    {
                        "summary": red("stability gate failed"),
                        "severity": "error",
                    },
                )
            if any(
                e.get("event_type") in ("offline", "reboot")
                for e in result.get("device_events") or []
            ):
                notifier.notify(
                    "on_device_offline",
                    {
                        "summary": red("device gap detected"),
                        "severity": "warning",
                    },
                )
            notifier.notify(
                "on_run_complete",
                {
                    "summary": red(f"verdict={result.get('verdict')}"),
                    "severity": "info",
                },
            )
            result["notifications"] = notifier.stats()
            result_builder.write(result, self.config.output_dir)
            if self.config.emit_html:
                html_renderer.write(result, self.config.output_dir)

        plugin_outputs = {}
        if self.config.plugins_enabled:
            runner = PluginRunner()
            for name in discover_plugins(enabled=True):
                cls = load_plugin(name)
                if cls is None:
                    continue
                try:
                    plugin = cls()
                    outputs = list(runner.call(name, plugin.collect, self._adb) or [])
                    plugin_outputs[name] = outputs
                except Exception:
                    runner.health[name] = "failed"
            result["plugins"] = {
                "enabled": True,
                "health": dict(runner.health),
                "outputs": plugin_outputs,
            }
            result_builder.write(result, self.config.output_dir)
            if self.config.emit_html:
                html_renderer.write(result, self.config.output_dir)

        self._result = result
