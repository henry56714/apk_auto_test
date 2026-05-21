"""Library API — PerfConfig + PerfTest context manager.

Usage:
    cfg = PerfConfig(package="com.example.app", output_dir="./perf-out")
    with PerfTest(cfg) as t:
        run_scenario_a()
        t.bookmark("scenario_a_done")
        run_scenario_b()
    print(t.result["run"]["exit_code"])

PerfTest is the same plumbing the CLI uses. The CLI is a thin wrapper that
adds duration timing + exit-code translation on top.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adb import Adb, AdbError
from .alerting import ThresholdConfig
from .bookmark import BookmarkWriter
from .device import DeviceInfo, DeviceSetupError, preflight
from .discovery import wait_for_processes
from .pool import CollectorPool, DumpsConfig, ThresholdsBundle
from .reporter import html as html_renderer
from .reporter import result as result_builder
from .status import StatusWriter
from .storage import (
    CPU_COLUMNS,
    CPU_SCHEMA_TAG,
    LIFECYCLE_COLUMNS,
    LIFECYCLE_SCHEMA_TAG,
    MEM_COLUMNS,
    MEM_SCHEMA_TAG,
    CsvStreamWriter,
)

log = logging.getLogger(__name__)

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_WAIT_TIMEOUT_SEC: float = 60.0
DEFAULT_CPU_INTERVAL_SEC: float = 1.0
DEFAULT_MEM_INTERVAL_SEC: float = 5.0
DEFAULT_RESCAN_INTERVAL_SEC: float = 5.0
DEFAULT_CPU_THRESHOLD_PERCENT: float = 80.0
DEFAULT_CPU_SUSTAIN_SEC: float = 60.0
DEFAULT_CPU_COOLDOWN_SEC: float = 300.0
DEFAULT_MEM_THRESHOLD_PSS_MB: float = 500.0
DEFAULT_MEM_SUSTAIN_SEC: float = 120.0
DEFAULT_MEM_COOLDOWN_SEC: float = 600.0
DEFAULT_ENABLE_HEAP_DUMPS: bool = True
DEFAULT_MAX_CPU_DUMPS: int = 50
DEFAULT_MAX_HEAP_DUMPS: int = 20
DEFAULT_MAX_CONCURRENT_DUMPS: int = 2
DEFAULT_EMIT_HTML: bool = True
DEFAULT_STATUS_INTERVAL_SEC: float = 10.0


@dataclass
class PerfConfig:
    package: str
    output_dir: Path

    device: Optional[str] = None
    wait_timeout_sec: float = DEFAULT_WAIT_TIMEOUT_SEC

    cpu_interval_sec: float = DEFAULT_CPU_INTERVAL_SEC
    mem_interval_sec: float = DEFAULT_MEM_INTERVAL_SEC
    rescan_interval_sec: float = DEFAULT_RESCAN_INTERVAL_SEC
    process_filter: Optional[List[str]] = None

    cpu_threshold_percent: float = DEFAULT_CPU_THRESHOLD_PERCENT
    cpu_sustain_sec: float = DEFAULT_CPU_SUSTAIN_SEC
    cpu_cooldown_sec: float = DEFAULT_CPU_COOLDOWN_SEC
    mem_threshold_pss_mb: float = DEFAULT_MEM_THRESHOLD_PSS_MB
    mem_sustain_sec: float = DEFAULT_MEM_SUSTAIN_SEC
    mem_cooldown_sec: float = DEFAULT_MEM_COOLDOWN_SEC

    enable_heap_dumps: bool = DEFAULT_ENABLE_HEAP_DUMPS
    max_cpu_dumps: int = DEFAULT_MAX_CPU_DUMPS
    max_heap_dumps: int = DEFAULT_MAX_HEAP_DUMPS
    max_concurrent_dumps: int = DEFAULT_MAX_CONCURRENT_DUMPS

    emit_html: bool = DEFAULT_EMIT_HTML

    status_interval_sec: float = DEFAULT_STATUS_INTERVAL_SEC

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        if not self.package:
            raise ValueError("PerfConfig.package is required")

    def config_effective(self) -> Dict[str, Any]:
        """A JSON-safe snapshot of the config for the report's run.config_effective."""
        d = asdict(self)
        d["output_dir"] = str(self.output_dir)
        return d


