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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .adb import Adb, AdbError
from .bookmark import BookmarkWriter
from .device import DeviceInfo, DeviceSetupError, preflight
from .discovery import wait_for_processes
from .pool import (
    CollectorPool,
    CollectorsConfig,
    DetectionConfig,
    DumpsConfig,
)
from .reporter import html as html_renderer
from .reporter import result as result_builder
from .status import StatusWriter
from .storage import (
    EVENTS_COLUMNS,
    EVENTS_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    CsvStreamWriter,
    LogStreamWriter,
)

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

    enable_java_crash: bool = True
    enable_native_crash: bool = True
    enable_anr: bool = True
    enable_process_death: bool = True
    dedup_window_sec: float = DEFAULT_DEDUP_WINDOW_SEC

    pre_context_sec: float = DEFAULT_PRE_CONTEXT_SEC
    post_context_sec: float = DEFAULT_POST_CONTEXT_SEC
    max_incidents_per_type: int = DEFAULT_MAX_INCIDENTS_PER_TYPE
    max_concurrent_dumps: int = DEFAULT_MAX_CONCURRENT_DUMPS
    pull_tombstone: bool = True
    pull_anr_trace: bool = True

    emit_html: bool = DEFAULT_EMIT_HTML
    status_interval_sec: float = DEFAULT_STATUS_INTERVAL_SEC

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        if not self.package:
            raise ValueError("StabilityConfig.package is required")

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
    ) -> None:
        self.config = config
        self._adb_override = adb
        self._discover_fn = discover_fn
        self._adb: Optional[Adb] = None
        self._device_info: Optional[DeviceInfo] = None
        self._events_writer: Optional[CsvStreamWriter] = None
        self._lifecycle_writer: Optional[CsvStreamWriter] = None
        self._logcat_writer: Optional[LogStreamWriter] = None
        self._pool: Optional[CollectorPool] = None
        self._bookmarks: Optional[BookmarkWriter] = None
        self._status: Optional[StatusWriter] = None
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
        self._bookmarks = BookmarkWriter(self.config.output_dir)
        self._started_at = datetime.now(timezone.utc)
        self._started_monotonic = time.monotonic()
        self._adb = self._adb_override or Adb(serial=self.config.device)

        try:
            self._device_info = preflight(
                self._adb, serial=self.config.device, package=self.config.package,
            )
        except DeviceSetupError as e:
            self._abort("setup_failed", exit_code=2, msg=str(e))
            raise
        except AdbError as e:
            self._abort("adb_unavailable", exit_code=2, msg=str(e))
            raise DeviceSetupError(str(e)) from e

        procs = wait_for_processes(
            self._adb, self.config.package,
            timeout_sec=self.config.wait_timeout_sec,
        )
        if not procs:
            msg = (f"no processes for {self.config.package!r} within "
                   f"{self.config.wait_timeout_sec}s")
            self._abort("wait_timeout", exit_code=3, msg=msg)
            raise TimeoutError(msg)

        self._events_writer = CsvStreamWriter(
            self.config.output_dir, "events", EVENTS_COLUMNS, EVENTS_SCHEMA_TAG,
        )
        self._lifecycle_writer = CsvStreamWriter(
            self.config.output_dir, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG,
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
            pull_tombstone=self.config.pull_tombstone,
            pull_anr_trace=self.config.pull_anr_trace,
        )
        collectors = CollectorsConfig(
            logcat_enabled=self.config.logcat_enabled,
            logcat_buffers=tuple(self.config.logcat_buffers),
            logcat_reconnect_backoff_sec=self.config.logcat_reconnect_backoff_sec,
        )

        self._pool = CollectorPool(
            self._adb, self.config.package,
            events_writer=self._events_writer,
            lifecycle_writer=self._lifecycle_writer,
            logcat_writer=self._logcat_writer,
            rescan_interval_sec=self.config.rescan_interval_sec,
            process_filter=self.config.process_filter,
            detection=detection,
            dumps=dumps,
            collectors=collectors,
            incidents_dir=self.config.output_dir / "incidents",
            discover_fn=self._discover_fn,
        )
        self._pool.start(initial_processes=procs)

        self._status = StatusWriter(
            self.config.output_dir,
            interval_sec=self.config.status_interval_sec,
            query_fn=self._query_status,
        )
        self._status.start()

    # ------------------------------------------------------------------

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            if self._status is not None:
                self._status.stop()
            if self._pool is not None:
                self._pool.stop()
            for w in (self._events_writer, self._lifecycle_writer, self._logcat_writer):
                if w is not None:
                    w.close()
        finally:
            self._ended_monotonic = time.monotonic()
            self._ended_at = datetime.now(timezone.utc)
            self._build_and_write_reports()

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

    def _query_status(self) -> Dict:
        if self._pool is None:
            return {"processes": [], "event_counts": {}}
        procs = self._pool.current_processes()
        incidents_dir = self.config.output_dir / "incidents"
        incidents_count = 0
        if incidents_dir.exists():
            incidents_count = sum(1 for _ in incidents_dir.glob("*.json"))
        return {
            "processes": [{"name": p.name, "pid": p.pid} for p in procs],
            "event_counts": self._pool.event_counts(),
            "sample_failures": self._pool.sample_failures(),
            "incidents_count": incidents_count,
        }

    def _build_and_write_reports(self) -> None:
        bookmarks: List[Dict] = []
        if self._bookmarks is not None:
            bookmarks = self._bookmarks.read_all()

        device = (asdict(self._device_info) if self._device_info is not None
                  else {"serial": self.config.device or "?",
                        "android_version": "?", "sdk_int": 0, "cpu_cores": 0})

        sample_failures = (self._pool.sample_failures()
                           if self._pool is not None else {})

        # Prefer monotonic delta for duration_sec — it does not advance
        # while the OS suspends the process, so the reported duration
        # always matches the configured run budget (regardless of wall
        # clock divergence from system sleep).
        active_duration_sec: Optional[float] = None
        if self._started_monotonic is not None and self._ended_monotonic is not None:
            active_duration_sec = max(0.0, self._ended_monotonic - self._started_monotonic)

        result = result_builder.build(
            output_dir=self.config.output_dir,
            package=self.config.package,
            started_at=self._started_at or datetime.now(timezone.utc),
            ended_at=self._ended_at or datetime.now(timezone.utc),
            device=device,
            config_effective=self.config.config_effective(),
            exit_code=self._exit_code,
            exit_reason=self._exit_reason,
            bookmarks=bookmarks,
            sample_failures=sample_failures,
            duration_sec=active_duration_sec,
        )
        result_builder.write(result, self.config.output_dir)

        if self.config.emit_html:
            try:
                html_renderer.write(result, self.config.output_dir)
            except Exception:
                log.exception("html render failed")

        self._result = result
