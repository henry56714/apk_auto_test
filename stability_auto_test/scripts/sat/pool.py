"""Collector pool: 2 long-lived pipelines + dispatcher.

1. logcat thread  — reads `adb logcat` stream, parses lines into events
   (java_crash, native_crash, ANR, process_death via am_proc_died/am_kill),
   writes raw lines to the rotating log file, dispatches events.
2. watcher thread — discovers processes for the target package on a 5 s
   reconcile cadence; writes lifecycle rows (new/restart/gone) to the CSV
   but does NOT dispatch stability events (process_death is detected via the
   am_proc_died / am_kill entries in the logcat events buffer).

Dispatch path: event → Deduper → fire_dump(event)
fire_dump submits a bounded task to a ThreadPoolExecutor. The pool tracks every
task through `queued -> running -> persisted|failed|timed_out` so `stop()`
can drain in-flight dumps before reports are generated. Per-type incident
caps prevent runaway disk usage.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from . import discovery
from .adb import Adb
from .analyzers.anr import analyze_anr_trace
from .analyzers.fingerprint import fingerprint_incident
from .analyzers.java_retrace import deobfuscate_stack
from .analyzers.native_symbolizer import symbolize_frames
from .backpressure import BackpressureController, EvidenceSampler
from .collectors.device_health import DeviceHealthMonitor
from .collectors.exit_info import exit_info_available, latest_watermark, query_exit_info
from .collectors.logcat import LogcatStream
from .collectors.resource_risk import ResourceRiskDetector, ResourceRiskMonitor
from .context import LogcatContextBuffer, LogEntry, format_context_slice
from .detection import (
    ALL_EVENT_TYPES,
    EVENT_ANR,
    EVENT_JAVA_CRASH,
    EVENT_NATIVE_CRASH,
    EVENT_PROCESS_DEATH,
    LOGCAT_LINE_RE,
    LogcatLineParser,
    StabilityEvent,
)
from .discovery import Process
from .dumpers import anr as anr_dumper
from .dumpers import base_name_for
from .dumpers import java_crash as java_crash_dumper
from .dumpers import native_crash as native_crash_dumper
from .dumpers import proc_death as proc_death_dumper
from .fusion import FusionEngine, Occurrence
from .journal import (
    STATUS_DROPPED_BY_BACKPRESSURE,
    STATUS_DROPPED_BY_CAP,
    STATUS_FAILED,
    STATUS_PERSISTED,
    STATUS_TIMED_OUT,
    IncidentJournal,
)
from .observations import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    SEVERITY_ERROR,
    SEVERITY_FATAL,
    SOURCE_EXIT_INFO,
    Observation,
    observation_from_event,
)
from .quota import QuotaConfig, QuotaTracker
from .selfmon import SelfMonitor
from .storage import CsvStreamWriter, LogStreamWriter
from .tasks import TaskCancelled, TaskContext
from .utils import utc_now_iso

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectionConfig:
    enable_java_crash: bool = True
    enable_native_crash: bool = True
    enable_anr: bool = True
    enable_process_death: bool = True
    # Host-time fallback window: used when device_ts is absent.
    dedup_window_sec: float = 5.0
    # Device-time window: dedup events from the same physical crash that arrive
    # via different logcat tags (e.g. libc + DEBUG for native crashes).
    device_ts_window_sec: float = 10.0


@dataclass(frozen=True)
class DumpsConfig:
    pre_context_sec: float = 30.0
    post_context_sec: float = 10.0
    max_incidents_per_type: int = 200
    max_concurrent: int = 2
    dump_shutdown_timeout_sec: float = 60.0
    context_retention_sec: Optional[float] = None
    context_buffer_max_lines: int = 5000
    context_buffer_max_bytes: int = 4 * 1024 * 1024
    max_disk_bytes: Optional[int] = None  # deprecated alias for min_free_bytes
    min_free_bytes: Optional[int] = None
    max_run_bytes: Optional[int] = None
    max_log_file_bytes: int = 512 * 1024 * 1024
    log_retention_hours: int = 24
    max_queue_size: int = 50
    evidence_sample_every_n: int = 5
    self_monitor_enabled: bool = True
    self_monitor_interval_sec: float = 60.0
    pull_tombstone: bool = True
    pull_anr_trace: bool = True


@dataclass(frozen=True)
class CollectorsConfig:
    logcat_enabled: bool = True
    logcat_buffers: tuple = ("main", "system", "events", "crash")
    logcat_reconnect_backoff_sec: float = 2.0
    device_health_interval_sec: float = 5.0
    device_reboot_policy: str = "wait-and-resume"
    resource_risk_enabled: bool = True
    resource_risk_interval_sec: float = 30.0
    resource_fd_growth_threshold: int = 200
    resource_thread_growth_threshold: int = 50
    resource_rss_growth_threshold_kb: int = 300 * 1024


@dataclass(frozen=True)
class DiagnosisConfig:
    mapping_file: Optional[str] = None
    retrace_command: Optional[str] = None
    native_symbols_dir: Optional[str] = None
    llvm_symbolizer_path: Optional[str] = None


DUMP_TASK_STATES = ("queued", "running", "persisted", "failed", "timed_out")

# Fault Lab marker: SAT_FAULT_BEGIN id=<uuid> type=<FAULT_TYPE> process=<name>
_FAULT_BEGIN_RE = re.compile(
    r"SAT_FAULT_BEGIN\s+id=(?P<id>\S+)\s+type=(?P<ftype>\S+)"
    r"(?:\s+process=(?P<process>\S+))?"
)
# ActivityManager process start (for startup-crash classification when the
# process dies before the watcher's first reconcile).
_START_PROC_RE = re.compile(r"Start proc\s+\d+:(?P<proc>\S+?)(?:/u\d+a?\d*)?\s+for\s+")
# App self-reported resource sample (Fault Lab SAT_RESOURCE_SAMPLE marker).
_RESOURCE_SAMPLE_RE = re.compile(
    r"SAT_RESOURCE_SAMPLE\s+id=(?P<id>\S+)"
    r"(?:\s+fd_count=(?P<fd>-?\d+))?"
    r"(?:\s+thread_count=(?P<threads>-?\d+))?"
    r"(?:\s+rss_kb=(?P<rss>-?\d+))?"
)
_FAULT_MARKER_TTL_SEC = 120.0


@dataclass
class _DumpTask:
    event: Optional[StabilityEvent] = None
    anchor_sec: float = 0.0
    deadline: float = 0.0
    state: str = "queued"
    future: Optional[concurrent.futures.Future] = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    _terminal_written: bool = False


class CollectorPool:
    def __init__(
        self,
        adb: Adb,
        package: str,
        *,
        events_writer: CsvStreamWriter,
        lifecycle_writer: CsvStreamWriter,
        logcat_writer: Optional[LogStreamWriter] = None,
        rescan_interval_sec: float = 5.0,
        process_filter: Optional[Iterable[str]] = None,
        detection: Optional[DetectionConfig] = None,
        dumps: Optional[DumpsConfig] = None,
        collectors: Optional[CollectorsConfig] = None,
        diagnosis: Optional[DiagnosisConfig] = None,
        incidents_dir: Optional[Path] = None,
        journal: Optional[IncidentJournal] = None,
        run_id: Optional[str] = None,
        adb_path: str = "adb",
        # Test injection points (production passes none):
        discover_fn: Optional[Callable[[Adb, str], List[Process]]] = None,
        on_fail_fast: Optional[Callable[[], None]] = None,
        logcat_stream_factory: Optional[Callable[[], LogcatStream]] = None,
        java_crash_dump_fn: Optional[Callable] = None,
        native_crash_dump_fn: Optional[Callable] = None,
        anr_dump_fn: Optional[Callable] = None,
        proc_death_dump_fn: Optional[Callable] = None,
        now_iso_fn: Optional[Callable[[], str]] = None,
        now_sec_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._adb = adb
        self._package = package
        self._events_writer = events_writer
        self._lifecycle_writer = lifecycle_writer
        self._logcat_writer = logcat_writer
        self._rescan_interval = float(rescan_interval_sec)
        self._filter = self._normalize_filter(process_filter, package)
        self._detection = detection or DetectionConfig()
        self._dumps_cfg = dumps or DumpsConfig()
        self._collectors_cfg = collectors or CollectorsConfig()
        self._diagnosis = diagnosis or DiagnosisConfig()
        self._incidents_dir = Path(incidents_dir) if incidents_dir else None
        if journal is None and self._incidents_dir is not None:
            journal = IncidentJournal(self._incidents_dir.parent / "incident_journal.jsonl")
        self._journal = journal
        self._run_id = run_id
        self._adb_path = adb_path

        self._discover = discover_fn or discovery.discover
        self._on_fail_fast = on_fail_fast
        self._logcat_stream_factory = logcat_stream_factory or self._default_logcat_factory
        self._java_crash_dump = java_crash_dump_fn or java_crash_dumper.run
        self._native_crash_dump = native_crash_dump_fn or native_crash_dumper.run
        self._anr_dump = anr_dump_fn or anr_dumper.run
        self._proc_death_dump = proc_death_dump_fn or proc_death_dumper.run
        self._now_iso = now_iso_fn or utc_now_iso
        self._now_sec = now_sec_fn or time.time

        self._procs: Dict[str, Process] = {}
        self._procs_lock = threading.RLock()
        self._gone_at: Dict[str, float] = {}
        # process → host monotonic time of its latest (re)start (S2-01:
        # startup-crash classification).
        self._proc_started_monotonic: Dict[str, float] = {}

        self._global_stop = threading.Event()
        self._logcat_thread: Optional[threading.Thread] = None
        self._watcher_thread: Optional[threading.Thread] = None
        self._logcat_stream: Optional[LogcatStream] = None
        self._logcat_stats: Dict = {}
        self._parser: Optional[LogcatLineParser] = None
        self._exit_watermark: Optional[float] = None
        self._exit_records: List[Dict] = []
        self._device_monitor: Optional[DeviceHealthMonitor] = None
        self._resource_monitor: Optional[ResourceRiskMonitor] = None
        self._quota = QuotaTracker(
            self._incidents_dir.parent if self._incidents_dir else Path("."),
            QuotaConfig(
                min_free_bytes=(self._dumps_cfg.min_free_bytes or self._dumps_cfg.max_disk_bytes),
                max_run_bytes=self._dumps_cfg.max_run_bytes,
                max_file_bytes=self._dumps_cfg.max_log_file_bytes,
                log_retention_hours=self._dumps_cfg.log_retention_hours,
                max_queue_size=self._dumps_cfg.max_queue_size,
                evidence_sample_every_n=self._dumps_cfg.evidence_sample_every_n,
            ),
        )
        self._backpressure = BackpressureController(
            max_queue_size=self._dumps_cfg.max_queue_size,
        )
        self._sampler = EvidenceSampler(
            every_n=self._dumps_cfg.evidence_sample_every_n,
        )
        self._self_monitor: Optional[SelfMonitor] = None

        self._fusion = FusionEngine(
            device_ts_window_sec=self._detection.device_ts_window_sec,
            host_window_sec=self._detection.dedup_window_sec,
            year=self._query_device_year(),
        )
        self._occurrence_by_event_id: Dict[str, Occurrence] = {}
        # event_id → incident base name (for later cross-source annotation).
        self._event_base_by_id: Dict[str, str] = {}
        # process → (fault_id, host_sec): latest SAT_FAULT_BEGIN marker per process.
        self._fault_markers: Dict[str, tuple] = {}
        self._exit_capable: Optional[bool] = None
        # (mtime, manifest) cache for the workload manifest (action windows).
        self._manifest_cache: Optional[tuple] = None
        # Capability probes (IMP-23): what this device/run can actually see.
        self._capabilities: Dict[str, Dict] = {}
        # Run-level DropBox cache: storm-bounded dumpsys calls (IMP-20).
        from .collectors.dropbox import CachingDropboxFetcher

        self._dropbox_fetcher = CachingDropboxFetcher(self._adb)
        # device_ts → first host monotonic time that line was observed
        # (context slices anchor on the first trigger line, IMP-09).
        self._first_host_ts_by_device_ts: Dict[str, float] = {}
        context_retention = self._dumps_cfg.context_retention_sec or (
            self._dumps_cfg.pre_context_sec + self._dumps_cfg.post_context_sec + 60.0
        )
        self._context_buffer = LogcatContextBuffer(
            retention_sec=context_retention,
            max_entries=self._dumps_cfg.context_buffer_max_lines,
            max_bytes=self._dumps_cfg.context_buffer_max_bytes,
            clock=self._now_sec,
        )
        self._accepting = True
        self._dispatch_lock = threading.Lock()
        self._dump_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, int(self._dumps_cfg.max_concurrent)),
            thread_name_prefix="dump-",
        )
        self._task_lock = threading.Lock()
        self._tasks: List[_DumpTask] = []
        self._pending_dumps = 0
        self._queue_peak = 0
        self._event_counts: Dict[str, int] = {t: 0 for t in ALL_EVENT_TYPES}
        self._dropped_by_cap = 0
        self._sample_failures: Dict[str, int] = {"logcat": 0}
        self._event_counts_lock = threading.Lock()

    # ------------------------------------------------------------------

    def start(self, initial_processes: Iterable[Process] = ()) -> None:
        with self._procs_lock:
            for p in initial_processes:
                if self._passes_filter(p):
                    self._procs[p.name] = p
                    self._write_lifecycle("new", p, old_pid=0, gap_sec=0.0)

        if self._collectors_cfg.logcat_enabled:
            self._logcat_thread = threading.Thread(
                target=self._logcat_loop,
                daemon=True,
                name="logcat-collector",
            )
            self._logcat_thread.start()

        self._device_monitor = DeviceHealthMonitor(
            self._adb,
            interval_sec=self._collectors_cfg.device_health_interval_sec,
            on_gap_started=self._on_device_gap,
            on_recovered=self._on_device_recovered,
        )
        self._device_monitor.start()

        if self._collectors_cfg.resource_risk_enabled:
            self._resource_monitor = ResourceRiskMonitor(
                self._adb,
                self._package,
                interval_sec=self._collectors_cfg.resource_risk_interval_sec,
                detector=ResourceRiskDetector(
                    fd_growth_threshold=(self._collectors_cfg.resource_fd_growth_threshold),
                    thread_growth_threshold=(self._collectors_cfg.resource_thread_growth_threshold),
                    rss_growth_threshold_kb=(self._collectors_cfg.resource_rss_growth_threshold_kb),
                ),
            )
            self._resource_monitor.start()

        if self._dumps_cfg.self_monitor_enabled:
            self._self_monitor = SelfMonitor(
                self._incidents_dir.parent if self._incidents_dir else Path("."),
                interval_sec=self._dumps_cfg.self_monitor_interval_sec,
                queue_depth_fn=self._backpressure.queued_count,
            )
            self._self_monitor.start()

        # Probe exit-info capability exactly once; later queries reuse it
        # (IMP-03: no repeated probe per query).
        try:
            self._exit_capable = exit_info_available(self._adb)
        except Exception:
            self._exit_capable = False
        self._probe_capabilities()
        try:
            history_watermark = latest_watermark(
                self._adb,
                self._package,
                available=self._exit_capable,
            )
        except Exception:
            log.exception("exit-info watermark query failed; using no watermark")
            history_watermark = None
        device_epoch = self._query_device_epoch_sec()
        # The run-start watermark is *at least* the device's current epoch so
        # records from previous runs can never pollute this run (IMP-03).
        if history_watermark is None:
            self._exit_watermark = device_epoch
        elif device_epoch is None:
            self._exit_watermark = history_watermark
        else:
            self._exit_watermark = max(history_watermark, device_epoch)
        if self._exit_watermark is not None:
            # Host/device clock skew tolerance: an event that happened right
            # after run start can carry a device timestamp a few seconds
            # before the watermark when the two clocks differ. 7 s covers
            # the observed ~6 s skew while keeping back-to-back runs from
            # seeing each other's records.
            self._exit_watermark = max(0.0, self._exit_watermark - 7.0)

        self._watcher_thread = threading.Thread(
            target=self._watch_loop,
            daemon=True,
            name="proc-watcher",
        )
        self._watcher_thread.start()

    def stop(
        self,
        join_timeout: float = 5.0,
        *,
        dump_shutdown_timeout_sec: Optional[float] = None,
    ) -> None:
        """Stop the pool in a fixed order and drain dump tasks.

        Order:
        1. stop accepting new events;
        2. stop + join the logcat thread, then flush the parser so an
           in-progress crash block is not silently lost;
        3. join the watcher thread;
        4. wait for dump tasks up to `dump_shutdown_timeout_sec` and mark
           anything still pending as `timed_out`;
        5. cancel queued work / shut the executor down.

        Writers and the final report are closed/built by the caller
        (`api.StabilityTest.stop()`), which runs after this returns.
        """
        self._accepting = False
        self._global_stop.set()
        if self._logcat_stream is not None:
            self._logcat_stream.stop()
        if self._logcat_thread is not None:
            self._logcat_thread.join(timeout=join_timeout)
        # Flush any in-progress parser block that arrived at the tail of the
        # stream; these are already-detected events, not new ones.
        if self._parser is not None:
            for event in self._parser.flush():
                self._dispatch_flushed(event)
        if self._watcher_thread is not None:
            self._watcher_thread.join(timeout=join_timeout)
        if self._device_monitor is not None:
            self._device_monitor.stop()
        if self._resource_monitor is not None:
            self._resource_monitor.stop()
        if self._self_monitor is not None:
            self._self_monitor.stop()

        records = []
        try:
            # The AM can finish writing an ExitInfo record a few seconds
            # after the visible crash; retry once and merge so a just-written
            # record is never missed at run end.
            for attempt in range(2):
                batch = query_exit_info(
                    self._adb,
                    self._package,
                    since_epoch=self._exit_watermark,
                    available=self._exit_capable,
                )
                if batch and attempt > 0:
                    known = {(r.pid, r.timestamp) for r in records}
                    records.extend(r for r in batch if (r.pid, r.timestamp) not in known)
                else:
                    records = list(batch)
                if records and attempt == 0:
                    break
                if not records:
                    time.sleep(0.5)
            self._exit_records = [r.to_dict() for r in records]
            log.info(
                "exit-info stop query: watermark=%s records=%d",
                self._exit_watermark,
                len(records),
            )
            self._fuse_exit_info(records)
        except Exception:
            log.exception("exit-info query failed at stop")

        # Drain pending dump tasks normally first.
        timeout = (
            self._dumps_cfg.dump_shutdown_timeout_sec
            if dump_shutdown_timeout_sec is None
            else dump_shutdown_timeout_sec
        )
        with self._task_lock:
            pending = [t for t in self._tasks if t.future is not None and not t.future.done()]
        if pending:
            deadline = time.monotonic() + max(0.0, float(timeout))
            for task in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    task.future.result(timeout=remaining)
                except BaseException:  # noqa: BLE001 - state already recorded by wrapper
                    pass

        # Signal cancellation for any tasks still not done.
        with self._task_lock:
            for task in self._tasks:
                if task.state in ("queued", "running") and (
                    task.future is None or not task.future.done()
                ):
                    task.cancelled.set()

        # Mark any still-unfinished tasks as timed_out (single terminal state).
        with self._task_lock:
            for task in self._tasks:
                if task.state in ("queued", "running") and (
                    task.future is None or not task.future.done()
                ):
                    task.state = "timed_out"
                    task._terminal_written = True
                    if self._journal is not None and task.event is not None:
                        try:
                            self._journal.terminal(
                                task.event.event_id or "",
                                STATUS_TIMED_OUT,
                                error_type="dump_shutdown_timeout",
                                error=("dump task did not finish within dump_shutdown_timeout_sec"),
                            )
                        except Exception:
                            log.exception("journal timed_out append failed")
        self._dump_executor.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        """Close the incident journal file handle (flush + close)."""
        if self._journal is not None:
            try:
                self._journal.close()
            except Exception:
                log.exception("journal close failed")

    # ------------------------------------------------------------------

    def current_processes(self) -> List[Process]:
        with self._procs_lock:
            return list(self._procs.values())

    def event_counts(self) -> Dict[str, int]:
        with self._event_counts_lock:
            return dict(self._event_counts)

    def dump_task_states(self) -> Dict[str, int]:
        counts = {s: 0 for s in DUMP_TASK_STATES}
        with self._task_lock:
            for task in self._tasks:
                counts[task.state] = counts.get(task.state, 0) + 1
        return counts

    def dropped_by_cap_count(self) -> int:
        with self._event_counts_lock:
            return self._dropped_by_cap

    def dropped_by_backpressure_count(self) -> int:
        return self._backpressure.dropped_count()

    def collector_status(self) -> Dict:
        out: Dict = {}
        if self._logcat_stream is not None:
            self._logcat_stats = dict(self._logcat_stream.stats)
        if self._logcat_stats:
            out["logcat"] = dict(self._logcat_stats)
        return out

    def queue_backlog_peak(self) -> int:
        with self._task_lock:
            return self._queue_peak

    def exit_info_records(self) -> List[Dict]:
        return [dict(r) for r in self._exit_records]

    def device_events(self) -> List[Dict]:
        if self._device_monitor is None:
            return []
        return [e.to_dict() for e in self._device_monitor.events()]

    def resource_risk_events(self) -> List[Dict]:
        if self._resource_monitor is None:
            return []
        return self._resource_monitor.events()

    def self_resource_summary(self) -> Dict:
        if self._self_monitor is None:
            return {}
        return self._self_monitor.summary()

    def _on_device_gap(self, kind: str) -> None:
        log.warning("device gap started: %s", kind)
        if self._collectors_cfg.device_reboot_policy == "fail-fast":
            self._accepting = False
            self._global_stop.set()
            if self._on_fail_fast is not None:
                try:
                    self._on_fail_fast()
                except Exception:
                    log.exception("fail-fast callback failed")
        if self._logcat_stream is not None:
            self._logcat_stream.stop()

    def _on_device_recovered(self) -> None:
        if self._global_stop.is_set():
            return
        log.info("device recovered; clearing process state and restarting logcat")
        with self._procs_lock:
            self._procs.clear()
            self._gone_at.clear()
        if self._collectors_cfg.logcat_enabled:
            self._logcat_thread = threading.Thread(
                target=self._logcat_loop,
                daemon=True,
                name="logcat-collector",
            )
            self._logcat_thread.start()

    def sample_failures(self) -> Dict[str, int]:
        with self._event_counts_lock:
            return dict(self._sample_failures)

    def quota_audit(self) -> List[Dict]:
        return [dict(a) for a in self._quota.audit]

    def capabilities(self) -> List[Dict]:
        """Machine-readable capability list (IMP-23)."""
        out = []
        for name, cap in sorted(self._capabilities.items()):
            out.append({"name": name, **cap})
        return out

    def _probe_capabilities(self) -> None:
        """Probe what this device/run can see (trace dirs, exit-info, ...)."""
        self._capabilities = {
            "exit_info": {
                "probe": "dumpsys activity exit-info",
                "status": "available" if self._exit_capable else "unavailable",
                "detail": (
                    "ApplicationExitInfo readable"
                    if self._exit_capable
                    else "command failed or no history"
                ),
            },
            "logcat": {
                "probe": f"adb logcat -b {','.join(self._collectors_cfg.logcat_buffers)}",
                "status": "available" if self._collectors_cfg.logcat_enabled else "disabled",
                "detail": "configured buffers",
            },
        }
        for name, remote_dir in (
            ("anr_trace_dir", "/data/anr/"),
            ("tombstone_dir", "/data/tombstones/"),
        ):
            try:
                r = self._adb.shell(
                    f"ls {remote_dir} >/dev/null 2>&1; echo $?",
                    check=False,
                    timeout=5.0,
                )
                accessible = r.returncode == 0 and r.stdout.strip() == "0"
            except Exception as e:  # noqa: BLE001 - capability probes stay quiet
                accessible = False
                self._capabilities[name] = {
                    "probe": f"ls {remote_dir}",
                    "status": "unavailable",
                    "detail": f"probe failed: {e}",
                    "degraded_path": "fallback to logcat/dropbox evidence",
                }
                continue
            self._capabilities[name] = {
                "probe": f"ls {remote_dir}",
                "status": "available" if accessible else "unavailable",
                "detail": ("pullable traces" if accessible else "permission denied on user build"),
                "degraded_path": (
                    None if accessible else "fallback_reason recorded; run continues"
                ),
            }

    # ── default factory ──

    def _default_logcat_factory(self) -> LogcatStream:
        return LogcatStream(
            serial=self._adb.serial,
            adb_path=self._adb_path,
            buffers=list(self._collectors_cfg.logcat_buffers),
            reconnect_backoff_sec=self._collectors_cfg.logcat_reconnect_backoff_sec,
            initial_device_ts=self._query_device_ts(),
        )

    def _query_device_ts(self) -> Optional[str]:
        """Return `MM-DD HH:MM:SS.mmm` for the initial logcat watermark."""
        try:
            r = self._adb.shell(
                "date +%m-%d_%H:%M:%S.000",
                check=False,
                timeout=3.0,
            )
        except Exception:
            return None
        if r.returncode != 0:
            return None
        ts = r.stdout.strip().replace("_", " ")
        return ts if ts else None

    def _query_device_year(self) -> Optional[int]:
        """Device year for parsing short-format logcat timestamps."""
        try:
            r = self._adb.shell("date +%Y", check=False, timeout=3.0)
        except Exception:
            return None
        if r.returncode != 0:
            return None
        try:
            return int(r.stdout.strip())
        except ValueError:
            return None

    def _query_device_epoch_sec(self) -> Optional[float]:
        """Current device epoch seconds (ExitInfo watermark floor, IMP-03)."""
        try:
            r = self._adb.shell("date +%s", check=False, timeout=3.0)
        except Exception:
            return None
        if r.returncode != 0:
            return None
        try:
            return float(r.stdout.strip())
        except ValueError:
            return None

    # ── filter ──

    @staticmethod
    def _normalize_filter(filter_list, package: str):
        if not filter_list:
            return None
        out = set()
        for f in filter_list:
            f = (f or "").strip()
            if not f or f == "main":
                out.add(package)
            elif f.startswith(":"):
                out.add(package + f)
            else:
                out.add(f)
        return out

    def _passes_filter(self, p: Process) -> bool:
        return self._filter is None or p.name in self._filter

    # ── logcat pipeline ──

    def _logcat_loop(self) -> None:
        parser = LogcatLineParser(
            self._package,
            now_iso_fn=self._now_iso,
            enable_java_crash=self._detection.enable_java_crash,
            enable_native_crash=self._detection.enable_native_crash,
            enable_anr=self._detection.enable_anr,
            enable_process_death=self._detection.enable_process_death,
        )
        self._parser = parser
        try:
            self._logcat_stream = self._logcat_stream_factory()
        except Exception:
            log.exception("logcat stream factory failed; logcat pipeline disabled")
            return
        try:
            for line in self._logcat_stream.lines():
                if self._global_stop.is_set():
                    break
                self._append_context_entry(line)
                self._record_fault_marker(line)
                if self._logcat_writer is not None:
                    try:
                        self._logcat_writer.write_line(line)
                    except Exception:
                        log.exception("logcat writer failed")
                try:
                    events = parser.feed_line(line)
                except Exception:
                    self._record_sample_failure("logcat")
                    log.exception("logcat parser failed on line")
                    continue
                for event in events:
                    self._dispatch(event)
            try:
                self._quota.enforce_log_retention()
            except Exception:
                log.exception("log retention failed")
            # End-of-stream: flush any in-progress block.
            for event in parser.flush():
                self._dispatch(event)
        finally:
            if self._logcat_stream is not None:
                self._logcat_stats = dict(self._logcat_stream.stats)
            self._logcat_stream = None

    # ── watcher pipeline ──

    def _watch_loop(self) -> None:
        ticks = 0
        try:
            self._reconcile()
        except Exception:
            log.exception("watcher initial reconcile failed")
        while not self._global_stop.is_set():
            if self._global_stop.wait(self._rescan_interval):
                break
            ticks += 1
            try:
                self._reconcile()
                # Periodic retention: run during the run, not only at the end
                # (IMP-12: background quota cleanup with an audit trail).
                if ticks % 12 == 0:
                    self._quota.enforce_log_retention()
            except Exception:
                log.exception("watcher reconcile failed")

    def _reconcile(self) -> None:
        try:
            live = self._discover(self._adb, self._package)
        except Exception:
            log.exception("discover failed during reconcile")
            return
        live = [p for p in live if self._passes_filter(p)]
        live_by_name: Dict[str, Process] = {p.name: p for p in live}

        with self._procs_lock:
            current_names = set(self._procs.keys())
            live_names = set(live_by_name.keys())

            for name in current_names - live_names:
                proc = self._procs.pop(name, None)
                if proc is None:
                    continue
                self._write_lifecycle("gone", proc, old_pid=proc.pid, gap_sec=0.0)
                self._gone_at[name] = self._now_sec()
                # process_death events are detected via am_proc_died / am_kill
                # in the logcat events buffer — no dispatch here.

            for name in live_names:
                proc = live_by_name[name]
                if name in self._procs:
                    if self._procs[name].pid != proc.pid:
                        old_pid = self._procs[name].pid
                        self._write_lifecycle("restart", proc, old_pid=old_pid, gap_sec=0.0)
                        self._procs[name] = proc
                        self._proc_started_monotonic[name] = self._now_sec()
                else:
                    gap = 0.0
                    event = "new"
                    if name in self._gone_at:
                        gap = max(0.0, self._now_sec() - self._gone_at.pop(name))
                        event = "restart"
                    self._procs[name] = proc
                    self._proc_started_monotonic[name] = self._now_sec()
                    self._write_lifecycle(event, proc, old_pid=0, gap_sec=gap)

    def _write_lifecycle(
        self,
        event: str,
        process: Process,
        *,
        old_pid: int,
        gap_sec: float,
    ) -> None:
        if self._lifecycle_writer is None:
            return
        self._lifecycle_writer.write_row(
            {
                "timestamp": self._now_iso(),
                "process_name": process.name,
                "event": event,
                "old_pid": old_pid,
                "new_pid": 0 if event == "gone" else process.pid,
                "gap_sec": round(gap_sec, 3),
            }
        )

    # ── dispatcher ──

    def _append_context_entry(self, line: str) -> None:
        m = LOGCAT_LINE_RE.match(line)
        if m:
            entry = LogEntry(
                host_ts=self._now_sec(),
                device_ts=m.group("ts"),
                pid=int(m.group("pid")),
                tid=int(m.group("tid")),
                tag=m.group("tag").strip(),
                level=m.group("level"),
                raw=line,
            )
        else:
            entry = LogEntry(
                host_ts=self._now_sec(),
                device_ts=None,
                pid=None,
                tid=None,
                tag="",
                level="",
                raw=line,
            )
        if entry.device_ts:
            with self._dispatch_lock:
                if entry.device_ts not in self._first_host_ts_by_device_ts:
                    self._first_host_ts_by_device_ts[entry.device_ts] = entry.host_ts
                    if len(self._first_host_ts_by_device_ts) > 2000:
                        oldest = next(iter(self._first_host_ts_by_device_ts))
                        self._first_host_ts_by_device_ts.pop(oldest, None)
        try:
            self._context_buffer.append(entry)
        except Exception:
            log.exception("context buffer append failed")

    def _dispatch(self, event: StabilityEvent) -> None:
        if not self._accepting:
            return
        self._dispatch_inner(event)

    def _dispatch_flushed(self, event: StabilityEvent) -> None:
        """Dispatch an event recovered from the parser during stop-flush."""
        self._dispatch_inner(event)

    def _record_fault_marker(self, line: str) -> None:
        """Track `SAT_FAULT_BEGIN` markers so later events carry a fault_id.

        Also consumes the app's self-reported `SAT_RESOURCE_SAMPLE` lines and
        feeds them to the resource-risk monitor — the only reliable per-app
        FD/thread source where Android hides `/proc/<pid>` from shell.
        """
        m = _FAULT_BEGIN_RE.search(line)
        if m:
            proc = m.group("process")
            with self._procs_lock:
                if proc:
                    self._fault_markers[proc] = (m.group("id"), self._now_sec())
                else:
                    # Marker without a process: associate with the main package.
                    self._fault_markers[self._package] = (m.group("id"), self._now_sec())
            return
        sp = _START_PROC_RE.search(line)
        if sp:
            with self._procs_lock:
                self._proc_started_monotonic.setdefault(
                    sp.group("proc"),
                    self._now_sec(),
                )
        rm = _RESOURCE_SAMPLE_RE.search(line)
        if rm and self._resource_monitor is not None:
            pid = 0
            lm = LOGCAT_LINE_RE.match(line)
            if lm:
                try:
                    pid = int(lm.group("pid"))
                except (ValueError, TypeError):
                    pid = 0
            from .collectors.resource_risk import ResourceSample

            def _int_or_none(value: Optional[str]) -> Optional[int]:
                if value is None:
                    return None
                try:
                    return int(value)
                except ValueError:
                    return None

            self._resource_monitor.submit_external_sample(
                ResourceSample(
                    pid=pid,
                    ts=self._now_sec(),
                    fd_count=_int_or_none(rm.group("fd")),
                    thread_count=_int_or_none(rm.group("threads")),
                    rss_kb=_int_or_none(rm.group("rss")),
                    process_start_time="app_self_report",
                )
            )

    def _fault_id_for(self, event: StabilityEvent) -> Optional[str]:
        if event.fault_id:
            return event.fault_id
        with self._procs_lock:
            entry = self._fault_markers.get(event.process)
        if entry is None:
            return None
        fault_id, ts = entry
        if self._now_sec() - ts > _FAULT_MARKER_TTL_SEC:
            return None
        return fault_id

    def _device_epoch(self) -> int:
        if self._device_monitor is None:
            return 1
        return 1 + self._device_monitor.pid_epoch

    def _dispatch_inner(self, event: StabilityEvent) -> None:
        event_id = str(uuid.uuid4())
        event.event_id = event_id
        event.run_id = self._run_id
        with self._dispatch_lock:
            event.fault_id = self._fault_id_for(event)
            observation = observation_from_event(
                event,
                device_epoch=self._device_epoch(),
                now_iso=self._now_iso(),
                now_sec=self._now_sec(),
                run_id=self._run_id,
            )
            is_new, occurrence = self._fusion.observe(observation, self._now_sec())
            if not is_new:
                # Same physical failure already tracked via another source or
                # replay; never create a second occurrence.
                return
            self._occurrence_by_event_id[event_id] = occurrence
            self._event_base_by_id[event_id] = base_name_for(event)
            with self._event_counts_lock:
                cap = self._dumps_cfg.max_incidents_per_type
                if self._event_counts.get(event.event_type, 0) >= cap:
                    log.warning(
                        "max incidents (%d) reached for %s; dropping", cap, event.event_type
                    )
                    self._dropped_by_cap += 1
                    self._journal_detected(event)
                    self._journal_terminal(
                        event_id,
                        STATUS_DROPPED_BY_CAP,
                        error_type="max_incidents_per_type",
                        error=f"incident cap {cap} reached for {event.event_type}",
                    )
                    return
                self._event_counts[event.event_type] = (
                    self._event_counts.get(event.event_type, 0) + 1
                )
            self._write_event_row(event)
        self._journal_detected(event)
        self._submit_dump(event)

    def _submit_dump(self, event: StabilityEvent) -> None:
        if not self._backpressure.try_acquire():
            self._journal_terminal(
                event.event_id or "",
                STATUS_DROPPED_BY_BACKPRESSURE,
                error_type="queue_full",
                error=f"dump queue full (max {self._backpressure.max_queue_size})",
            )
            return
        anchor_sec = self._now_sec()
        if event.device_ts:
            with self._dispatch_lock:
                anchor_sec = self._first_host_ts_by_device_ts.get(
                    event.device_ts,
                    anchor_sec,
                )
        task = _DumpTask(
            event=event,
            anchor_sec=anchor_sec,
            deadline=self._now_sec() + float(self._dumps_cfg.dump_shutdown_timeout_sec),
        )
        with self._task_lock:
            self._tasks.append(task)
            self._pending_dumps += 1
            self._queue_peak = max(self._queue_peak, self._pending_dumps)

        def run() -> dict:
            with self._task_lock:
                # Guard: stop() may have already claimed a terminal state.
                if task._terminal_written:
                    return {}
                task.state = "running"
            try:
                # Check cooperative cancellation before starting work.
                if task.cancelled.is_set():
                    raise TaskCancelled("dump cancelled before start")
                result = self._run_dump(event, task.anchor_sec, task)
            except BaseException as exc:
                # Single terminal state: only write if not already timed_out.
                with self._task_lock:
                    if not task._terminal_written:
                        task._terminal_written = True
                        task.state = "failed"
                    else:
                        # Another path (stop timeout) already claimed the terminal state.
                        return {}
                self._journal_terminal(
                    event.event_id or "",
                    STATUS_FAILED,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
                raise
            # Single terminal state: only write persisted if not already timed_out.
            with self._task_lock:
                if not task._terminal_written:
                    task._terminal_written = True
                    task.state = "persisted"
                else:
                    return result
            self._journal_terminal(event.event_id or "", STATUS_PERSISTED)
            return result

        def run_wrapper() -> dict:
            try:
                return run()
            finally:
                with self._task_lock:
                    self._pending_dumps -= 1
                self._backpressure.release()

        with self._task_lock:
            task.future = self._dump_executor.submit(run_wrapper)

    def _write_event_row(self, event: StabilityEvent) -> None:
        if self._events_writer is None:
            return
        try:
            self._events_writer.write_row(
                {
                    "timestamp": event.triggered_at,
                    "event_id": event.event_id or "",
                    "run_id": event.run_id or "",
                    "event_type": event.event_type,
                    "process_name": event.process,
                    "pid": event.pid,
                    "severity": event.severity,
                    "summary": event.summary[:500],
                    "source": event.source,
                    "fault_id": event.fault_id or "",
                }
            )
        except Exception:
            log.exception("events writer failed")

    def _journal_detected(self, event: StabilityEvent) -> None:
        if self._journal is None:
            return
        try:
            self._journal.detected(event.event_id or "", event)
        except Exception:
            log.exception("journal detected append failed")

    def _journal_terminal(
        self,
        event_id: str,
        status: str,
        *,
        error_type: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if self._journal is None:
            return
        try:
            self._journal.terminal(
                event_id,
                status,
                error_type=error_type,
                error=error,
            )
        except Exception:
            log.exception("journal terminal append failed (%s)", status)

    # ── workload action windows (spec S1-06 / IMP-08) ──

    def _read_workload_manifest(self) -> Optional[Dict]:
        if self._incidents_dir is None:
            return None
        path = self._incidents_dir.parent / "workload_manifest.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if self._manifest_cache is not None and self._manifest_cache[0] == mtime:
            return self._manifest_cache[1]
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        self._manifest_cache = (mtime, manifest)
        return manifest

    @staticmethod
    def _iso_epoch(ts: str) -> Optional[float]:
        try:
            from datetime import datetime, timezone

            normalized = ts.strip().replace("Z", "+00:00").replace(" ", "T")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, AttributeError):
            return None

    def _workload_expected_exit(self, event: StabilityEvent) -> bool:
        """True only when a manifest action declares this fault id AND its
        expected-exit window covers the event time."""
        if not event.fault_id:
            return False
        manifest = self._read_workload_manifest()
        actions = (manifest or {}).get("actions") or []
        event_ts = self._iso_epoch(event.triggered_at)
        if event_ts is None:
            return False
        for action in actions:
            if action.get("fault_id") != event.fault_id:
                continue
            if not action.get("expected_exit"):
                continue
            started = self._iso_epoch(action.get("started_at") or "")
            if started is None:
                continue
            window = float(action.get("window_sec") or 0)
            if window <= 0:
                window = 120.0  # marker-based actions: generous default window
            if started - 5.0 <= event_ts <= started + window:
                return True
        return False

    # ── ExitInfo fusion (spec S1-03 / IMP-03) ──

    @staticmethod
    def _exit_info_event_type(rec) -> Optional[str]:
        reason = getattr(rec, "exit_reason", "")
        if reason == "anr":
            return EVENT_ANR
        if reason == "crashed":
            desc = (getattr(rec, "description", "") or "").lower()
            if any(k in desc for k in ("signal", "sigsegv", "sigabrt", "native", "tombstone")):
                return EVENT_NATIVE_CRASH
            # The record alone cannot distinguish Java from native: callers
            # first try to attach to the crash occurrence the time window
            # already holds; only a truly unmatched record defaults to java.
            return "crashed_generic"
        return EVENT_PROCESS_DEATH

    @staticmethod
    def _exit_taxonomy(rec) -> str:
        """expected / failure / unknown three-state exit taxonomy (S2-04)."""
        if getattr(rec, "expected", False):
            return "expected"
        if getattr(rec, "exit_reason", "") in (
            "crashed",
            "anr",
            "low_memory",
            "signaled",
            "initialization_failure",
            "dependency_death",
            "excessive_resource_usage",
        ):
            return "failure"
        return "unknown"

    def _fuse_exit_info(self, records) -> None:
        """Fuse run-end ExitInfo records into the occurrence model.

        - Records already seen via logcat attach `exit_info` as a supporting
          source (annotating the incident JSON, cross-source dedup).
        - Unmatched *failure* records become new incidents so a crash/ANR/LMK
          that happened during a logcat gap can never produce a false "stable".
        - Expected exits are audited in the `exit_info` list only.
        """
        for rec in records:
            if getattr(rec, "expected", False):
                continue
            event_type = self._exit_info_event_type(rec)
            obs_type = "process_exit" if event_type == EVENT_PROCESS_DEATH else event_type
            observation = Observation(
                source=SOURCE_EXIT_INFO,
                source_record_id=(f"exit-{getattr(rec, 'pid', 0)}-{getattr(rec, 'timestamp', '')}"),
                process=getattr(rec, "process", ""),
                pid=getattr(rec, "pid", 0) or 0,
                type=obs_type,
                subtype=getattr(rec, "exit_reason", ""),
                severity=(
                    SEVERITY_FATAL
                    if event_type in (EVENT_JAVA_CRASH, EVENT_NATIVE_CRASH, EVENT_ANR)
                    else SEVERITY_ERROR
                ),
                expected=False,
                device_event_time=getattr(rec, "timestamp", "") or None,
                host_received_at=self._now_iso(),
                host_monotonic_sec=self._now_sec(),
                device_epoch=self._device_epoch(),
                confidence=(
                    CONFIDENCE_HIGH if event_type != EVENT_PROCESS_DEATH else CONFIDENCE_MEDIUM
                ),
                extra={
                    "exit_subreason": getattr(rec, "exit_subreason", "") or "",
                    "description": getattr(rec, "description", "") or "",
                    "status": getattr(rec, "status", "") or "",
                    "importance": getattr(rec, "importance", "") or "",
                    "pss_kb": getattr(rec, "pss_kb", None),
                    "rss_kb": getattr(rec, "rss_kb", None),
                    "raw_reason": getattr(rec, "raw_reason", "") or "",
                },
            )
            if observation.subtype != "crashed" and not getattr(rec, "expected", False):
                # A non-crash failure record (e.g. excessive_resource_usage
                # written by the AM for a crash while cached) corroborates a
                # same-window crash occurrence: attach, never double-count.
                any_occ = self._fusion.find_any_window_occurrence(observation)
                if any_occ is not None and any_occ.type in (
                    EVENT_JAVA_CRASH,
                    EVENT_NATIVE_CRASH,
                    EVENT_ANR,
                ):
                    self._annotate_incident_sources(any_occ, "exit_info", rec)
                    continue
            if observation.subtype == "crashed":
                existing = self._fusion.find_compatible(
                    observation,
                    (EVENT_JAVA_CRASH, EVENT_NATIVE_CRASH),
                )
                if existing is not None:
                    observation = Observation(
                        source=observation.source,
                        source_record_id=observation.source_record_id,
                        process=observation.process,
                        pid=observation.pid,
                        type=existing.type,
                        subtype=observation.subtype,
                        severity=observation.severity,
                        expected=False,
                        device_event_time=observation.device_event_time,
                        host_monotonic_sec=observation.host_monotonic_sec,
                        device_epoch=observation.device_epoch,
                        extra=dict(observation.extra),
                    )
                else:
                    # The AM may have classified this death differently (e.g.
                    # `excessive_resource_usage` for a crash while cached).
                    # The record still corroborates the crash: attach it as a
                    # supporting source instead of fabricating an incident.
                    any_occ = self._fusion.find_any_window_occurrence(observation)
                    if any_occ is not None:
                        self._annotate_incident_sources(any_occ, "exit_info", rec)
                        continue
            is_new, occurrence = self._fusion.observe(observation, self._now_sec())
            if not is_new:
                self._annotate_incident_sources(occurrence, "exit_info", rec)
            else:
                self._create_exit_info_incident(observation, rec)

    def _annotate_incident_sources(self, occurrence: Occurrence, source: str, rec) -> None:
        """Add a supporting source to the incident JSON of an occurrence."""
        if self._incidents_dir is None:
            return
        event_id = None
        with self._dispatch_lock:
            for eid, occ in self._occurrence_by_event_id.items():
                if occ is occurrence:
                    event_id = eid
                    break
        if event_id is None:
            return
        base = self._event_base_by_id.get(event_id)
        if base is None:
            return
        path = self._incidents_dir / f"{base}.json"
        if not path.exists():
            return
        try:
            incident = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        evidence = incident.setdefault("evidence", {})
        sources = list(evidence.get("supporting_sources") or [])
        if source not in sources and evidence.get("source") != source:
            sources.append(source)
        evidence["supporting_sources"] = sources
        for key in (
            "exit_info_reason",
            "exit_subreason",
            "exit_info_description",
            "exit_info_status",
            "exit_info_importance",
            "exit_info_pss_kb",
            "exit_info_rss_kb",
            "exit_info_raw_reason",
        ):
            evidence.pop(key, None)
        evidence["exit_info_reason"] = getattr(rec, "exit_reason", "")
        if getattr(rec, "exit_subreason", ""):
            evidence["exit_subreason"] = rec.exit_subreason
        if getattr(rec, "description", ""):
            evidence["exit_info_description"] = rec.description
        if getattr(rec, "status", ""):
            evidence["exit_info_status"] = rec.status
        if getattr(rec, "importance", ""):
            evidence["exit_info_importance"] = rec.importance
        if getattr(rec, "pss_kb", None) is not None:
            evidence["exit_info_pss_kb"] = rec.pss_kb
        if getattr(rec, "rss_kb", None) is not None:
            evidence["exit_info_rss_kb"] = rec.rss_kb
        if getattr(rec, "raw_reason", ""):
            evidence["exit_info_raw_reason"] = rec.raw_reason
        from .atomic_io import atomic_write_json

        try:
            atomic_write_json(path, incident)
        except Exception:
            log.exception("incident source annotation write failed")

    def _exit_info_triggered_at(self, rec) -> str:
        """Triggered-at for an ExitInfo-only incident.

        ExitInfo records are written by the framework asynchronously and may
        be flushed long after the actual exit. `timestamp_epoch` is the
        device-tz-corrected real UTC epoch of the exit, so it places the
        incident on the timeline where the exit really happened instead of
        stacking every late flush at the poll time (soak review finding).
        Falls back to the observation time when the epoch is unavailable.
        """
        ts_epoch = getattr(rec, "timestamp_epoch", None)
        if ts_epoch is None:
            return self._now_iso()
        return (
            datetime.fromtimestamp(ts_epoch, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("T", " ")
            .replace("+00:00", "")
        )

    def _create_exit_info_incident(self, observation: Observation, rec) -> None:
        """Create a full incident for an ExitInfo-only failure (IMP-03)."""
        event_id = str(uuid.uuid4())
        event_type = self._exit_info_event_type(rec) or EVENT_PROCESS_DEATH
        if event_type == "crashed_generic":
            # No existing crash occurrence in the window: default to java
            # (most Android crashes are Java) with reduced confidence.
            event_type = EVENT_JAVA_CRASH
        event = StabilityEvent(
            event_type=event_type,
            process=observation.process,
            pid=observation.pid,
            triggered_at=self._exit_info_triggered_at(rec),
            severity=observation.severity,
            summary=(
                f"exit-info {observation.subtype}"
                + (
                    f": {getattr(rec, 'description', '')[:120]}"
                    if getattr(rec, "description", "")
                    else ""
                )
            ),
            source=SOURCE_EXIT_INFO,
            reason=observation.subtype,
            device_ts=observation.device_event_time,
            event_id=event_id,
            run_id=self._run_id,
        )
        with self._event_counts_lock:
            self._event_counts[event.event_type] = self._event_counts.get(event.event_type, 0) + 1
        self._write_event_row(event)
        self._journal_detected(event)
        self._journal_terminal(event_id, STATUS_PERSISTED)
        self._occurrence_by_event_id[event_id] = self._fusion.occurrences()[-1]
        if self._incidents_dir is not None:
            try:
                from .dumpers import build_incident_dict, write_incident

                evidence_extra = {
                    "source": SOURCE_EXIT_INFO,
                    "supporting_sources": [SOURCE_EXIT_INFO],
                    "exit_info_reason": getattr(rec, "exit_reason", ""),
                    "exit_taxonomy": self._exit_taxonomy(rec),
                    "detection_confidence": observation.confidence,
                    "evidence_completeness": "exit_info_only",
                }
                if getattr(rec, "exit_subreason", ""):
                    evidence_extra["exit_subreason"] = rec.exit_subreason
                if getattr(rec, "description", ""):
                    evidence_extra["exit_info_description"] = rec.description
                if getattr(rec, "status", ""):
                    evidence_extra["exit_info_status"] = rec.status
                if getattr(rec, "importance", ""):
                    evidence_extra["exit_info_importance"] = rec.importance
                if getattr(rec, "pss_kb", None) is not None:
                    evidence_extra["exit_info_pss_kb"] = rec.pss_kb
                if getattr(rec, "rss_kb", None) is not None:
                    evidence_extra["exit_info_rss_kb"] = rec.rss_kb
                if getattr(rec, "raw_reason", ""):
                    evidence_extra["exit_info_raw_reason"] = rec.raw_reason
                incident = build_incident_dict(
                    event,
                    logcat_slice_file=None,
                    trace_file=None,
                    fallback_reason="detected via ApplicationExitInfo (logcat missed)",
                    extra_evidence=evidence_extra,
                )
                base = base_name_for(event)
                write_incident(self._incidents_dir / f"{base}.json", incident)
            except Exception:
                log.exception("exit-info incident write failed")

    def _attach_context(
        self,
        event: StabilityEvent,
        anchor_sec: float,
        *,
        ctx: Optional[TaskContext] = None,
        staging: Optional[Path] = None,
    ) -> None:
        """Wait for the post-context window, then snapshot and write the slice.

        When `staging` is given the context file is written into the task's
        staging directory and only becomes visible after publish.
        """
        pre_sec = self._dumps_cfg.pre_context_sec
        post_sec = self._dumps_cfg.post_context_sec
        deadline = anchor_sec + max(0.0, float(post_sec))
        now = self._now_sec()
        while now < deadline and not self._global_stop.is_set():
            self._global_stop.wait(min(0.1, max(0.0, deadline - now)))
            now = self._now_sec()

        slice_ = self._context_buffer.snapshot(
            anchor_sec,
            pre_sec=pre_sec,
            post_sec=post_sec,
            now_ts=now,
        )
        if self._global_stop.is_set() and now < deadline:
            slice_.post_missing_reason = "run_stopped_early"
        if slice_.dropped_by_cap_count > 0 and pre_sec > 0:
            slice_.pre_missing_reason = "buffer_overflow_dropped"

        event.context_meta = {
            "pre_context_sec": round(float(pre_sec), 3),
            "post_context_sec": round(float(post_sec), 3),
            "pre_context_sec_actual": slice_.pre_context_sec_actual,
            "post_context_sec_actual": slice_.post_context_sec_actual,
            "pre_context_missing_reason": slice_.pre_missing_reason,
            "post_context_missing_reason": slice_.post_missing_reason,
            "context_buffer_dropped_count": slice_.dropped_by_cap_count,
        }
        if self._incidents_dir is None:
            return
        if pre_sec <= 0 and post_sec <= 0:
            return
        if self._quota.hard_reached:
            event.context_meta["disk_quota_skipped"] = True
            return
        try:
            base = base_name_for(event)
            target_dir = staging if staging is not None else self._incidents_dir
            path = target_dir / f"{base}_context.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                format_context_slice(event.raw_lines, slice_),
                encoding="utf-8",
            )
            event.context_file = path.name
        except Exception:
            log.exception("context slice write failed")

    def _staging_root(self) -> Path:
        """Task staging area, *outside* the output dir so late workers can
        never modify the frozen run directory after `stop()` returns."""
        import tempfile

        root = Path(tempfile.gettempdir()) / "sat-staging"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _publish_staging(self, staging: Path) -> None:
        """Atomically move every staged evidence file into the incident dir.

        Runs only after the task completed within its deadline and was not
        cancelled, so anything published here is a consistent snapshot.
        """
        assert self._incidents_dir is not None
        self._incidents_dir.mkdir(parents=True, exist_ok=True)
        for src in sorted(staging.iterdir()):
            if src.is_file():
                dst = self._incidents_dir / src.name
                os.replace(src, dst)
        try:
            staging.rmdir()
        except OSError:
            pass

    def _cleanup_staging(self, staging: Optional[Path]) -> None:
        if staging is None or not staging.exists():
            return
        import shutil

        try:
            shutil.rmtree(staging, ignore_errors=True)
        except Exception:
            log.exception("staging cleanup failed")

    @staticmethod
    def _call_dumper(fn, adb, event, incidents_dir, **extra):
        """Call a dumper, passing only the kwargs it accepts.

        Production dumpers take `ctx`/`staging_dir`; test-injected fakes often
        keep the legacy `(adb, event, incidents_dir)` signature.
        """
        import inspect

        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return fn(adb, event, incidents_dir)
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return fn(adb, event, incidents_dir, **extra)
        filtered = {k: v for k, v in extra.items() if k in params}
        return fn(adb, event, incidents_dir, **filtered)

    def _run_dump(self, event: StabilityEvent, anchor_sec: float, task: _DumpTask) -> dict:
        staging: Optional[Path] = None
        if self._incidents_dir is not None:
            staging = self._staging_root() / f"{base_name_for(event)}-{event.event_id}"
            staging.mkdir(parents=True, exist_ok=True)
        ctx = TaskContext(
            deadline=task.deadline,
            cancelled=task.cancelled,
            now_fn=self._now_sec,
        )
        try:
            self._attach_context(event, anchor_sec, ctx=ctx, staging=staging)
            if self._incidents_dir is None:
                return {}
            if event.event_type == EVENT_JAVA_CRASH:
                incident = self._call_dumper(
                    self._java_crash_dump,
                    self._adb,
                    event,
                    self._incidents_dir,
                    ctx=ctx,
                    staging_dir=staging,
                    fetcher=self._dropbox_fetcher,
                )
            elif event.event_type == EVENT_NATIVE_CRASH:
                incident = self._call_dumper(
                    self._native_crash_dump,
                    self._adb,
                    event,
                    self._incidents_dir,
                    pull_tombstone=self._dumps_cfg.pull_tombstone,
                    ctx=ctx,
                    staging_dir=staging,
                    fetcher=self._dropbox_fetcher,
                )
            elif event.event_type == EVENT_ANR:
                incident = self._call_dumper(
                    self._anr_dump,
                    self._adb,
                    event,
                    self._incidents_dir,
                    pull_anr_trace=self._dumps_cfg.pull_anr_trace,
                    ctx=ctx,
                    staging_dir=staging,
                    fetcher=self._dropbox_fetcher,
                )
            elif event.event_type == EVENT_PROCESS_DEATH:
                incident = self._call_dumper(
                    self._proc_death_dump,
                    self._adb,
                    event,
                    self._incidents_dir,
                    ctx=ctx,
                    staging_dir=staging,
                )
            else:
                raise ValueError(f"unknown event type: {event.event_type}")
            self._postprocess_incident(event, incident, staging=staging)
            # Only publish a task that finished in time and was not cancelled.
            ctx.check()
            with self._task_lock:
                if task._terminal_written:
                    raise TaskCancelled("terminal state claimed by stop(); staging not published")
            self._publish_staging(staging)
            return incident
        except BaseException:
            self._cleanup_staging(staging)
            raise

    def _postprocess_incident(
        self,
        event: StabilityEvent,
        incident: dict,
        *,
        staging: Optional[Path] = None,
    ) -> None:
        """Apply diagnosis analyzers and rewrite the incident JSON atomically.

        With `staging` set, the JSON is written into the staging directory and
        becomes visible only after publish.
        """
        evidence = incident.setdefault("evidence", {})
        if event.event_type == EVENT_PROCESS_DEATH and self._workload_expected_exit(event):
            # Only an action window declaring this fault id as an expected
            # exit marks the death expected (IMP-08) — never the mere
            # presence of a workload manifest.
            evidence["workload_expected"] = True
        fingerprint = fingerprint_incident(incident)
        decision = self._sampler.decide(fingerprint)
        if decision == "occurrence_only":
            for key in ("context_file", "trace_file", "logcat_slice_file", "dropbox_file"):
                evidence.pop(key, None)
            evidence["sampled"] = True
            evidence["sample_reason"] = "occurrence_only"
        else:
            evidence["sampled"] = False

        if event.event_type == EVENT_JAVA_CRASH:
            # S2-01: subtype / crashing thread / startup crash classification.
            from .analyzers.java_crash import classify_java_crash

            with self._procs_lock:
                started_mono = self._proc_started_monotonic.get(event.process)
            classification = classify_java_crash(
                exception_class=event.exception_class,
                summary=event.summary,
                crashing_thread=getattr(event, "crashing_thread", None),
                process_start_host_sec=started_mono,
                crash_host_sec=self._now_sec(),
            )
            evidence["subtype"] = classification["subtype"]
            evidence["crashing_thread"] = classification["crashing_thread"]
            evidence["thread_category"] = classification["thread_category"]
            evidence["startup_crash"] = classification["startup_crash"]
        if event.event_type == EVENT_JAVA_CRASH and self._diagnosis.mapping_file:
            result = deobfuscate_stack(
                evidence.get("top_frames", []),
                mapping_path=Path(self._diagnosis.mapping_file),
                retrace_command=self._diagnosis.retrace_command,
            )
            evidence["symbolication_status"] = result.status
            if result.error:
                evidence["symbolication_error"] = result.error
            if result.frames:
                evidence["deobfuscated_frames"] = result.frames
        elif event.event_type == EVENT_NATIVE_CRASH and (
            self._diagnosis.native_symbols_dir or self._diagnosis.llvm_symbolizer_path
        ):
            result = symbolize_frames(
                evidence.get("top_frames", []),
                symbols_dir=(
                    Path(self._diagnosis.native_symbols_dir)
                    if self._diagnosis.native_symbols_dir
                    else None
                ),
                llvm_symbolizer=self._diagnosis.llvm_symbolizer_path,
            )
            evidence["symbolication_status"] = result.status
            if result.error:
                evidence["symbolication_error"] = result.error
            if result.frames:
                evidence["symbolized_frames"] = result.frames
        elif event.event_type == EVENT_ANR:
            trace_lines: List[str] = list(event.raw_lines)
            trace_file = evidence.get("trace_file")
            search_dir = staging if staging is not None else self._incidents_dir
            if trace_file and search_dir is not None and (search_dir / trace_file).exists():
                trace_lines = (
                    (search_dir / trace_file)
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()
                )
            evidence["diagnosis"] = analyze_anr_trace(
                trace_lines,
                reason=evidence.get("reason"),
            )

        if self._quota.hard_reached and event.event_type != EVENT_PROCESS_DEATH:
            evidence["disk_quota_skipped"] = True

        base = base_name_for(event)
        target_dir = staging if staging is not None else self._incidents_dir
        path = target_dir / f"{base}.json"
        if path.exists():
            from .dumpers import write_incident

            write_incident(path, incident)

    def _record_sample_failure(self, source: str) -> None:
        with self._event_counts_lock:
            self._sample_failures[source] = self._sample_failures.get(source, 0) + 1