class PerfTest:
    def __init__(
        self,
        config: PerfConfig,
        *,
        adb: Optional[Adb] = None,
        discover_fn: Optional[Any] = None,
    ) -> None:
        """`adb` and `discover_fn` are test hooks; production code passes neither."""
        self.config = config
        self._adb_override = adb
        self._discover_fn = discover_fn
        self._adb: Optional[Adb] = None
        self._device_info: Optional[DeviceInfo] = None
        self._cpu_writer: Optional[CsvStreamWriter] = None
        self._mem_writer: Optional[CsvStreamWriter] = None
        self._lifecycle_writer: Optional[CsvStreamWriter] = None
        self._pool: Optional[CollectorPool] = None
        self._bookmarks: Optional[BookmarkWriter] = None
        self._status: Optional[StatusWriter] = None
        self._started_at: Optional[datetime] = None
        self._ended_at: Optional[datetime] = None
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
            raise RuntimeError("PerfTest.result is only available after stop()")
        return self._result

    @property
    def started_at(self) -> Optional[datetime]:
        return self._started_at

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            raise RuntimeError("PerfTest already started")
        self._started = True
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self._bookmarks = BookmarkWriter(self.config.output_dir)
        self._started_at = datetime.now(timezone.utc)
        self._adb = self._adb_override or Adb(serial=self.config.device)

        # Preflight + wait — failure here aborts start; report is still written.
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

        self._cpu_writer = CsvStreamWriter(
            self.config.output_dir, "cpu", CPU_COLUMNS, CPU_SCHEMA_TAG,
        )
        self._mem_writer = CsvStreamWriter(
            self.config.output_dir, "mem", MEM_COLUMNS, MEM_SCHEMA_TAG,
        )
        self._lifecycle_writer = CsvStreamWriter(
            self.config.output_dir, "lifecycle", LIFECYCLE_COLUMNS, LIFECYCLE_SCHEMA_TAG,
        )

        thresholds = ThresholdsBundle(
            cpu=ThresholdConfig(
                "cpu_pct",
                self.config.cpu_threshold_percent,
                self.config.cpu_sustain_sec,
                self.config.cpu_cooldown_sec,
            ),
            mem=ThresholdConfig(
                "mem_pss_mb",
                self.config.mem_threshold_pss_mb,
                self.config.mem_sustain_sec,
                self.config.mem_cooldown_sec,
            ),
        )
        dumps = DumpsConfig(
            enable_heap=self.config.enable_heap_dumps,
            max_cpu_dumps=self.config.max_cpu_dumps,
            max_heap_dumps=self.config.max_heap_dumps,
            max_concurrent=self.config.max_concurrent_dumps,
        )

        self._pool = CollectorPool(
            self._adb, self.config.package,
            cpu_cores=self._device_info.cpu_cores,
            cpu_writer=self._cpu_writer,
            mem_writer=self._mem_writer,
            lifecycle_writer=self._lifecycle_writer,
            cpu_interval_sec=self.config.cpu_interval_sec,
            mem_interval_sec=self.config.mem_interval_sec,
            rescan_interval_sec=self.config.rescan_interval_sec,
            process_filter=self.config.process_filter,
            thresholds=thresholds,
            dumps=dumps,
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
            for w in (self._cpu_writer, self._mem_writer, self._lifecycle_writer):
                if w is not None:
                    w.close()
        finally:
            self._ended_at = datetime.now(timezone.utc)
            self._build_and_write_reports()

    def __enter__(self) -> "PerfTest":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None and self._exit_reason == "duration_elapsed":
            # The user's `with` block raised; record that fact.
            self._exit_reason = "exception"
            self._exit_code = max(self._exit_code, 1)
        self.stop()

    # ------------------------------------------------------------------

    def bookmark(self, label: str, metadata: Optional[Dict] = None) -> None:
        if self._bookmarks is None:
            raise RuntimeError("PerfTest.bookmark() called before start()")
        self._bookmarks.append(label, metadata)

    def set_exit(self, exit_code: int, exit_reason: str) -> None:
        """Tell the report what to record as the run's terminal state."""
        self._exit_code = int(exit_code)
        self._exit_reason = str(exit_reason)

    def rewrite_reports(self) -> None:
        """Re-emit report.json/summary.md/etc. with the current exit_code
        and exit_reason. Only valid after stop()."""
        if not self._stopped:
            raise RuntimeError("rewrite_reports() is only valid after stop()")
        self._build_and_write_reports()

    # ------------------------------------------------------------------

    def _abort(self, exit_reason: str, *, exit_code: int, msg: str) -> None:
        log.error("PerfTest aborting: %s (%s)", exit_reason, msg)
        self._exit_code = exit_code
        self._exit_reason = exit_reason
        self._stopped = True
        self._ended_at = datetime.now(timezone.utc)
        # Still build a (minimal) report so the caller can see what happened.
        try:
            self._build_and_write_reports()
        except Exception:
            log.exception("failed to write reports during abort")

    def _query_status(self) -> Dict:
        if self._pool is None:
            return {"processes": [], "dump_counts": {"cpu": 0, "mem": 0}}
        procs = self._pool.current_processes()
        incidents_dir = self.config.output_dir / "incidents"
        incidents_count = 0
        if incidents_dir.exists():
            incidents_count = sum(
                1 for p in incidents_dir.glob("*.json")
                if not p.name.endswith(".meminfo.json")
            )
        return {
            "processes": [{"name": p.name, "pid": p.pid} for p in procs],
            "dump_counts": self._pool.dump_counts(),
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
        )
        result_builder.write(result, self.config.output_dir)

        if self.config.emit_html:
            try:
                html_renderer.write(result, self.config.output_dir)
            except Exception:
                log.exception("html render failed")

        self._result = result
